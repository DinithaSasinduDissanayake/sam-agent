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

def group_alive(pgid):
    """True while any NON-zombie process is in process group ``pgid``.

    os.killpg(pgid, 0) succeeds while any member exists, including unreaped
    zombies, so members are confirmed via /proc. Never raises.
    """
    if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 0:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    try:
        entries = os.listdir("/proc")
    except OSError:
        return True
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "r") as f:
                data = f.read()
        except OSError:
            continue
        lp = data.rfind(")")
        if lp == -1:
            continue
        fields = data[lp + 1:].strip().split()
        if len(fields) < 3:
            continue
        try:
            if int(fields[2]) == pgid and fields[0] != "Z":
                return True
        except ValueError:
            continue
    return False


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
                if line.startswith("rchar:"):
                    io_read = int(line.split()[1])
                elif line.startswith("wchar:"):
                    io_write = int(line.split()[1])
                elif io_read is None and line.startswith("read_bytes:"):
                    io_read = int(line.split()[1])
                elif io_write is None and line.startswith("write_bytes:"):
                    io_write = int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return {"state": state, "cpu_ticks": cpu_ticks,
            "io_read_bytes": io_read, "io_write_bytes": io_write}


PROBE_DIR = "probe"


def probe_dir(sam_home=None):
    from sam import config as sam_config
    return (sam_home or sam_config.get_sam_home()) / PROBE_DIR


def probe_path(agent_id, sam_home=None):
    return probe_dir(sam_home) / f"{agent_id}.json"


def load_probe_sample(agent_id, sam_home=None):
    p = probe_path(agent_id, sam_home)
    if not p.is_file():
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_probe_sample(agent_id, sample, sam_home=None):
    p = probe_path(agent_id, sam_home)
    try:
        p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        import tempfile
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix="probe.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(sample, f)
            f.flush()
        os.replace(tmp, p)
    except Exception:
        pass


def sample_process_group(pid, pgid=None):
    """Enumerate processes in the target's process group or session.

    Matches pgrp == target_pgid or session == target_sid.
    Returns dict: str(pid) -> {"starttime": int, "cpu": int, "io_read": int, "io_write": int}
    """
    if pid is None and pgid is None:
        return None
    target_pgid = pgid or (pgid_of(pid) if pid else None)
    if target_pgid is not None and target_pgid <= 0:
        target_pgid = None
    target_sid = None
    if pid is not None:
        try:
            with open(f"/proc/{pid}/stat", "r") as f:
                data = f.read()
            lp = data.rfind(")")
            if lp != -1:
                fields = data[lp + 1:].strip().split()
                if len(fields) > 3:
                    sid_val = int(fields[3])  # session id (field 3)
                    if sid_val > 0:
                        target_sid = sid_val
        except Exception:
            pass

    if target_pgid is None and target_sid is None:
        return None

    samples = {}
    try:
        proc_entries = os.listdir("/proc")
    except OSError:
        return None

    for entry in proc_entries:
        if not entry.isdigit():
            continue
        p_int = int(entry)
        stat_path = f"/proc/{entry}/stat"
        try:
            with open(stat_path, "r") as f:
                data = f.read()
            lp = data.rfind(")")
            if lp == -1:
                continue
            fields = data[lp + 1:].strip().split()
            if len(fields) < 20:
                continue
            pgrp = int(fields[2])
            sid = int(fields[3])
            matched = False
            if target_pgid is not None and pgrp == target_pgid:
                matched = True
            elif target_sid is not None and sid == target_sid:
                matched = True
            if not matched:
                continue
            utime = int(fields[11])
            stime = int(fields[12])
            starttime = int(fields[19])
        except (OSError, ValueError, IndexError):
            continue

        rchar = 0
        wchar = 0
        io_path = f"/proc/{entry}/io"
        try:
            with open(io_path, "r") as f:
                for line in f:
                    if line.startswith("rchar:"):
                        rchar = int(line.split()[1])
                    elif line.startswith("wchar:"):
                        wchar = int(line.split()[1])
        except (OSError, ValueError, IndexError):
            pass

        samples[str(p_int)] = {
            "starttime": starttime,
            "cpu": utime + stime,
            "io_read": rchar,
            "io_write": wchar,
        }

    return samples if samples else None


