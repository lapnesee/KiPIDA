"""Secure LAN execution client for an existing Palace simulation project.

Palace consumes a user-supplied configuration and mesh.  This module therefore
transfers an explicitly selected project directory; it never invents a FEM mesh
from incomplete PCB geometry.  Authentication is delegated to OpenSSH keys or
an SSH agent and passwords are never persisted by Ki-PIDA.
"""

from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid

try:
    from .models import EMCPalaceRemoteRunResult
except (ImportError, ValueError):
    from models import EMCPalaceRemoteRunResult


WINDOWS_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_SAFE_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.:-]*[A-Za-z0-9])?$")
_SAFE_USER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SAFE_REMOTE_TOKEN = re.compile(r"^[A-Za-z0-9_./~+-]+$")


def bundled_palace_smoke_config():
    """Return the installed minimal Palace project without importing the UI."""
    return (
        Path(__file__).resolve().parent
        / "examples" / "palace-lan-minimal" / "minimal-electrostatic.json"
    )


def _quote_remote_path(path):
    """Quote a validated POSIX path while preserving an initial home expansion."""
    path = str(path)
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


@dataclass(frozen=True)
class PalaceRemoteConnection:
    host: str
    username: str = ""
    port: int = 22
    identity_file: str = ""
    remote_root: str = "~/kipida-palace"
    executable: str = "palace"
    mpi_processes: int = 1
    host_key_policy: str = "STRICT"
    connect_timeout_s: float = 10.0
    run_timeout_s: float = 3600.0
    keep_remote_files: bool = True

    @classmethod
    def from_settings(cls, settings):
        return cls(
            host=str(settings.palace_remote_host).strip(),
            username=str(settings.palace_remote_username).strip(),
            port=int(settings.palace_remote_port),
            identity_file=str(settings.palace_remote_identity_file).strip(),
            remote_root=str(settings.palace_remote_root).strip(),
            executable=str(settings.palace_remote_executable).strip(),
            mpi_processes=int(settings.palace_remote_mpi_processes),
            host_key_policy=str(settings.palace_remote_host_key_policy).strip().upper(),
            connect_timeout_s=float(settings.palace_remote_connect_timeout_s),
            run_timeout_s=float(settings.solver_timeout_s),
            keep_remote_files=bool(settings.palace_remote_keep_files),
        )

    def validate(self):
        if not self.host or not _SAFE_HOST.fullmatch(self.host):
            raise ValueError("Palace server host must be a DNS name or numeric LAN address.")
        if self.username and not _SAFE_USER.fullmatch(self.username):
            raise ValueError("Palace SSH username contains unsupported characters.")
        if not 1 <= self.port <= 65535:
            raise ValueError("Palace SSH port must be between 1 and 65535.")
        if not 1 <= self.mpi_processes <= 4096:
            raise ValueError("Palace MPI process count must be between 1 and 4096.")
        if self.connect_timeout_s <= 0 or self.run_timeout_s <= 0:
            raise ValueError("Palace connection and run timeouts must be positive.")
        if self.host_key_policy not in {"STRICT", "ACCEPT_NEW"}:
            raise ValueError("Palace host-key policy must be STRICT or ACCEPT_NEW.")
        for label, value in (("remote root", self.remote_root), ("executable", self.executable)):
            if not value or not _SAFE_REMOTE_TOKEN.fullmatch(value):
                raise ValueError(f"Palace {label} contains unsupported characters.")
        parts = PurePosixPath(self.remote_root.replace("~", "home", 1)).parts
        if ".." in parts or self.remote_root in {"/", "~", "~/"}:
            raise ValueError("Palace remote root must be a dedicated non-root directory.")
        if self.identity_file and not Path(self.identity_file).is_file():
            raise ValueError("The configured Palace SSH identity file does not exist.")
        return self


