#!/usr/bin/env python3
"""SAM proc module: PID helpers — alive, start_time, killpg, pgid_of —
plus the global spawn rate limiter (launch-storm prevention).

Rate limiter spec (IISA failure review, rounds 1–4): the 06:57 burst was 4
spawns in 0.257 s from one nested spawner while 3–4 agents were already
running. Steady-state headcount (5–6) proved fine; spawn *rate* killed.
So: global ≥15 s spacing between launches + max 4 running, enforced in
code (skill prose is unenforceable on nested spawners). Quota/auth
endpoints are account-global, hence the limiter is global, not per-parent.
"""

import fcntl
import json
import os
import signal
import time

from sam import config as sam_config


def proc_alive(pid):
    """Check if a PID is alive. Returns bool.
    Line 1: Try os.kill(pid, 0). Return True.
    Line 2: If ProcessLookupError, return False.
    Line 3: If PermissionError, return True (process exists but not ours, assume alive).
    """
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def read_pid_start_time(pid):
    """Read starttime field 19 from /proc/<pid>/stat.
    Returns int or None.
    Line 1: Try to read /proc/{pid}/stat as text.
    Line 2: If FileNotFoundError or PermissionError, return None.
    Line 3: Find first ( after PID, find last ) ending comm.
    Line 4: Extract substring after ). Split on spaces.
    Line 5: Fields after comm: state(0), ppid(1), pgrp(2), ..., starttime(19).
    Line 6: Extract field 19 (0-indexed) from the split list.
    Line 7: Return int(starttime).
    """
    try:
        with open(f"/proc/{pid}/stat", "r") as f:
            data = f.read()
    except (FileNotFoundError, PermissionError):
        return None

    # Find first '(' and last ')' to handle comm field with spaces
    first_paren = data.find("(")
    last_paren = data.rfind(")")
    if first_paren == -1 or last_paren == -1 or last_paren <= first_paren:
        return None

    after_comm = data[last_paren + 1:].strip()
    fields = after_comm.split()
    # starttime is field 19 (0-indexed) after comm
    if len(fields) < 20:
        return None
    try:
        return int(fields[19])
    except (ValueError, IndexError):
        return None


def proc_start_time_match(pid, stored_start_time):
    """Check if PID's current start time matches stored value.
    Line 1: current = read_pid_start_time(pid).
    Line 2: If current is None or stored_start_time is None, return False.
    Line 3: Return current == stored_start_time.
    """
    current = read_pid_start_time(pid)
    if current is None or stored_start_time is None:
        return False
    return current == stored_start_time


def killpg(pgid, sig):
    """Send signal to process group.
    Line 1: Try os.killpg(pgid, sig).
    Line 2: If ProcessLookupError, pass (group already gone).
    Line 3: If PermissionError, raise.
    """
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        raise


def pgid_of(pid):
    """Read process group ID from /proc/<pid>/stat.
    Returns int or None.
    Line 1: Try to read /proc/{pid}/stat as text.
    Line 2: If FileNotFoundError or PermissionError, return None.
    Line 3: Find first ( after PID, find last ) ending comm.
    Line 4: Extract substring after ). Split on spaces.
    Line 5: Extract field 2 (0-indexed) from the split list (pgrp).
    Line 6: Return int(pgrp).
    """
    try:
        with open(f"/proc/{pid}/stat", "r") as f:
            data = f.read()
    except (FileNotFoundError, PermissionError):
        return None

    first_paren = data.find("(")
    last_paren = data.rfind(")")
    if first_paren == -1 or last_paren == -1 or last_paren <= first_paren:
        return None

    after_comm = data[last_paren + 1:].strip()
    fields = after_comm.split()
    # pgrp is field 2 (0-indexed) after comm
    if len(fields) < 3:
        return None
    try:
        return int(fields[2])
    except (ValueError, IndexError):
        return None


def _pid_is_zombie(pid):
    """Return True if pid exists as a zombie (terminated, awaiting reap)."""
    try:
        with open(f"/proc/{pid}/stat", "r") as f:
            data = f.read()
    except (FileNotFoundError, PermissionError):
        return False
    lp = data.rfind(")")
    if lp == -1:
        return False
    parts = data[lp + 1:].strip().split()
    return bool(parts) and parts[0] == "Z"


