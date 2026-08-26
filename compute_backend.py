"""CPU and optional CUDA sparse solver backends."""

from contextlib import nullcontext
from dataclasses import dataclass
import os
import time

import numpy as np
import scipy.sparse
import scipy.sparse.linalg

try:
    import pypardiso
except ImportError:
    pypardiso = None

try:
    from threadpoolctl import threadpool_limits
except ImportError:
    threadpool_limits = None

try:
    from .runtime_config import RuntimeComputeSettings
except (ImportError, ValueError):
    from runtime_config import RuntimeComputeSettings


@dataclass
class ComputeMetadata:
    backend: str
    device: str = "CPU"
    solve_seconds: float = 0.0
    transfer_seconds: float = 0.0
    relative_residual: float = 0.0
    iterations: int = 1
    cpu_threads: int = 1
    fallback_reason: str = ""
    cache_hit: bool = False
    matrix_reused: bool = False
    matrix_assembly: str = "CPU_CSR"


@dataclass
class ComputeSolution:
    values: np.ndarray
    metadata: ComputeMetadata


def cuda_diagnostics():
    info = {
        "available": False,
        "cupy_version": "not installed",
        "devices": [],
        "driver_version": None,
        "runtime_version": None,
        "error": "",
    }
    try:
        import cupy as cp
        info["cupy_version"] = cp.__version__
        info["driver_version"] = int(cp.cuda.runtime.driverGetVersion())
        info["runtime_version"] = int(cp.cuda.runtime.runtimeGetVersion())
        count = int(cp.cuda.runtime.getDeviceCount())
        for index in range(count):
            with cp.cuda.Device(index):
                props = cp.cuda.runtime.getDeviceProperties(index)
                name = props.get("name", "CUDA device")
                if isinstance(name, bytes):
                    name = name.decode(errors="replace")
                free_bytes, total_bytes = cp.cuda.runtime.memGetInfo()
                info["devices"].append({
                    "index": index,
                    "name": str(name),
                    "free_bytes": int(free_bytes),
                    "total_bytes": int(total_bytes),
                })
        info["available"] = bool(info["devices"])
    except Exception as exc:
        info["error"] = str(exc)
    return info


