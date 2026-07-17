#!/usr/bin/env python3
"""SAM state module: resolve_agent_state, TERMINAL_STATES, is_terminal().

Spec: reviews-line-by-line.md — GLM-5.2, File: sam/state.py
"""

import json
import os
from datetime import datetime, timezone

from sam import proc as sam_proc


TERMINAL_STATES = frozenset({"completed", "failed", "killed"})


def is_terminal(state):
    """Line 1: Return state in TERMINAL_STATES."""
    return state in TERMINAL_STATES


def resolve_agent_state(
    agent_entry,
    run_id,
    proc_alive_fn=sam_proc.proc_alive,
    read_start_time_fn=sam_proc.read_pid_start_time,
    read_result_fn=None,  # will use _read_result_file if None
    _read_result_helper=None,
):
    """Compute the true state of an agent from registry + /proc + result.json.

    Pure computation — NEVER writes the registry.

    Parameters:
        agent_entry (dict): Registry entry for one agent.
        run_id (int): The current run_id to validate against result.json.
        proc_alive_fn (callable): Function to check PID liveness.
        read_start_time_fn (callable): Function to read PID start time.
        read_result_fn (callable): Function to read result.json given path.

    Returns:
        str: One of: spawning, running, completed, failed, killed, unknown
    """
    # Line 1: registry_state = agent_entry.get("state")
    registry_state = agent_entry.get("state")

    # Line 2: If registry_state in TERMINAL_STATES, return registry_state
    if registry_state in TERMINAL_STATES:
        return registry_state

    # Line 3: pid = agent_entry.get("pid")
    pid = agent_entry.get("pid")

    # Line 4: If pid is None
    if pid is None:
        # Line 5-7: spawning → check launch_deadline_at
        if registry_state == "spawning":
            deadline = agent_entry.get("launch_deadline_at")
            if deadline is not None:
                try:
                    if datetime.now(timezone.utc) < _parse_iso(deadline):
                        return "spawning"
                except (ValueError, TypeError):
                    pass
            return "failed"
        # Line 8: running → unknown
        if registry_state == "running":
            return "unknown"

    # Line 9: alive = proc_alive_fn(pid)
    alive = proc_alive_fn(pid)

    # Check result.json FIRST — it is the authoritative completion marker
    # If result.json exists with valid final_state_hint, trust it over PID state
    # (PID may be recycled immediately after process exits, causing false "unknown")
    result = _read_result(agent_entry, read_result_fn)
    if result is not None and isinstance(result, dict):
        hint = result.get("final_state_hint")
        if hint == "completed":
            return "completed"
        if hint == "failed":
            return "failed"

    # Line 10-14: PID-based checks (only if result.json didn't give us the answer)
    if alive:
        current_start = read_start_time_fn(pid)
        stored_start = agent_entry.get("pid_start_time")
        if current_start is not None and current_start == stored_start:
            return "running"
        return "unknown"

    # Line 15: PID is dead — check result.json (already checked above, but
    # recheck for the dead-PID-specific rules)
    result = _read_result(agent_entry, read_result_fn)

    # Line 17: If result exists with valid final_state_hint
    if result is not None and isinstance(result, dict):
        hint = result.get("final_state_hint")
        if hint in ("completed", "failed"):
            return hint
        # Line 21: invalid hint → failed
        # Line 19: if hint == "completed" → completed
        if hint == "completed":
            return "completed"
        # Line 20: if hint == "failed" → failed
        if hint == "failed":
            return "failed"
        # Line 21: else → failed (invalid hint)
        return "failed"

    # Line 22: If result is corrupt/invalid → failed
    # (read_result_fn should catch exceptions; if it returns something invalid, treat as failed)
    if result is not None and not isinstance(result, dict):
        return "failed"

    # Line 23-24: PID is dead, no valid result
    if registry_state == "spawning":
        return "failed"
    # Line 25: running → unknown
    return "unknown"


def _parse_iso(iso_str):
    """Parse ISO 8601 string to datetime. Supports Z suffix."""
    if iso_str.endswith("Z"):
        iso_str = iso_str[:-1] + "+00:00"
    return datetime.fromisoformat(iso_str)


def _read_result_file(result_path):
    """Default result reader: read and parse result.json."""
    import json
    try:
        if result_path and __import__('os').path.exists(result_path):
            with open(result_path) as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _read_result(agent_entry, read_result_fn):
    """Read result.json from agent entry. Returns dict or None."""
    result_path = agent_entry.get("result_path")
    if result_path is not None:
        reader = read_result_fn if read_result_fn is not None else _read_result_file
        return reader(result_path)
    return None