def read_proc_resource(pid):
    """Single /proc sample: process state, CPU ticks, IO byte counters.

    Returns None when /proc/<pid>/stat cannot be read (gone/permission);
    otherwise a dict whose individual values may be None (e.g.
    /proc/<pid>/io is unreadable for some processes).
    cpu_ticks = utime + stime (fields 14+15 of /proc/<pid>/stat).
    """
    if pid is None:
        return None
    try:
        with open(f"/proc/{pid}/stat", "r") as f:
            data = f.read()
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return None
    lp = data.rfind(")")
    if lp == -1:
        return None
    fields = data[lp + 1:].strip().split()
    # after comm: state(0), pgrp(1), session(2), ... utime(11), stime(12)
    state = fields[0] if fields else None
    cpu_ticks = None
    if len(fields) >= 13:
        try:
            cpu_ticks = int(fields[11]) + int(fields[12])
        except ValueError:
            cpu_ticks = None
    io_read = io_write = None
    try:
        with open(f"/proc/{pid}/io", "r") as f:
            for line in f:
                if line.startswith("read_bytes:"):
                    io_read = int(line.split()[1])
                elif line.startswith("write_bytes:"):
                    io_write = int(line.split()[1])
                if io_read is not None and io_write is not None:
                    break
    except (OSError, ValueError, IndexError):
        pass
    return {"state": state, "cpu_ticks": cpu_ticks,
            "io_read_bytes": io_read, "io_write_bytes": io_write}


def proc_liveness(pid, stored_start_time=None, stored_pgid=None):
    """Item-7 tier-1 proc check: pgid + start-time identity of a live pid.

    ok = pid alive AND not a zombie AND start time matches the value
    stored at spawn AND (when a pgid was recorded) the process still
    belongs to that group. Pure read; never raises.
    """
    out = {"pid": pid, "alive": False, "zombie": False,
           "start_time_match": False, "pgid_match": None, "ok": False,
           "reason": None}
    if pid is None:
        out["reason"] = "no pid recorded"
        return out
    if not proc_alive(pid):
        out["reason"] = "pid dead"
        return out
    out["alive"] = True
    if _pid_is_zombie(pid):
        out["zombie"] = True
        out["reason"] = "pid is a zombie"
        return out
    current_start = read_pid_start_time(pid)
    if (current_start is None or stored_start_time is None
            or current_start != stored_start_time):
        out["reason"] = ("start-time mismatch (stored %r, current %r)"
                         % (stored_start_time, current_start))
        return out
    out["start_time_match"] = True
    if stored_pgid is not None:
        current_pgid = pgid_of(pid)
        out["pgid_match"] = current_pgid == stored_pgid
        if not out["pgid_match"]:
            out["reason"] = ("pgid mismatch (stored %r, current %r)"
                             % (stored_pgid, current_pgid))
            return out
    out["ok"] = True
    out["reason"] = "alive"
    return out


def resource_delta(pid, interval_seconds, sleep_fn=None):
    """Item-7 tier-2: two-sample CPU/IO delta over interval_seconds.

    Returns None when either /proc sample is unavailable, else
    {"interval_seconds", "cpu_ticks", "io_read_bytes", "io_write_bytes",
    "moving"} where moving is True when any counter advanced. Counter
    values that were unreadable in both samples count as 0.
    """
    if sleep_fn is None:
        sleep_fn = time.sleep
    a = read_proc_resource(pid)
    if interval_seconds and interval_seconds > 0:
        sleep_fn(interval_seconds)
    b = read_proc_resource(pid)
    if a is None or b is None:
        return None

    def _d(key):
        va, vb = a.get(key), b.get(key)
        if va is None or vb is None:
            return 0
        return vb - va

    cpu = _d("cpu_ticks")
    rd = _d("io_read_bytes")
    wr = _d("io_write_bytes")
    return {"interval_seconds": interval_seconds, "cpu_ticks": cpu,
            "io_read_bytes": rd, "io_write_bytes": wr,
            "moving": bool(cpu > 0 or rd > 0 or wr > 0)}


