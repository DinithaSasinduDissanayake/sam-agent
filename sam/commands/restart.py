#!/usr/bin/env python3
"""SAM restart — Restart terminal agent with new run-N directory.

Spec: reviews-phase-f-batch2.md — GLM-5.2 §2 + Grok-4.5 16-step sequence
"""

import json
import os
import shutil
import signal
import subprocess
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
        # Line 1: Lock-free load, find agent
        registry = sam_registry.load_registry()
        agents = registry.get("agents", [])

        ref = getattr(args, "id_or_name", None) or getattr(args, "name", None)
        if ref is None:
            return _emit_error(3, "agent identifier required", as_json)

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
            return _emit_error(3, f"agent not found: {ref}", as_json)

        target_name = agent.get("name", "unnamed")
        agent_id = agent["id"]

        # Line 3: Acquire name lock + registry lock
        with sam_locks.name_lock(target_name, timeout=10):
            with sam_locks.registry_lock(exclusive=True, timeout=10):
                registry = sam_registry.load_registry()
                for a in registry["agents"]:
                    if a["id"] == agent_id:
                        agent = a
                        break

                # Line 6-7: Resolve state, must be terminal or unknown
                resolved = sam_state.resolve_agent_state(
                    agent, agent.get("run_id", 1))
                if resolved not in sam_state.TERMINAL_STATES and resolved != "unknown":
                    return _emit_error(6, f"agent not terminal (state={resolved})", as_json)

                # Line 8: Check max restarts
                config = sam_config.load_config()
                max_restarts = int(config.get("defaults", {}).get("max_restarts", 1))
                rc = agent.get("restart_count", 0)
                if rc >= max_restarts:
                    return _emit_error(7, f"max restarts ({max_restarts}) reached", as_json)

                # Line 9: Update counters
                run_count = agent.get("run_count", 1) + 1
                agent["run_id"] = run_count
                agent["run_count"] = run_count
                if resolved != "completed":
                    agent["restart_count"] = rc + 1
                else:
                    agent["restart_count"] = 0  # Reset on success

                # Line 10-11: Reset state to spawning
                now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                deadline = datetime.now(timezone.utc).isoformat() + "Z"
                agent["state"] = "spawning"
                agent["pid"] = None
                agent["pgid"] = None
                agent["pid_start_time"] = None
                agent["exit_code"] = None
                agent["exit_signal"] = None
                agent["killed_reason"] = None
                agent["launch_deadline_at"] = deadline

                # Line 12: New run directory paths
                new_run_dir = sam_config.agents_dir() / agent_id / f"run-{run_count:03d}"
                agent["log_path"] = str(new_run_dir / "output.log")
                agent["result_path"] = str(new_run_dir / "result.json")
                agent["session_path"] = str(new_run_dir / "session.jsonl")

                # Line 13: Save registry
                sam_registry.save_registry(registry)

            # Line 15: Create run directory (outside lock)
            new_run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

            # Line 16: Build argv (same as spawn)
            wrapper = sam_config.wrapper_path()
            if not wrapper.is_file():
                return _emit_error(1, "wrapper not installed; run sam init first", as_json)

            argv = [
                str(wrapper),
                "--agent-id", agent_id,
                "--model", agent.get("model", ""),
                "--session", agent["session_path"],
                "--task", agent["task_path"],
                "--result", agent["result_path"],
            ]

            # Build env (same as spawn)
            env = os.environ.copy()
            env["SAM_AGENT_ID"] = agent_id
            env["SAM_MODEL"] = agent.get("model", "")
            parent_depth = int(os.environ.get("SAM_DEPTH", "0"))
            env["SAM_DEPTH"] = str(parent_depth + 1)
            spawner_id = os.environ.get("SAM_AGENT_ID")
            env["SAM_PARENT_ID"] = spawner_id or ""
            env["SAM_ROOT_ID"] = os.environ.get("SAM_ROOT_ID") or agent_id

            cwd = agent.get("cwd", os.getcwd())

            try:
                # Line 17: Popen
                proc = subprocess.Popen(
                    argv, cwd=cwd, env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    close_fds=True,
                )
            except Exception as e:
                # Line 18: Popen failed — mark failed
                with sam_locks.registry_lock(exclusive=True, timeout=10):
                    registry = sam_registry.load_registry()
                    for a in registry["agents"]:
                        if a["id"] == agent_id:
                            a["state"] = "failed"
                            a["exit_code"] = -1
                            a["updated_at"] = datetime.now(timezone.utc).strftime(
                                "%Y-%m-%dT%H:%M:%SZ")
                            break
                    sam_registry.save_registry(registry)
                return _emit_error(1, f"restart Popen failed: {e}", as_json)

            # Line 19-22: Update registry to running
            with sam_locks.registry_lock(exclusive=True, timeout=10):
                registry = sam_registry.load_registry()
                for a in registry["agents"]:
                    if a["id"] == agent_id:
                        try:
                            a["state"] = "running"
                            a["pid"] = proc.pid
                            a["pgid"] = proc.pid
                            a["pid_start_time"] = sam_proc.read_pid_start_time(proc.pid)
                            a["launch_deadline_at"] = None
                            a["updated_at"] = datetime.now(timezone.utc).strftime(
                                "%Y-%m-%dT%H:%M:%SZ")
                            sam_registry.save_registry(registry)
                        except Exception:
                            # Line 23: Save failed — kill orphan
                            sam_proc.killpg(proc.pid, signal.SIGKILL)
                            return _emit_error(1, "restart PID persist failed", as_json)
                        break

        # Line 24: Success
        result = {"status": "ok", "agent_id": agent_id,
                  "pid": proc.pid, "run_count": run_count}
        if as_json:
            print(json.dumps(result))
        else:
            print(f"Restarted agent {agent_id} (pid {proc.pid}, run {run_count})")
        return 0

    except sam_locks.LockTimeout as e:
        return _emit_error(1, f"lock timeout: {e}", as_json)
    except Exception as e:
        return _emit_error(1, str(e), as_json)