def read_group_resource(pgid):
    """Enumerate /proc/*/stat for pgrp == pgid, sum utime+stime,
    and sum rchar/wchar from /proc/<pid>/io.
    """
    samples = sample_process_group(pgid, pgid)
    if not samples:
        return None
    total_cpu = sum(s["cpu"] for s in samples.values())
    total_rd = sum(s["io_read"] for s in samples.values())
    total_wr = sum(s["io_write"] for s in samples.values())
    return {
        "cpu_ticks": total_cpu,
        "io_read_bytes": total_rd,
        "io_write_bytes": total_wr,
    }


def compute_sample_delta(sample_a, sample_b, interval_seconds):
    """Compute per-pid resource deltas between sample_a and sample_b.

    Per N6:
    - Match processes present in both samples by (pid, starttime).
    - New pids count as movement.
    - Exited children do not subtract counters or hide sibling activity.
    """
    if sample_a is None or sample_b is None:
        return None

    total_cpu_delta = 0
    total_rd_delta = 0
    total_wr_delta = 0
    moving = False

    for pid, b_data in sample_b.items():
        if pid not in sample_a or sample_a[pid].get("starttime") != b_data.get("starttime"):
            # New process spawned in the group
            moving = True
            total_cpu_delta += b_data.get("cpu", 0)
            total_rd_delta += b_data.get("io_read", 0)
            total_wr_delta += b_data.get("io_write", 0)
        else:
            a_data = sample_a[pid]
            c_delta = max(0, b_data.get("cpu", 0) - a_data.get("cpu", 0))
            r_delta = max(0, b_data.get("io_read", 0) - a_data.get("io_read", 0))
            w_delta = max(0, b_data.get("io_write", 0) - a_data.get("io_write", 0))
            if c_delta > 0 or r_delta > 0 or w_delta > 0:
                moving = True
            total_cpu_delta += c_delta
            total_rd_delta += r_delta
            total_wr_delta += w_delta

    return {
        "interval_seconds": interval_seconds,
        "cpu_ticks": total_cpu_delta,
        "io_read_bytes": total_rd_delta,
        "io_write_bytes": total_wr_delta,
        "moving": moving,
    }


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


def resource_delta(pid, interval_seconds, sleep_fn=None, pgid=None, agent_id=None):
    """Two-sample CPU/IO delta over interval_seconds (or cross-call).

    Probes the whole process group and session with per-pid tracking (N6).
    If agent_id is provided and a fresh persisted sample exists (0.5s - 300s),
    uses that sample without sleeping.
    """
    target_pgid = pgid or (pgid_of(pid) if pid else None)
    if target_pgid is not None and target_pgid <= 0:
        target_pgid = None
    now = time.time()
    curr_samples = sample_process_group(pid, target_pgid)
    if curr_samples is None and pid is not None:
        single = read_proc_resource(pid)
        if single is not None:
            curr_samples = {
                str(pid): {
                    "starttime": read_pid_start_time(pid) or 0,
                    "cpu": single.get("cpu_ticks", 0),
                    "io_read": single.get("io_read_bytes", 0),
                    "io_write": single.get("io_write_bytes", 0),
                }
            }

    if curr_samples is None:
        return None

    curr_record = {"ts": now, "pids": curr_samples}

    # Cross-call continuity check
    if agent_id:
        prev_record = load_probe_sample(agent_id)
        if prev_record and isinstance(prev_record, dict) and "ts" in prev_record and "pids" in prev_record:
            dt = now - float(prev_record["ts"])
            if 0.5 <= dt <= 300.0:
                save_probe_sample(agent_id, curr_record)
                return compute_sample_delta(prev_record["pids"], curr_samples, dt)

    if sleep_fn is None:
        sleep_fn = time.sleep
    if interval_seconds and interval_seconds > 0:
        sleep_fn(interval_seconds)

    sample_b = sample_process_group(pid, target_pgid)
    if sample_b is None and pid is not None:
        single_b = read_proc_resource(pid)
        if single_b is not None:
            sample_b = {
                str(pid): {
                    "starttime": read_pid_start_time(pid) or 0,
                    "cpu": single_b.get("cpu_ticks", 0),
                    "io_read": single_b.get("io_read_bytes", 0),
                    "io_write": single_b.get("io_write_bytes", 0),
                }
            }

    if sample_b is None:
        if agent_id:
            save_probe_sample(agent_id, curr_record)
        return None

    dt = max(0.001, time.time() - now)
    if agent_id:
        save_probe_sample(agent_id, {"ts": time.time(), "pids": sample_b})
    return compute_sample_delta(curr_samples, sample_b, dt)


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


