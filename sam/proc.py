#!/usr/bin/env python3
"""SAM proc module: PID helpers — alive, start_time, killpg, pgid_of.

Spec: reviews-line-by-line.md — GLM-5.2, File: sam/proc.py
"""

import os
import signal
import time


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
        return False  # Still alive (unlikely)
    except (ProcessLookupError, PermissionError):
        return True
