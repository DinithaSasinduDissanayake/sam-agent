"""sam.plat - tiny OS abstraction layer (POSIX + Windows).

NOTE: this package must never be named ``platform``: SAM command modules put
the ``sam/`` directory itself on sys.path, so ``sam/platform`` would shadow the
standard library module ``platform``.

Everything here is safe to import on both OSes. Windows-only code lives in
``sam.plat.windows`` and is imported lazily.
"""

import os
import socket
import subprocess
import sys
import time

IS_WINDOWS = os.name == "nt"

#: Names Windows reserves as devices; they cannot be used as file/dir names.
_RESERVED = frozenset(
    ["con", "prn", "aux", "nul"]
    + ["com%d" % i for i in range(1, 10)]
    + ["lpt%d" % i for i in range(1, 10)])


def hostname():
    """Short host name recorded in every registry entry and result.json."""
    try:
        return socket.gethostname() or "unknown"
    except OSError:
        return "unknown"


def is_reserved_name(name):
    """True when ``name`` cannot be a file or directory name on this OS."""
    return IS_WINDOWS and str(name).lower() in _RESERVED


def replace(src, dst):
    """os.replace with a retry loop on Windows.

    On Windows os.replace raises PermissionError while any other process has
    the destination open (even for reading). POSIX: plain os.replace.
    """
    if not IS_WINDOWS:
        os.replace(src, dst)
        return
    for _ in range(100):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            time.sleep(0.02)
    os.replace(src, dst)


def read_text(path, encoding="utf-8"):
    """Read a small text file; on Windows retry while a writer replaces it."""
    if not IS_WINDOWS:
        with open(path, "r", encoding=encoding) as f:
            return f.read()
    last = None
    for _ in range(100):
        try:
            with open(path, "r", encoding=encoding) as f:
                return f.read()
        except PermissionError as e:
            last = e
            time.sleep(0.02)
    raise last


def fsync_dir(path):
    """fsync a directory (POSIX durability). No-op on Windows (not possible)."""
    if IS_WINDOWS:
        return
    dir_fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def fix_child_env(env, cwd):
    """Environment fix-ups applied to every launched wrapper/harness.

    PWD: some harnesses (opencode) trust an inherited $PWD over the real
    working directory, so it must always name the child's cwd.
    """
    env.pop("OLDPWD", None)
    if cwd:
        env["PWD"] = str(cwd)
    if IS_WINDOWS:
        env["PYTHONUTF8"] = "1"
        env["MSYS_NO_PATHCONV"] = "1"
    return env


def popen_detached(argv, cwd, env):
    """Start a wrapper/runner so that it survives the calling process.

    POSIX: new session (the child becomes a process-group leader; its pid is
    the pgid). Windows: no console window, new process group, and break away
    from the caller's Job Object when that is allowed.
    """
    if not IS_WINDOWS:
        return subprocess.Popen(
            argv, cwd=cwd, env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    base = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    last = None
    for flags in (base | 0x01000000, base):  # try CREATE_BREAKAWAY_FROM_JOB first
        try:
            return subprocess.Popen(
                argv, cwd=cwd, env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
                close_fds=True,
            )
        except PermissionError as e:  # job forbids breakaway
            last = e
            if flags != base:
                # The caller sits in a Job Object that forbids breakaway. If that
                # job is kill-on-close the agent would die with the caller, so
                # start the runner through WMI: the new process is a child of the
                # WMI host and belongs to no caller job.
                try:
                    return _popen_via_wmi(argv, cwd, env)
                except Exception:
                    pass
    raise last


class DetachedProcess:
    """Minimal stand-in for subprocess.Popen when the runner was started by WMI."""

    def __init__(self, pid):
        self.pid = int(pid)
        self.returncode = None


def _popen_via_wmi(argv, cwd, env):
    """Windows only. The WMI host does not pass our environment on, so it is
    handed over in a private file that the runner loads and deletes
    (``--env-file``, must directly follow the runner script path)."""
    import json
    import tempfile
    # User-private temp dir (never the agent workspace: the file holds the
    # whole environment, API keys included, until the runner deletes it).
    fd, env_path = tempfile.mkstemp(prefix=".runner-env-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(env, f)
    full = list(argv[:2]) + ["--env-file", env_path] + list(argv[2:])
    script = ("$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
              "-Arguments @{CommandLine=$env:SAM_WMI_CMD; CurrentDirectory=$env:SAM_WMI_CWD}; "
              "Write-Output \"$($r.ReturnValue) $($r.ProcessId)\"")
    cp = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=60,
        env=dict(os.environ, SAM_WMI_CMD=subprocess.list2cmdline(full),
                 SAM_WMI_CWD=str(cwd or os.getcwd())))
    parts = cp.stdout.split()
    if cp.returncode != 0 or len(parts) < 2 or parts[0] != "0":
        try:
            os.unlink(env_path)
        except OSError:
            pass
        raise OSError("WMI process create failed: %s %s" % (cp.stdout.strip(), cp.stderr.strip()[:200]))
    return DetachedProcess(parts[1])


def python_argv():
    """argv prefix that runs a Python script with the current interpreter."""
    return [sys.executable]