def _with_spawn_state(fn, timeout_s=5.0):
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
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    except OSError:
        return False, None
    try:
        deadline = time.monotonic() + timeout_s
        acquired = False
        while time.monotonic() < deadline:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                time.sleep(0.02)
                continue
            except InterruptedError:
                continue
            except OSError:
                return False, None
        if not acquired:
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
                "bypassed": False, "fail_open": True}

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
                "duplicate_suppressed": False, "bypassed": True,
                "fail_open": False}

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
        return False, (True, None)
    locked, dup = _with_spawn_state(_check_duplicate)
    if not locked:
        return _fail_open("spawn lock unavailable; fail-open")
    proceed, dup_info = dup
    if not proceed:
        return {"granted": False, "waited_s": 0.0,
                "retry_after_s": dup_info["retry_after_s"],
                "reason": dup_info["reason"],
                "duplicate_suppressed": True, "bypassed": False,
                "fail_open": False}

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
                "duplicate_suppressed": False, "bypassed": False,
                "fail_open": False}

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
                    "duplicate_suppressed": False, "bypassed": False,
                    "fail_open": False}

    def _record_deferral(state):
        _record_request(state, name, task, model, False,
                        retry_after_s=retry_after, reason=reason)
        return True, None
    _with_spawn_state(_record_deferral)
    return {"granted": False, "waited_s": time.time() - start,
            "retry_after_s": retry_after, "reason": reason,
            "duplicate_suppressed": False, "bypassed": False,
            "fail_open": False}


LAUNCHES_FILE = "launches.jsonl"


def launches_path(sam_home=None):
    from sam import config as sam_config
    return (sam_home or sam_config.get_sam_home()) / LAUNCHES_FILE


def record_launch(agent_id, run_id, kind, bypassed=False, fail_open=False, model=None, name=None, ts=None, harness=None):
    """Record a launch event in append-only launches.jsonl."""
    ts = time.time() if ts is None else float(ts)
    entry = {
        "ts": ts,
        "agent_id": agent_id,
        "name": name,
        "run_id": run_id,
        "kind": kind,
        "model": model,
        "bypassed": bool(bypassed),
        "fail_open": bool(fail_open),
        "harness": harness,
    }
    path = launches_path()
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
            f.flush()
    except Exception:
        pass


def load_launches(sam_home=None):
    """Load all launch records from launches.jsonl."""
    path = launches_path(sam_home)
    if not path.is_file():
        return []
    records = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except Exception:
                        pass
    except Exception:
        pass
    return records


def count_live_agents():
    """Count running agents by inspecting true state."""
    from sam import registry as sam_registry
    from sam import state as sam_state
    try:
        reg = sam_registry.load_registry()
        n = 0
        for a in reg.get("agents", []):
            try:
                if sam_state.resolve_agent_state(
                        a, a.get("run_id", 1)) == "running":
                    n += 1
            except Exception:
                continue
        return n
    except Exception:
        return 0