def kill_process_group(pgid, sigterm_timeout=5):
    """Send SIGTERM to PGID, poll, escalate to SIGKILL if needed.

    Returns True if the process group is confirmed dead, False otherwise.
    This is the shared helper used by kill, wait, and other modules.
    """
    # Phase 1: SIGTERM
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True  # Already gone
    except PermissionError:
        raise

    # Phase 2: Poll for death
    deadline = time.monotonic() + sigterm_timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pgid, 0)
            if _pid_is_zombie(pgid):
                return True  # terminated, awaiting parent reap
            time.sleep(0.2)
        except ProcessLookupError:
            return True  # Confirmed dead
        except PermissionError:
            # Still alive (process exists but not ours)
            time.sleep(0.2)

    # Phase 3: SIGKILL escalation
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except PermissionError:
        raise

    # Phase 4: Brief wait after SIGKILL
    time.sleep(1.0)
    try:
        os.kill(pgid, 0)
        if _pid_is_zombie(pgid):
            return True
        return False  # Still alive (unlikely)
    except (ProcessLookupError, PermissionError):
        return True


# ---------------------------------------------------------------------------
# Global spawn rate limiter
# ---------------------------------------------------------------------------

#: Minimum seconds between two launches (any parent, any model/harness).
SPAWN_SPACING_S = 15
#: Max agents counted as running before new spawns defer.
MAX_RUNNING = 4
#: Upper bound for the in-CLI slot wait. Past this, spawn returns a
#: deferral (instruction-formatted) instead of blocking forever.
SLOT_WAIT_S = 45
#: Identical (name, task) re-requests inside this window collapse into the
#: previous deferral instead of consuming slot attempts (retry-spam guard).
DUPLICATE_WINDOW_S = 120

_SPAWN_STATE_FILE = ".spawn_state.json"
_SPAWN_LOCK_FILE = "spawn.lock"


def _spawn_state_path():
    return sam_config.get_sam_home() / _SPAWN_STATE_FILE


def _spawn_lock_path():
    return sam_config.locks_dir() / _SPAWN_LOCK_FILE


def _load_spawn_state():
    try:
        with open(_spawn_state_path(), "r") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, PermissionError, ValueError):
        pass
    return {}


def _save_spawn_state(state):
    path = _spawn_state_path()
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(state, f)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        pass  # Best-effort: a missing state file only disables spacing


def count_running_agents(agents):
    """Count registry entries that are genuinely running.

    An entry counts when state == "running" AND its pid is alive, not a
    zombie, and (when recorded) its start time still matches — i.e. the
    same semantics as resolve_agent_state without importing sam.state
    (which would be a circular import).
    """
    n = 0
    for a in agents or []:
        if a.get("state") != "running":
            continue
        pid = a.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            continue
        if not proc_alive(pid):
            continue
        if _pid_is_zombie(pid):
            continue
        stored = a.get("pid_start_time")
        if stored is not None and not proc_start_time_match(pid, stored):
            continue  # PID reused by an unrelated process
        n += 1
    return n


def _slot_status(now, state, running):
    """Return (granted, retry_after_s, reason) for current conditions."""
    if running >= MAX_RUNNING:
        return False, SLOT_WAIT_S, f"{running} agents already running (cap {MAX_RUNNING})"
    last = state.get("last_spawn")
    if isinstance(last, (int, float)):
        elapsed = now - last
        if elapsed < SPAWN_SPACING_S:
            return False, (SPAWN_SPACING_S - elapsed), (
                f"last launch {elapsed:.1f}s ago (min spacing {SPAWN_SPACING_S}s)")
    return True, 0.0, "slot available"


def _with_spawn_state(fn):
    """Run fn(state) under the spawn lock; save state if fn returns True.

    The lock is held only for the (fast) read-modify-write critical
    section — never across sleeps — so concurrent spawners queue on the
    lock briefly instead of hanging behind a 45 s holder.
    Returns (True, fn_result), or (False, None) when the lock is unusable
    (callers fail open: spacing is advisory, launches are not).
    """
    lock_path = _spawn_lock_path()
    try:
        lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return False, None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            return False, None
        try:
            state = _load_spawn_state()
            save, result = fn(state)
            if save:
                _save_spawn_state(state)
            return True, result
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _prune_recent(state, now):
    recent = state.get("recent") or []
    state["recent"] = [r for r in recent
                       if isinstance(r, dict)
                       and now - r.get("ts", 0) < DUPLICATE_WINDOW_S]
    return state["recent"]


def _record_request(state, name, task, model, granted,
                    retry_after_s=0.0, reason=""):
    recent = _prune_recent(state, time.time())
    recent.insert(0, {"name": name, "task": str(task), "model": model,
                      "ts": time.time(), "granted": granted,
                      "retry_after_s": retry_after_s, "reason": reason})
    state["recent"] = recent[:20]