def _parse_palace_config(path):
    """Read standard JSON metadata without attempting Palace's comment extensions."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "UNKNOWN", "postpro", (
            "Palace config metadata could not be parsed locally; remote --dry-run remains authoritative."
        )
    problem = payload.get("Problem", {}) if isinstance(payload, dict) else {}
    return (
        str(problem.get("Type", "UNKNOWN")),
        str(problem.get("Output", "postpro")),
        "",
    )


class PalaceRemoteClient:
    """Upload, validate, execute and retrieve a Palace project through OpenSSH."""

    def __init__(self, connection, log_callback=None, cancellation_callback=None):
        self.connection = connection.validate()
        self.log_callback = log_callback
        self.cancellation_callback = cancellation_callback
        self.ssh_path = shutil.which("ssh")
        self.scp_path = shutil.which("scp")

    def _log(self, message):
        if self.log_callback:
            self.log_callback(message)

    @property
    def target(self):
        host = f"[{self.connection.host}]" if ":" in self.connection.host else self.connection.host
        return f"{self.connection.username}@{host}" if self.connection.username else host

    def _common_options(self):
        policy = "yes" if self.connection.host_key_policy == "STRICT" else "accept-new"
        options = [
            "-P", str(self.connection.port),
            "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={max(1, int(self.connection.connect_timeout_s))}",
            "-o", f"StrictHostKeyChecking={policy}",
        ]
        if self.connection.identity_file:
            options.extend(["-i", self.connection.identity_file])
        return options

    def _ssh_command(self, *remote_args):
        if not self.ssh_path:
            raise RuntimeError("OpenSSH client 'ssh' was not found on this computer.")
        ssh_options = self._common_options()
        ssh_options[0] = "-p"
        return [self.ssh_path, *ssh_options, self.target, "--", *map(str, remote_args)]

    def _ssh_shell_command(self, remote_command):
        """Run one complete POSIX shell expression without SSH splitting it."""
        return self._ssh_command("sh", "-lc", shlex.quote(str(remote_command)))

    def _scp_command(self, source, destination, recursive=False):
        if not self.scp_path:
            raise RuntimeError("OpenSSH client 'scp' was not found on this computer.")
        options = self._common_options()
        command = [self.scp_path, *options]
        if recursive:
            command.append("-r")
        command.extend([str(source), str(destination)])
        return command

    @staticmethod
    def _capture(command, timeout, cwd=None):
        return subprocess.run(
            [str(item) for item in command], cwd=str(cwd) if cwd else None,
            text=True, capture_output=True, timeout=timeout,
            creationflags=WINDOWS_NO_WINDOW,
        )

    def probe(self):
        """Bounded non-interactive connectivity and Palace availability check."""
        command = self._ssh_command(self.connection.executable, "--help")
        completed = self._capture(command, self.connection.connect_timeout_s + 15.0)
        output = (completed.stdout + "\n" + completed.stderr).strip()
        if completed.returncode != 0:
            raise RuntimeError(output[-1000:] or f"SSH probe failed with code {completed.returncode}.")
        if "palace" not in output.lower():
            raise RuntimeError("SSH connected, but the configured command did not identify Palace.")
        version_line = next(
            (line.strip() for line in output.splitlines() if "palace" in line.lower()),
            "Palace available",
        )
        return version_line

    def _monitor(self, command, timeout, log_path):
        started = time.monotonic()
        last_report = -5.0
        with Path(log_path).open("w", encoding="utf-8", errors="replace") as stream:
            process = subprocess.Popen(
                [str(item) for item in command], text=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=WINDOWS_NO_WINDOW,
            )

            def drain():
                try:
                    for line in process.stdout or ():
                        stream.write(line)
                        stream.flush()
                except (OSError, ValueError):
                    pass

            thread = threading.Thread(target=drain, daemon=True)
            thread.start()
            status = "COMPLETED"
            while process.poll() is None:
                elapsed = time.monotonic() - started
                if self.cancellation_callback and self.cancellation_callback():
                    process.terminate()
                    status = "CANCELLED"
                    break
                if elapsed >= timeout:
                    process.terminate()
                    status = "TIMEOUT"
                    break
                if elapsed - last_report >= 5.0:
                    last_report = elapsed
                    self._log(f"Palace remote solve running: {elapsed:.0f} s / {timeout:.0f} s")
                time.sleep(0.25)
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5.0)
            thread.join(timeout=2.0)
            if process.stdout is not None:
                process.stdout.close()
        return process.returncode if status == "COMPLETED" else None, status

    def _best_effort_remote_stop(self, pid_path):
        remote = (
            f"test -f {_quote_remote_path(pid_path)} && "
            f"kill -TERM -- $(cat {_quote_remote_path(pid_path)})"
        )
        try:
            self._capture(self._ssh_shell_command(remote), self.connection.connect_timeout_s + 5)
        except Exception:
            pass

    def run_project(self, config_path, local_output_directory):
        started = time.perf_counter()
        config_path = Path(config_path).resolve()
        if not config_path.is_file() or config_path.suffix.lower() != ".json":
            raise ValueError("Select an existing Palace JSON configuration file.")
        project_directory = config_path.parent
        local_output = Path(local_output_directory)
        local_output.mkdir(parents=True, exist_ok=True)
        problem_type, configured_output, metadata_warning = _parse_palace_config(config_path)
        result = EMCPalaceRemoteRunResult(
            status="PREPARING", server=self.target, config_path=str(config_path),
            problem_type=problem_type, local_artifact_directory=str(local_output),
            output_directory=configured_output,
        )
        if metadata_warning:
            result.warnings.append(metadata_warning)
        result.palace_version = self.probe()
        job_id = f"kipida-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        remote_job = self.connection.remote_root.rstrip("/") + "/" + job_id
        remote_input = remote_job + "/input"
        remote_pid = remote_job + "/palace.pid"
        result.remote_job_directory = remote_job
        self._log(f"Palace server ready: {result.palace_version}")
        mkdir = self._capture(
            self._ssh_command("mkdir", "-p", "--", remote_job),
            self.connection.connect_timeout_s + 15,
        )
        if mkdir.returncode != 0:
            raise RuntimeError((mkdir.stderr or mkdir.stdout).strip() or "Could not create Palace job directory.")
        self._log(f"Uploading the explicit Palace project directory to {self.target}...")
        upload = self._capture(
            self._scp_command(project_directory, f"{self.target}:{remote_input}", recursive=True),
            max(60.0, self.connection.run_timeout_s),
        )
        if upload.returncode != 0:
            raise RuntimeError((upload.stderr or upload.stdout).strip() or "Palace project upload failed.")
        quoted_cd = _quote_remote_path(remote_input)
        quoted_executable = shlex.quote(self.connection.executable)
        quoted_config = shlex.quote(config_path.name)
        dry_remote_command = (
            f"cd {quoted_cd} && "
            f"exec {quoted_executable} -serial --dry-run {quoted_config}"
        )
        dry_command = self._ssh_shell_command(dry_remote_command)
        dry = self._capture(dry_command, min(max(30.0, self.connection.run_timeout_s), 300.0))
        (local_output / "palace-dry-run.log").write_text(
            dry.stdout + dry.stderr, encoding="utf-8", errors="replace",
        )
        if dry.returncode != 0:
            result.status = "VALIDATION_FAILED"
            result.warnings.append("Palace rejected the project during remote --dry-run validation.")
            result.elapsed_seconds = time.perf_counter() - started
            return result
        result.dry_run_passed = True
        quoted_pid = _quote_remote_path(remote_pid)
        remote_command = (
            f"cd {quoted_cd} && printf '%s\\n' $$ > {quoted_pid} && "
            f"exec {quoted_executable} -np {self.connection.mpi_processes} {quoted_config}"
        )
        self._log(
            f"Launching Palace {problem_type} analysis with "
            f"{self.connection.mpi_processes} MPI process(es)..."
        )
        return_code, status = self._monitor(
            self._ssh_shell_command(remote_command),
            self.connection.run_timeout_s, local_output / "palace-run.log",
        )
        result.return_code = return_code
        if status in {"CANCELLED", "TIMEOUT"}:
            self._best_effort_remote_stop(remote_pid)
            result.status = status
            result.warnings.append(
                "Remote Palace termination was requested; verify the server if MPI workers remain active."
            )
        elif return_code == 0:
            result.status = "SOLVED_REMOTE"
        else:
            result.status = "FAILED"
            result.warnings.append(f"Palace returned exit code {return_code}.")
            if return_code in {9, 137}:
                result.warnings.append(
                    "The Palace process was killed by the remote operating system; this "
                    "usually indicates peak-memory exhaustion. Use a coarser mesh or raise "
                    "the server/cgroup memory limit."
                )
        artifact_root = local_output / "project"
        download = self._capture(
            self._scp_command(f"{self.target}:{remote_input}", artifact_root, recursive=True),
            max(60.0, self.connection.run_timeout_s),
        )
        if download.returncode != 0:
            result.warnings.append(
                "Palace finished, but remote artifacts could not be retrieved: "
                + ((download.stderr or download.stdout).strip()[-500:])
            )
        if artifact_root.exists():
            result.csv_files = sorted(
                str(path.relative_to(local_output)) for path in artifact_root.rglob("*.csv")
            )
            resolved = next(artifact_root.rglob("*_resolved.json"), None)
            if resolved:
                result.resolved_config_path = str(resolved)
        if not self.connection.keep_remote_files:
            cleanup = f"rm -rf -- {_quote_remote_path(remote_job)}"
            self._capture(
                self._ssh_shell_command(cleanup),
                self.connection.connect_timeout_s + 10,
            )
        result.elapsed_seconds = time.perf_counter() - started
        return result