def launch_gate(
    name,
    task,
    model,
    kind="spawn",
    override_reason=None,
    no_space=False,
    is_infra_retry=False,
    wait_s=None,
    running=None,
    count_running=None,
):
    """Unified launch admission gate for spawn, resume, restart, and retry.

    1. Quota Circuit Breaker:
       If not an infra retry, checks sam_retry.active_window(model).
       If an active 429 window exists and no override_reason is provided,
       defers with exit code 6 (quota_window).
    2. Spacing & Concurrency Limiter:
       Enforces global >=15s spacing and max 4 running agents via
       acquire_spawn_slot. Bounded wait up to wait_s.
    """
    if count_running is None:
        count_running = count_live_agents
    if running is None:
        running = count_running()

    from sam import retry as sam_retry

    # 1. Quota breaker
    if not is_infra_retry:
        sam_retry.reconcile_pending()
        quota_window = None if override_reason else sam_retry.active_window(model)
        if quota_window is not None:
            retry_after = max(1, int(quota_window - time.time() + 0.5))
            try:
                from datetime import datetime, timezone
                eta_str = datetime.fromtimestamp(quota_window, timezone.utc).strftime("%H:%M:%SZ")
            except Exception:
                eta_str = str(quota_window)
            guidance = (
                f"429 quota window active for model {model} "
                f"(advisory until ~{eta_str}). "
                f"This is NOT an error — do not abort the task. "
                f"Fresh {kind}s defer ~{retry_after}s "
                f"(e.g. `sleep {retry_after}` then re-run this exact {kind} "
                f"command unchanged), or pass --override-reason 'why now' to force "
                f"(logged), or pick a different --model. Queued infra-retries "
                f"for this model are unaffected."
            )
            return {
                "granted": False,
                "code": 6,
                "reason": "quota_window",
                "message": guidance,
                "retry_after_s": retry_after,
                "model": model,
                "window_until": quota_window,
                "duplicate_suppressed": False,
                "bypassed": False,
                "fail_open": False,
            }

    # 2. Spacing & capacity slot acquisition
    if wait_s is None:
        try:
            wait_s = float(os.environ.get("SAM_SLOT_WAIT_S", SLOT_WAIT_S))
        except (TypeError, ValueError):
            wait_s = SLOT_WAIT_S

    slot = acquire_spawn_slot(
        name, str(task), model, no_space=no_space, wait_s=wait_s,
        running=running, count_running=count_running
    )
    if not slot["granted"]:
        retry_after = max(1, int(slot["retry_after_s"] + 0.5))
        guidance = (
            f"Launch slot unavailable ({slot['reason']}). "
            f"This is NOT an error — do not abort the task. "
            f"Wait ~{retry_after}s (e.g. `sleep {retry_after}`) and re-run "
            f"this exact {kind} command unchanged."
        )
        if slot.get("duplicate_suppressed"):
            guidance += " (duplicate request suppressed; slot still held)"
        return {
            "granted": False,
            "code": 6,
            "reason": slot["reason"],
            "message": guidance,
            "retry_after_s": retry_after,
            "duplicate_suppressed": slot.get("duplicate_suppressed", False),
            "bypassed": False,
            "fail_open": False,
        }

    return {
        "granted": True,
        "code": 0,
        "reason": slot.get("reason", "granted"),
        "message": "ok",
        "retry_after_s": 0.0,
        "waited_s": round(slot.get("waited_s", 0.0), 1),
        "bypassed": bool(slot.get("bypassed", False)),
        "fail_open": bool(slot.get("fail_open", False)),
        "duplicate_suppressed": False,
    }


def emit_gate_rejection(gate_res, as_json):
    """Emit formatted rejection on stderr and return exit code."""
    import sys
    if as_json:
        payload = {
            "status": "deferred",
            "code": gate_res.get("code", 6),
            "message": gate_res.get("message", "deferred"),
            "retry_after_s": gate_res.get("retry_after_s", 15),
            "reason": gate_res.get("reason", "slot_unavailable"),
        }
        if gate_res.get("window_until") is not None:
            payload["window_until"] = gate_res["window_until"]
        if "model" in gate_res:
            payload["model"] = gate_res["model"]
        if gate_res.get("duplicate_suppressed"):
            payload["duplicate_suppressed"] = True
        print(json.dumps(payload), file=sys.stderr)
    else:
        print(f"sam: {gate_res.get('message', 'deferred')}", file=sys.stderr)
    return gate_res.get("code", 6)