def acquire_spawn_slot(name, task, model, no_space=False,
                       wait_s=SLOT_WAIT_S, running=0, count_running=None):
    """Acquire a global launch slot. Returns a result dict.

    - no_space=True: experiment-only bypass (strace canaries). The launch
      is still timestamped so `doctor --window` can see the herd it
      permitted. Never persistable via config; per-invocation only.
    - Otherwise: blocks up to wait_s for spacing/capacity, then either
      grants or returns a deferral the caller must surface verbatim
      (instruction-formatted for LLM parents).
    - Identical (name, task) requests inside DUPLICATE_WINDOW_S collapse
      into the previous deferral (retry-spam guard).

    `running` is the caller-counted number of live agents; `count_running`
    is an optional zero-arg callable re-polled during the wait so the cap
    sees fresh completions. Returns keys: granted, waited_s,
    retry_after_s, reason, duplicate_suppressed, bypassed.
    """
    start = time.time()

    def _fail_open(reason):
        return {"granted": True, "waited_s": 0.0, "retry_after_s": 0.0,
                "reason": reason, "duplicate_suppressed": False,
                "bypassed": False}

    if no_space:
        def _do_bypass(state):
            state["last_spawn"] = time.time()
            _record_request(state, name, task, model, True,
                            reason="spacing bypassed (--no-space)")
            return True, None
        locked, _ = _with_spawn_state(_do_bypass)
        if not locked:
            return _fail_open("spawn lock unavailable; fail-open")
        return {"granted": True, "waited_s": 0.0, "retry_after_s": 0.0,
                "reason": "spacing bypassed (--no-space experiment flag)",
                "duplicate_suppressed": False, "bypassed": True}

    # Retry-spam guard: identical request recently deferred?
    # Inner result is always a (proceed, info) pair nested inside the
    # outer (save, result) protocol.
    def _check_duplicate(state):
        for r in _prune_recent(state, time.time()):
            if (r.get("name") == name and r.get("task") == str(task)
                    and not r.get("granted", True)):
                return False, (False, {
                    "retry_after_s": r.get("retry_after_s", SPAWN_SPACING_S),
                    "reason": r.get("reason", "slot unavailable")})
        return True, (True, None)
    locked, dup = _with_spawn_state(_check_duplicate)
    if not locked:
        return _fail_open("spawn lock unavailable; fail-open")
    proceed, dup_info = dup
    if not proceed:
        return {"granted": False, "waited_s": 0.0,
                "retry_after_s": dup_info["retry_after_s"],
                "reason": dup_info["reason"],
                "duplicate_suppressed": True, "bypassed": False}

    def _live_running():
        if count_running is not None:
            try:
                return int(count_running())
            except Exception:
                pass
        return running

    deadline = start + max(0.0, wait_s)
    live = _live_running()

    def _try_grant(state):
        granted, retry_after, reason = _slot_status(time.time(), state, live)
        if granted:
            state["last_spawn"] = time.time()
            _record_request(state, name, task, model, True)
            return True, (True, 0.0, reason)
        return False, (False, retry_after, reason)

    locked, decision = _with_spawn_state(_try_grant)
    if not locked:
        return _fail_open("spawn lock unavailable; fail-open")
    granted, retry_after, reason = decision
    if granted:
        return {"granted": True, "waited_s": time.time() - start,
                "retry_after_s": 0.0, "reason": reason,
                "duplicate_suppressed": False, "bypassed": False}

    # Slot busy: poll (lock-free sleeps) until grant or deadline.
    while time.time() < deadline:
        remaining = deadline - time.time()
        time.sleep(min(0.5, max(0.05, remaining)))
        live = _live_running()
        locked, decision = _with_spawn_state(_try_grant)
        if not locked:
            return _fail_open("spawn lock unavailable; fail-open")
        granted, retry_after, reason = decision
        if granted:
            return {"granted": True, "waited_s": time.time() - start,
                    "retry_after_s": 0.0, "reason": reason,
                    "duplicate_suppressed": False, "bypassed": False}

    def _record_deferral(state):
        _record_request(state, name, task, model, False,
                        retry_after_s=retry_after, reason=reason)
        return True, None
    _with_spawn_state(_record_deferral)
    return {"granted": False, "waited_s": time.time() - start,
            "retry_after_s": retry_after, "reason": reason,
            "duplicate_suppressed": False, "bypassed": False}
