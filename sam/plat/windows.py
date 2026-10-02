"""sam.plat.windows - Windows implementations of the sam.proc / sam.locks primitives.

Import only on Windows (needs psutil + ctypes/kernel32).

Model:
- process identity   = (pid, create time in ms)      [POSIX: /proc starttime]
- "process group id" = pid of the runner process     [POSIX: pgid]
- the runner puts itself into a named Job Object ``sam-job-<runner pid>`` with
  KILL_ON_JOB_CLOSE, so every descendant belongs to the job and dies with it.
- there is no graceful signal: every kill is a hard kill.
"""

import os
import re
import shutil
import subprocess
import time

import psutil

from sam.plat import winjob


# ── identity / liveness ──────────────────────────────────────────────────────

def proc_alive(pid):
    """True when a process with this pid exists. NEVER uses os.kill(pid, 0):
    on Windows signal 0 is CTRL_C_EVENT."""
    try:
        return bool(pid) and int(pid) > 0 and psutil.pid_exists(int(pid))
    except (TypeError, ValueError, psutil.Error):
        return False


def read_pid_start_time(pid):
    """Process create time in integer milliseconds, or None."""
    try:
        return int(round(psutil.Process(int(pid)).create_time() * 1000))
    except (TypeError, ValueError, psutil.Error, OSError):
        return None


def pid_is_zombie(pid):
    """Windows has no zombies."""
    return False


def pgid_of(pid):
    """The group id of a runner is its own pid (None when it is gone)."""
    return int(pid) if proc_alive(pid) else None


# ── job / tree ───────────────────────────────────────────────────────────────

def job_name(runner_pid):
    return "sam-job-%d" % int(runner_pid)


def enter_own_job():
    """Called by the runner at start: create the named kill-on-close job and
    join it. Returns the job handle (keep it open for the process lifetime)
    or None when the OS refuses (the caller then relies on tree kill)."""
    try:
        h = winjob.create(job_name(os.getpid()), kill_on_close=True)
        winjob.assign_self(h)
        return h
    except OSError:
        return None


def _tree_pids(root_pid):
    try:
        root = psutil.Process(int(root_pid))
        return [root] + root.children(recursive=True)
    except psutil.Error:
        return []


def group_pids(pgid):
    """Pids in the runner's job; falls back to the live parent/child tree."""
    try:
        h = winjob.open_job(job_name(pgid))
    except OSError:
        return [p.pid for p in _tree_pids(pgid)]
    try:
        return winjob.pids(h)
    except OSError:
        return [p.pid for p in _tree_pids(pgid)]
    finally:
        winjob.close(h)


def killpg(pgid, sig=None):
    """Hard-kill the whole group of runner ``pgid``. ``sig`` is ignored."""
    if not pgid:
        return
    try:
        h = winjob.open_job(job_name(pgid))
    except OSError:
        h = None
    if h is not None:
        try:
            winjob.terminate(h, 137)
            return
        except OSError:
            pass
        finally:
            winjob.close(h)
    # Fallback: parent/child walk (misses orphans whose parent already exited).
    procs = _tree_pids(pgid)
    for p in reversed(procs):
        try:
            p.kill()
        except psutil.Error:
            pass
    subprocess.run(["taskkill", "/PID", str(int(pgid)), "/T", "/F"],
                   capture_output=True)


def kill_process_group(pgid, sigterm_timeout=5):
    """Kill and confirm. Returns True when no member is left."""
    killpg(pgid)
    deadline = time.monotonic() + max(1.0, float(sigterm_timeout))
    while time.monotonic() < deadline:
        if not proc_alive(pgid):
            return True
        time.sleep(0.2)
    return not proc_alive(pgid)


# ── resource sampling (CPU/IO "is it moving") ────────────────────────────────

def _sample_one(p):
    try:
        with p.oneshot():
            ct = p.cpu_times()
            try:
                io = p.io_counters()
                rd, wr = io.read_bytes, io.write_bytes
            except (psutil.Error, OSError, AttributeError):
                rd, wr = 0, 0
            return {"starttime": int(round(p.create_time() * 1000)),
                    "cpu": int((ct.user + ct.system) * 100),
                    "io_read": int(rd), "io_write": int(wr)}
    except (psutil.Error, OSError):
        return None


def read_proc_resource(pid):
    if pid is None:
        return None
    try:
        s = _sample_one(psutil.Process(int(pid)))
    except (psutil.Error, TypeError, ValueError):
        return None
    if s is None:
        return None
    return {"state": "R", "cpu_ticks": s["cpu"],
            "io_read_bytes": s["io_read"], "io_write_bytes": s["io_write"]}


def sample_process_group(pid, pgid=None):
    """Same shape as sam.proc.sample_process_group: str(pid) -> counters."""
    root = pgid or pid
    if root is None:
        return None
    samples = {}
    for member in group_pids(root):
        try:
            s = _sample_one(psutil.Process(member))
        except psutil.Error:
            s = None
        if s is not None:
            samples[str(member)] = s
    return samples or None


# ── file locks ───────────────────────────────────────────────────────────────

def lock_fd(fd, exclusive=True):
    """Non-blocking lock attempt on an open fd. True = acquired."""
    return winjob.lockfileex(fd, exclusive=exclusive, blocking=False)


def unlock_fd(fd):
    winjob.unlockfileex(fd)


def with_spawn_state(fn, timeout_s, lock_path, load_state, save_state):
    """Windows body of sam.proc._with_spawn_state (same contract)."""
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    except OSError:
        return False, None
    try:
        deadline = time.monotonic() + timeout_s
        acquired = False
        while time.monotonic() < deadline:
            try:
                if lock_fd(fd, True):
                    acquired = True
                    break
            except OSError:
                return False, None
            time.sleep(0.02)
        if not acquired:
            return False, None
        try:
            state = load_state()
            save, result = fn(state)
            if save:
                save_state(state)
            return True, result
        finally:
            try:
                unlock_fd(fd)
            except OSError:
                pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


# ── executables ──────────────────────────────────────────────────────────────

def resolve_executable(name):
    """argv prefix for a harness CLI, or None when it is not installed.

    npm installs ``<name>.cmd`` shims. A .cmd goes through cmd.exe, which
    truncates arguments at the first newline and expands %VAR%, so the shim is
    unwrapped to the real ``.exe`` or to ``node <script.js>``.
    """
    found = shutil.which(name)
    if not found:
        return None
    if not found.lower().endswith((".cmd", ".bat")):
        return [found]
    try:
        with open(found, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return [found]
    here = os.path.dirname(found)
    m = re.search(r'"%dp0%\\([^"]+?\.exe)"\s+%\*', text, re.I)
    if m:
        return [os.path.join(here, m.group(1))]
    m = re.search(r'"%dp0%\\([^"]+?\.(?:js|cjs|mjs))"\s+%\*', text, re.I)
    if m:
        node = os.path.join(here, "node.exe")
        if not os.path.exists(node):
            node = shutil.which("node")
        if node:
            return [node, os.path.join(here, m.group(1))]
    return [found]
