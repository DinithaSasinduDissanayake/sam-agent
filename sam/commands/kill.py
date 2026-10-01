#!/usr/bin/env python3
"""SAM kill — Safely terminate a running agent (SIGTERM -> SIGKILL).

Spec: reviews-phase-f-batch2.md — GLM-5.2 §3 + Grok-4.5 timing/error details
"""

import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_SAM_PKG = _THIS_DIR.parent
if str(_SAM_PKG) not in sys.path:
    sys.path.insert(0, str(_SAM_PKG))

from sam import config as sam_config
from sam import locks as sam_locks
from sam import proc as sam_proc
from sam import registry as sam_registry
from sam import run_times as sam_run_times
from sam import state as sam_state


def _emit_error(code, message, as_json):
    if as_json:
        print(json.dumps({"status": "error", "code": code, "message": message}), file=sys.stderr)
    else:
        print(f"sam: {message}", file=sys.stderr)
    return code


def run(args):
    as_json = getattr(args, "json", False)

    try:
        # Line 1: Load registry, resolve agent
        registry = sam_registry.load_registry()
        agents = registry.get("agents", [])

        ref = getattr(args, "id_or_name", None) or getattr(args, "name", None)
        if ref is None:
            return _emit_error(1, "agent identifier required", as_json)

        agent = sam_registry.resolve_ref(agents, ref)
        if agent is None:
            return _emit_error(3, f"agent not found: {ref}", as_json)

        agent_id = agent["id"]

        # Line 2: Enter registry lock
        with sam_locks.registry_lock(exclusive=True, timeout=10):
            registry = sam_registry.load_registry()
            for a in registry["agents"]:
                if a["id"] != agent_id:
                    continue
                agent = a
                break

            snap_run_id = agent.get("run_id") or agent.get("run_count") or 1
            snap_pid = agent.get("pid")

            # Line 4-7: Resolve state, check killability
            current_state = sam_state.resolve_agent_state(agent, snap_run_id)

            # awaiting_retry = queued infra retry: kill cancels the queue item
            # (operator contract: enqueue/cancel/prioritize only — no signals).
            if current_state == "awaiting_retry":
                from sam import retry as sam_retry
                sam_retry.remove(agent_id)
                agent["retry_not_before"] = None
                agent["retry_kind"] = None
                agent["state"] = "killed"
                agent["killed_reason"] = "retry_cancelled"
                agent["updated_at"] = datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ")
                sam_registry.save_registry(registry)
                if as_json:
                    print(json.dumps({"status": "ok", "agent_id": agent_id,
                          "outcome": "cancelled", "previous_state": "awaiting_retry"}))
                else:
                    print(f"Cancelled queued retry for {agent_id} (now killed)")
                return 0

            if current_state in sam_state.TERMINAL_STATES:
                # Already terminal — idempotent
                if as_json:
                    print(json.dumps({"status": "ok",
                          "outcome": "already_terminal",
                          "state": current_state}))
                else:
                    print(f"Agent {agent_id} already {current_state}")
                return 0

            if current_state == "unknown":
                # Archive unknowns: dead/recycled PID → mark killed, no signal (safe).
                pid_u = agent.get("pid")
                stored_u = agent.get("pid_start_time")
                alive_u = sam_proc.proc_alive(pid_u) if pid_u else False
                identity_ok = bool(pid_u and stored_u and alive_u and sam_proc.proc_start_time_match(pid_u, stored_u))
                if not identity_ok:
                    agent["state"] = "killed"
                    agent["killed_reason"] = "unknown_stale"
                    agent["updated_at"] = datetime.now(timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ")
                    evidence = sam_run_times.run_end_evidence(agent)
                    agent["ended_at"] = (
                        evidence.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                        if evidence is not None else agent["updated_at"])
                    sam_registry.save_registry(registry)
                    if as_json:
                        print(json.dumps({"status": "ok", "agent_id": agent_id,
                              "outcome": "killed", "previous_state": "unknown"}))
                    else:
                        print(f"Killed agent {agent_id} (was unknown, PID dead/recycled, no signal sent)")
                    return 0
                # Live PID with matching identity despite unknown → fall through to normal kill.

            if current_state == "spawning" and agent.get("pid") is None:
                return _emit_error(1, "agent not killable yet (still spawning)", as_json)

            # Line 8: Verify PID identity before signaling
            pid = agent.get("pid")
            stored_start = agent.get("pid_start_time")
            if pid and stored_start:
                if not sam_proc.proc_start_time_match(pid, stored_start):
                    return _emit_error(1, "PID identity mismatch (recycled?)", as_json)

            target_pgid = agent.get("pgid") or pid
            if not target_pgid:
                return _emit_error(1, "no process group ID recorded", as_json)

        # Line 10: Exit lock
        # Line 11: Send SIGTERM
        sam_proc.killpg(target_pgid, signal.SIGTERM)

        # Line 12-14: Poll for death (5s max)
        for _ in range(25):  # 25 * 0.2 = 5s
            if not sam_proc.group_alive(target_pgid):
                break
            time.sleep(0.2)

        # Line 15: If anything in the group survived, send SIGKILL
        if sam_proc.group_alive(target_pgid):
            sam_proc.killpg(target_pgid, signal.SIGKILL)
            time.sleep(2.0)

        # Line 16: Re-enter lock to confirm
        with sam_locks.registry_lock(exclusive=True, timeout=10):
            registry = sam_registry.load_registry()
            for a in registry["agents"]:
                if a["id"] != agent_id:
                    continue
                a_run = a.get("run_id") or a.get("run_count") or 1
                if snap_run_id is not None and a_run != snap_run_id:
                    # New run started concurrently; do not clobber
                    break
                if snap_pid is not None and a.get("pid") != snap_pid:
                    # PID changed concurrently; do not clobber
                    break
                agent = a
                final_state = sam_state.resolve_agent_state(agent, a_run)

                if final_state not in sam_state.TERMINAL_STATES:
                    # Process might be hung (D-state)
                    print(f"Warning: agent {agent_id} may still be alive "
                          f"(state={final_state})", file=sys.stderr)

                if agent.get("state") not in sam_state.TERMINAL_STATES:
                    agent["state"] = "killed"
                    agent["killed_reason"] = "user_kill"
                    agent["updated_at"] = datetime.now(timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ")
                    agent["ended_at"] = agent["updated_at"]
                    sam_registry.save_registry(registry)
                break

        if as_json:
            print(json.dumps({"status": "ok", "agent_id": agent_id,
                  "outcome": "killed"}))
        else:
            print(f"Killed agent {agent_id}")
        return 0

    except sam_locks.LockTimeout as e:
        return _emit_error(1, f"lock timeout: {e}", as_json)
    except Exception as e:
        return _emit_error(1, str(e), as_json)
