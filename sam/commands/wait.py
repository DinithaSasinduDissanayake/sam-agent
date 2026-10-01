#!/usr/bin/env python3
"""SAM wait — Block until agent reaches terminal state, persist it.

Spec: reviews-phase-f-batch2.md — GLM-5.2 §5 + Grok-4.5 timing

Item 5 (wait inversion, IISA review): `--timeout 0` (the default) pins
wait-forever. Nonzero `--timeout N` is deprecated and DETACHES
immediately (exit 0, agent left running, warning on stderr) — it never
waits and never signals. `--kill-after N` is the explicit opt-in that
keeps the old terminate-on-expiry semantics (exit 4, state killed).
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
        # Line 1: Lock-free load, resolve agent
        registry = sam_registry.load_registry()
        agents = registry.get("agents", [])

        ref = (getattr(args, "id_or_name", None) or getattr(args, "name", None)
              or getattr(args, "agent", None))
        if ref is None:
            return _emit_error(5, "agent identifier required", as_json)

        agent = None
        for a in agents:
            if a.get("id") == ref:
                agent = a
                break
        if agent is None:
            matches = [a for a in agents if a.get("name") == ref]
            if matches:
                agent = matches[0]

        if agent is None:
            return _emit_error(5, f"agent not found: {ref}", as_json)

        agent_id = agent["id"]
        start_time = time.monotonic()
        timeout = getattr(args, "timeout", None)
        timeout = 0 if timeout is None else int(timeout)
        kill_after = getattr(args, "kill_after", None)
        try:
            kill_after = int(kill_after) if kill_after is not None else None
        except (TypeError, ValueError):
            kill_after = None

        def _warn_deprecated_timeout():
            print("sam wait: deprecation: --timeout N no longer waits or "
                  "terminates — it detaches immediately (exit 0, agent left "
                  "running). Use --timeout 0 (default) to block until done, "
                  "`sam status` for a non-blocking check, or --kill-after N "
                  "to terminate on expiry. Nonzero --timeout will be "
                  "rejected in a future release.", file=sys.stderr)

        # Line 3: Poll loop
        while True:
            registry = sam_registry.load_registry()
            for a in registry["agents"]:
                if a["id"] == agent_id:
                    agent = a
                    break

            current_state = sam_state.resolve_agent_state(
                agent, agent.get("run_id", 1))

            if current_state in sam_state.TERMINAL_STATES:
                break
            if current_state == "unknown":
                break

            elapsed = time.monotonic() - start_time
            if kill_after is not None and kill_after > 0 and elapsed > kill_after:
                # Item 5: --kill-after N is the explicit kill opt-in.
                # (A nonzero --timeout is ignored while kill-after is set.)
                try:
                    pgid = agent.get("pgid") or agent.get("pid")
                    pid = agent.get("pid")
                    def owned_group():
                        return (pid is not None and pgid == pid
                                and sam_proc.proc_start_time_match(
                                    pid, agent.get("pid_start_time"))
                                and sam_proc.pgid_of(pid) == pgid)
                    if pgid and owned_group():
                        sam_proc.killpg(pgid, signal.SIGTERM)
                        for _ in range(25):
                            if not sam_proc.proc_alive(pgid):
                                break
                            time.sleep(0.2)
                        if sam_proc.proc_alive(pgid) and owned_group():
                            sam_proc.killpg(pgid, signal.SIGKILL)
                            time.sleep(1.0)
                except Exception:
                    pass
                # Spec: exit 4, state killed — persist it (the wrapper
                # never gets to write a result after SIGKILL).
                try:
                    with sam_locks.registry_lock(exclusive=True, timeout=10):
                        reg = sam_registry.load_registry()
                        for a in reg.get("agents", []):
                            if a.get("id") == agent_id:
                                if a.get("state") not in sam_state.TERMINAL_STATES:
                                    a["state"] = "killed"
                                    a["killed_reason"] = "wait_kill_after"
                                    a["updated_at"] = datetime.now(
                                        timezone.utc).strftime(
                                        "%Y-%m-%dT%H:%M:%SZ")
                                break
                        sam_registry.save_registry(reg)
                except Exception:
                    pass
                if timeout > 0:
                    _warn_deprecated_timeout()
                return _emit_error(
                    4, f"kill-after {kill_after}s exceeded; worker terminated",
                    as_json)

            if not (kill_after and kill_after > 0) and timeout > 0:
                # Item 5 inversion: nonzero --timeout DETACHES. Never
                # waits, never signals. --timeout 0 (default) waits forever.
                _warn_deprecated_timeout()
                out = {"status": current_state, "agent_id": agent_id,
                       "detached": True, "timeout": timeout,
                       "result": None,
                       "elapsed_seconds": round(
                           time.monotonic() - start_time, 2)}
                if as_json:
                    print(json.dumps(out))
                else:
                    print(f"Agent {agent_id} still {current_state} — detached "
                          f"({out['elapsed_seconds']}s; deprecated --timeout "
                          f"detach, agent left running)")
                return 0

            time.sleep(0.5)

        # Line 11-14: Persist if needed
        if current_state in ("completed", "failed") and agent.get("state") == "running":
            with sam_locks.registry_lock(exclusive=True, timeout=10):
                registry = sam_registry.load_registry()
                for a in registry["agents"]:
                    if a["id"] == agent_id:
                        if a.get("state") == "running":
                            a["state"] = current_state
                            a["updated_at"] = datetime.now(timezone.utc).strftime(
                                "%Y-%m-%dT%H:%M:%SZ")
                            # Read result.json for exit_code, duration
                            result_path = a.get("result_path")
                            if result_path and os.path.exists(result_path):
                                try:
                                    with open(result_path) as f:
                                        r = json.load(f)
                                    a["exit_code"] = r.get("exit_code")
                                    a["duration_ms"] = r.get("duration_ms")
                                except Exception:
                                    pass
                            sam_registry.save_registry(registry)
                        break

        # Read result.json for output
        result_dict = None
        result_path = agent.get("result_path")
        if result_path and os.path.exists(result_path):
            try:
                with open(result_path) as f:
                    result_dict = json.load(f)
            except Exception:
                pass

        elapsed = round(time.monotonic() - start_time, 2)

        if current_state == "completed":
            out = {"status": "completed", "agent_id": agent_id,
                   "exit_code": 0, "result": result_dict,
                   "elapsed_seconds": elapsed}
            if as_json:
                print(json.dumps(out))
            else:
                print(f"Agent {agent_id} completed (exit 0, {elapsed}s)")
            return 0

        if current_state == "failed":
            ec = (result_dict.get("exit_code") if isinstance(result_dict, dict)
                  else agent.get("exit_code", -1))
            out = {"status": "failed", "agent_id": agent_id,
                   "exit_code": ec, "result": result_dict,
                   "elapsed_seconds": elapsed}
            if as_json:
                print(json.dumps(out))
            else:
                print(f"Agent {agent_id} failed (exit {ec}, {elapsed}s)")
            return 1

        # killed or unknown — exit 0 for ALL terminal states
        # Parent agent reads JSON status field to differentiate
        out = {"status": current_state, "agent_id": agent_id,
               "result": result_dict, "elapsed_seconds": elapsed}
        if as_json:
            print(json.dumps(out))
        else:
            print(f"Agent {agent_id} {current_state} ({elapsed}s)")
        return 0

    except sam_locks.LockTimeout as e:
        return _emit_error(1, f"lock timeout: {e}", as_json)
    except Exception as e:
        return _emit_error(1, str(e), as_json)