class SparseComputeBackend:
    def __init__(self, settings=None, log_callback=None):
        self.settings = (settings or RuntimeComputeSettings()).normalized()
        self.log_callback = log_callback
        self._cuda_info = None
        self._cuda_workspaces = {}

    def _log(self, message):
        if self.log_callback:
            self.log_callback(f"[COMPUTE] {message}")

    def _cpu_threads(self):
        if not self.settings.cpu_multithread:
            return 1
        return self.settings.cpu_threads or max(1, os.cpu_count() or 1)

    def _select(self, node_count):
        requested = self.settings.backend
        if requested == "CPU":
            return "CPU"
        if requested == "CUDA" and not self.settings.cuda_enabled:
            raise RuntimeError("CUDA was forced but CUDA acceleration is disabled.")
        if not self.settings.cuda_enabled:
            return "CPU"
        if self._cuda_info is None:
            self._cuda_info = cuda_diagnostics()
        diagnostics = self._cuda_info
        if requested == "CUDA":
            if not diagnostics["available"]:
                raise RuntimeError(
                    "CUDA was forced but is unavailable: " +
                    (diagnostics["error"] or "no CUDA device detected")
                )
            return "CUDA"
        return "CUDA" if (
            diagnostics["available"] and node_count >= self.settings.cuda_min_nodes
        ) else "CPU"

    @staticmethod
    def _residual(matrix, values, rhs):
        numerator = np.linalg.norm(matrix.dot(values) - rhs)
        denominator = max(float(np.linalg.norm(rhs)), 1.0e-30)
        return float(numerator / denominator)

    def solve(self, matrix, rhs, system_kind="SPD", cache_key=None, matrix_values_static=False):
        dtype = np.complex128 if (np.iscomplexobj(matrix.data) or np.iscomplexobj(rhs)) else np.float64
        if scipy.sparse.isspmatrix_coo(matrix):
            matrix = matrix.astype(dtype, copy=False)
        else:
            matrix = scipy.sparse.csr_matrix(matrix, dtype=dtype)
            matrix.sort_indices()
        rhs = np.asarray(rhs, dtype=dtype)
        selected = self._select(matrix.shape[0])
        if selected == "CUDA":
            try:
                return self._solve_cuda(
                    matrix, rhs, system_kind, cache_key=cache_key,
                    matrix_values_static=matrix_values_static,
                )
            except Exception as exc:
                if self.settings.backend == "CUDA":
                    raise
                self._log(f"CUDA fallback to CPU: {exc}")
                result = self._solve_cpu(matrix, rhs)
                result.metadata.fallback_reason = str(exc)
                return result
        return self._solve_cpu(matrix, rhs)

    def _solve_cpu(self, matrix, rhs):
        matrix = scipy.sparse.csr_matrix(matrix, dtype=matrix.dtype)
        matrix.sort_indices()
        threads = self._cpu_threads()
        context = threadpool_limits(limits=threads) if threadpool_limits else nullcontext()
        started = time.perf_counter()
        with context:
            if pypardiso is not None and not np.iscomplexobj(matrix.data):
                values = pypardiso.spsolve(matrix, rhs)
                backend_name = "CPU_PARDISO"
                effective_threads = threads
            else:
                values = scipy.sparse.linalg.spsolve(matrix, rhs)
                backend_name = "CPU_SCIPY"
                effective_threads = 1
        elapsed = time.perf_counter() - started
        values = np.asarray(values, dtype=matrix.dtype)
        return ComputeSolution(values, ComputeMetadata(
            backend=backend_name,
            solve_seconds=elapsed,
            relative_residual=self._residual(matrix, values, rhs),
            cpu_threads=effective_threads,
        ))

    def _solve_cuda(self, matrix, rhs, system_kind, cache_key=None, matrix_values_static=False):
        import cupy as cp
        import cupyx.scipy.sparse as cpx_sparse
        import cupyx.scipy.sparse.linalg as cpx_linalg

        device_index = self.settings.cuda_device
        with cp.cuda.Device(device_index):
            free_bytes, _ = cp.cuda.runtime.memGetInfo()
            is_coo = scipy.sparse.isspmatrix_coo(matrix)
            if is_coo:
                host_matrix_bytes = matrix.data.nbytes + matrix.row.nbytes + matrix.col.nbytes
            else:
                host_matrix_bytes = matrix.data.nbytes + matrix.indices.nbytes + matrix.indptr.nbytes
            estimated_bytes = int(host_matrix_bytes * 2 + rhs.nbytes * 8)
            if estimated_bytes > 0.75 * free_bytes:
                raise MemoryError(
                    f"CUDA solve needs an estimated {estimated_bytes / (1024 ** 3):.2f} GiB "
                    f"but only {free_bytes / (1024 ** 3):.2f} GiB is free."
                )
            transfer_started = time.perf_counter()
            workspace_key = None if cache_key is None else (
                device_index, cache_key, matrix.shape, matrix.dtype.str, str(system_kind).upper()
            )
            workspace = self._cuda_workspaces.get(workspace_key) if workspace_key is not None else None
            same_cached_source = bool(
                workspace is not None and matrix_values_static and
                workspace.get("source_identity") == id(matrix)
            )
            structure_matches = same_cached_source
            if workspace is not None and not is_coo and not same_cached_source:
                structure_matches = bool(
                    workspace.get("indices") is not None and
                    workspace.get("indptr") is not None and
                    np.array_equal(workspace["indices"], matrix.indices) and
                    np.array_equal(workspace["indptr"], matrix.indptr)
                )
            matrix_reused = bool(structure_matches and matrix_values_static)
            if structure_matches:
                gpu_matrix = workspace["matrix"]
                if not matrix_values_static:
                    gpu_matrix.data.set(matrix.data)
            else:
                if is_coo:
                    gpu_matrix = cpx_sparse.coo_matrix(
                        (
                            cp.asarray(matrix.data),
                            (cp.asarray(matrix.row), cp.asarray(matrix.col)),
                        ),
                        shape=matrix.shape,
                    ).tocsr()
                    matrix_assembly = "CUDA_COO_TO_CSR"
                else:
                    gpu_matrix = cpx_sparse.csr_matrix(matrix)
                    matrix_assembly = "CUDA_CSR_TRANSFER"
            if matrix_reused:
                matrix_assembly = "CUDA_CSR_REUSED"
            elif structure_matches:
                matrix_assembly = "CUDA_CSR_VALUES_UPDATED"
            gpu_rhs = cp.asarray(rhs)
            props = cp.cuda.runtime.getDeviceProperties(device_index)
            device_name = props.get("name", f"CUDA {device_index}")
            if isinstance(device_name, bytes):
                device_name = device_name.decode(errors="replace")

            if matrix_reused:
                preconditioner = workspace["preconditioner"]
            else:
                diagonal = gpu_matrix.diagonal()
                safe_diagonal = cp.where(cp.abs(diagonal) > 1.0e-30, diagonal, 1.0)
                preconditioner = cpx_linalg.LinearOperator(
                    gpu_matrix.shape, matvec=lambda vector: vector / safe_diagonal,
                    dtype=gpu_matrix.dtype,
                )
            if workspace_key is not None:
                self._cuda_workspaces[workspace_key] = {
                    "matrix": gpu_matrix,
                    "preconditioner": preconditioner,
                    "indices": None if is_coo else matrix.indices.copy(),
                    "indptr": None if is_coo else matrix.indptr.copy(),
                    "source_identity": id(matrix),
                }
            transfer_seconds = time.perf_counter() - transfer_started
            iterations = [0]

            def count_iteration(_):
                iterations[0] += 1

            solve_started = time.perf_counter()
            kwargs = {
                "rtol": self.settings.solver_rtol,
                "atol": 0.0,
                "maxiter": self.settings.solver_max_iterations,
                "M": preconditioner,
                "callback": count_iteration,
            }
            if str(system_kind).upper() == "SPD":
                gpu_values, status = cpx_linalg.cg(gpu_matrix, gpu_rhs, **kwargs)
            else:
                gpu_values, status = cpx_linalg.bicgstab(gpu_matrix, gpu_rhs, **kwargs)
            cp.cuda.get_current_stream().synchronize()
            solve_seconds = time.perf_counter() - solve_started
            if status != 0:
                raise RuntimeError(f"CUDA iterative solver did not converge (status={status}).")
            residual = cp.linalg.norm(gpu_matrix.dot(gpu_values) - gpu_rhs) / cp.maximum(
                cp.linalg.norm(gpu_rhs), 1.0e-30
            )
            values = cp.asnumpy(gpu_values)
            relative_residual = float(residual.get())
            if not np.all(np.isfinite(values)):
                raise RuntimeError("CUDA solution contains non-finite values.")
            return ComputeSolution(values, ComputeMetadata(
                backend="CUDA_CUPY",
                device=str(device_name),
                solve_seconds=solve_seconds,
                transfer_seconds=transfer_seconds,
                relative_residual=relative_residual,
                iterations=max(1, iterations[0]),
                cpu_threads=self._cpu_threads(),
                cache_hit=structure_matches,
                matrix_reused=matrix_reused,
                matrix_assembly=matrix_assembly,
            ))

    def clear_cache(self):
        """Release persistent CUDA sparse workspaces owned by this backend."""
        self._cuda_workspaces.clear()
