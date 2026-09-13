"""Record what the launching application says about itself. Decide nothing.

Whether the Zeo multi-board layer may activate depends on which application
launched the plugin (docs/plan-universalite.md, section 3). None of the obvious
signals can say so:

* Zeo answers ``get_version()`` as "KiCad 10.0.3", exactly like KiCad;
* Zeo shares KiCad 10's plugin directory and Python environment, so the
  plugin's own path says nothing;
* that environment was created by whichever host ran the plugin first, so
  ``sys.executable`` names the creator of the venv, not the launcher.

The candidates that remain -- the host binary directory reported over IPC, the
chain of parent processes, and the environment the host sets -- have never been
observed from inside either host. This module observes them and logs them, so
the detection rule is chosen from evidence rather than written from a guess.

Only the *names* of environment variables are recorded: ``KICAD_API_TOKEN``
authenticates the IPC session and must not reach a log file.
"""

import os
import subprocess
import sys

_ENV_PREFIXES = ("KICAD", "ZEO")
_MAX_ANCESTRY = 5


def host_environment_variable_names(environ=None):
    """Names of KICAD*/ZEO* variables in the environment, never their values."""
    environ = os.environ if environ is None else environ
    return sorted(name for name in environ if name.upper().startswith(_ENV_PREFIXES))


def process_ancestry(max_depth=_MAX_ANCESTRY):
    """Executables of the processes above this one, nearest parent first.

    An empty list means the platform offered no way to walk the chain, not that
    the plugin has no parent. On Windows a venv interpreter is itself a
    launcher that starts the base interpreter, so the host is typically two or
    three levels up rather than the immediate parent.
    """
    try:
        import psutil  # optional; not a dependency of the plugin
    except ImportError:
        psutil = None
    try:
        if psutil is not None:
            return _psutil_ancestry(psutil, max_depth)
        if sys.platform == "win32":
            return _windows_ancestry(max_depth)
        if sys.platform.startswith("linux"):
            return _proc_ancestry(max_depth)
        return _ps_ancestry(max_depth)
    except Exception:  # diagnostics must never block startup
        return []


def describe_host(client=None, board=None, project=None, environ=None, ancestry=None):
    """Lines describing the launching host, each safe to write to a log."""
    lines = []

    version = _call(client, "get_version")
    lines.append(f"IPC version: {_version_text(version)}")
    api_version = _call(client, "get_api_version")
    lines.append(f"IPC API version: {_version_text(api_version)}")
    lines.append(f"Host kicad-cli: {_call(client, 'get_kicad_binary_path', 'kicad-cli') or 'unavailable'}")

    project_name = getattr(project, "name", None)
    project_path = getattr(project, "path", None)
    lines.append(f"Project: name={project_name or 'unavailable'} path={project_path or 'unavailable'}")

    document = getattr(board, "document", None) if board is not None else None
    doc_project = getattr(document, "project", None)
    lines.append(
        "Board document: "
        f"board_filename={getattr(document, 'board_filename', '') or 'unavailable'} "
        f"project.name={getattr(doc_project, 'name', '') or 'unavailable'} "
        f"project.path={getattr(doc_project, 'path', '') or 'unavailable'}"
    )

    chain = process_ancestry() if ancestry is None else ancestry
    lines.append("Process ancestry: " + (" <- ".join(chain) if chain else "unavailable"))

    names = host_environment_variable_names(environ)
    lines.append("Environment variables (names only): " + (", ".join(names) if names else "none"))

    lines.append(f"Plugin interpreter: {sys.executable} (base {getattr(sys, 'base_prefix', '')})")
    return lines


def append_host_report(lines, path=None):
    """Append one timestamped block to a log that survives relaunches.

    ``kipida_entry.log`` is rewritten on every start, so comparing the host's
    answers across the launches the detection rule needs -- KiCad, Zeo from a
    container, Zeo on a board opened directly, Zeo on a standalone project --
    would mean copying it after each one. This file accumulates them instead.
    Returns the path written, or None if it could not be.
    """
    import datetime
    import tempfile

    path = path or os.path.join(tempfile.gettempdir(), "kipida_host_probe.log")
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"=== launch {stamp} ===\n")
            handle.writelines(f"{line}\n" for line in lines)
        return path
    except OSError:
        return None


def _call(obj, method, *args):
    function = getattr(obj, method, None)
    if function is None:
        return None
    try:
        return function(*args)
    except Exception:
        return None


def _version_text(version):
    if version is None:
        return "unavailable"
    return str(getattr(version, "full_version", "") or version)


def _psutil_ancestry(psutil, max_depth):
    chain = []
    for parent in psutil.Process().parents()[:max_depth]:
        try:
            chain.append(parent.exe() or parent.name())
        except Exception:
            chain.append(f"pid {parent.pid}")
    return chain


def _windows_ancestry(max_depth):
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    ]

    snapshot_processes = 0x00000002
    query_limited_information = 0x1000
    snapshot = kernel32.CreateToolhelp32Snapshot(snapshot_processes, 0)
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return []
    processes = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            processes[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile)
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)

    def image_path(pid):
        handle = kernel32.OpenProcess(query_limited_information, False, pid)
        if not handle:
            return None
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return buffer.value
            return None
        finally:
            kernel32.CloseHandle(handle)

    chain, seen, pid = [], {os.getpid()}, os.getpid()
    for _ in range(max_depth):
        parent = processes.get(pid, (0, ""))[0]
        if not parent or parent in seen or parent not in processes:
            break
        seen.add(parent)
        chain.append(image_path(parent) or processes[parent][1])
        pid = parent
    return chain


def _proc_ancestry(max_depth):
    chain, pid = [], os.getpid()
    for _ in range(max_depth):
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as handle:
            # The command name is parenthesised and may contain spaces, so split
            # after its closing parenthesis: the parent pid is the second field.
            parent = int(handle.read().rsplit(")", 1)[1].split()[1])
        if parent <= 1:
            break
        try:
            chain.append(os.readlink(f"/proc/{parent}/exe"))
        except OSError:
            with open(f"/proc/{parent}/comm", "r", encoding="utf-8") as handle:
                chain.append(handle.read().strip())
        pid = parent
    return chain


def _ps_ancestry(max_depth):
    chain, pid = [], os.getpid()
    for _ in range(max_depth):
        out = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        if not out or int(out) <= 1:
            break
        parent = int(out)
        command = subprocess.run(
            ["ps", "-o", "comm=", "-p", str(parent)], capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        chain.append(command or f"pid {parent}")
        pid = parent
    return chain
