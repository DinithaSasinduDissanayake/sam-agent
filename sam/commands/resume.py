#!/usr/bin/env python3
"""SAM resume — Continue an existing agent's session with a new task.

v0.1.2: Preserves session JSONL (conversation history), allocates new run-NNN/
for log/result, requires --task. No wrapper changes needed — pi handles
resume-vs-create based on session file existence.
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

from sam import config as sam_config
from sam import locks as sam_locks
from sam import proc as sam_proc
from sam import registry as sam_registry
from sam import state as sam_state
from sam import util as sam_util


def run(args):
    """Resume a terminal agent: preserve session, allocate new run, new task."""
    as_json = getattr(args, "json", False)

    try:
        config = sam_config.load_config()
        reg = sam_registry.load_registry()
        agents = reg.get("agents", [])

        ref = (getattr(args, "id_or_name", None) or getattr(args, "name", None))
        if ref is None:
            return _emit(5, "agent identifier required", as_json)

        # Resolve agent
        agent = None
        for a in agents:
            if a.get("id") == ref:
                agent = a
                break
        if agent is None:
            for a in agents:
                if a.get("name") == ref:
                    agent = a
                    break
        if agent is None:
            return _emit(3, f"agent not found: {ref}", as_json)

        agent_id = agent["id"]
        agent_name = agent.get("name", "unnamed")
        task_path = Path(getattr(args, "task", "")).expanduser().resolve()
        if not task_path.is_file():
            return _emit(1, f"task file not found: {task_path}", as_json)

        model = (getattr(args, "model", None)
                 or agent.get("model")
                 or os.environ.get("SAM_MODEL")
                 or config.get("defaults", {}).get("model"))
        if not model:
            return _emit(1, "no model configured", as_json)

        # Lock sequence: name lock + registry lock
        try:
            with sam_locks.name_lock(agent_name, timeout=10):
                with sam_locks.registry_lock(exclusive=True, timeout=10):
                    reg = sam_registry.load_registry()
                    for a in reg["agents"]:
                        if a["id"] == agent_id:
                            agent = a
                            break

                    # Re-resolve state — must be terminal
                    resolved = sam_state.resolve_agent_state(
                        agent, agent.get("run_id", 1))
                    if resolved not in sam_state.TERMINAL_STATES and resolved != "unknown":
                        return _emit(6, f"agent not terminal (state={resolved})", as_json)

                    # Check session file exists
                    session_path = agent.get("session_path")
                    if not session_path or not os.path.exists(session_path):
                        return _emit(1, f"session file not found: {session_path}", as_json)

                    # Check restart budget
                    max_restarts = int(config.get("defaults", {}).get("max_restarts", 1))
                    rc = agent.get("restart_count", 0)
                    if rc >= max_restarts:
                        return _emit(7, f"max restarts ({max_restarts}) reached", as_json)

                    # Allocate new run
                    run_count = agent.get("run_count", 1) + 1
                    new_run_dir = sam_config.agents_dir() / agent_id / f"run-{run_count:03d}"

                    # Update registry: preserve session_path, update run paths
                    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    agent["state"] = "spawning"
                    agent["pid"] = None
                    agent["pgid"] = None
                    agent["pid_start_time"] = None
                    agent["exit_code"] = None
                    agent["exit_signal"] = None
                    agent["killed_reason"] = None
                    agent["run_id"] = run_count
                    agent["run_count"] = run_count
                    agent["restart_count"] = rc + 1
                    # session_path is UNCHANGED (preserves history)
                    agent["log_path"] = str(new_run_dir / "output.log")
                    agent["result_path"] = str(new_run_dir / "result.json")
                    # task_path is writable but might be overwritten below
                    # model can be updated
                    agent["model"] = model
                    agent["updated_at"] = now_str

                    sam_registry.save_registry(reg)

        except sam_locks.LockTimeout:
            return _emit(8, f"could not acquire lock for '{agent_name}'", as_json)

        # Copy task file to tasks dir
        sam_task_path = sam_config.tasks_dir() / f"{agent_id}.md"
        sam_util.copy_task_file(task_path, sam_task_path)
        agent["task_path"] = str(sam_task_path)

        # Create run directory
        new_run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

        # Build argv and env (same as spawn)
        wrapper = sam_config.wrapper_path()
        if not wrapper.is_file():
            return _emit(1, "wrapper not installed; run sam init first", as_json)

        argv = [
            str(wrapper),
            "--agent-id", agent_id,
            "--model", model,
            "--session", agent["session_path"],
            "--task", str(sam_task_path),
            "--result", agent["result_path"],
        ]

        parent_depth = int(os.environ.get("SAM_DEPTH", "0"))
        env = sam_util.build_child_env(agent_id, model, parent_depth)
        cwd = agent.get("cwd", os.getcwd())

        try:
            proc = subprocess.Popen(
                argv, cwd=cwd, env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        except Exception as e:
            with sam_locks.registry_lock(exclusive=True, timeout=10):
                reg = sam_registry.load_registry()
                for a in reg["agents"]:
                    if a["id"] == agent_id:
                        a["state"] = "failed"
                        a["exit_code"] = -1
                        a["updated_at"] = datetime.now(timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%SZ")
                        break
                sam_registry.save_registry(reg)
            return _emit(1, f"resume Popen failed: {e}", as_json)

        # Persist running state
        with sam_locks.registry_lock(exclusive=True, timeout=10):
            reg = sam_registry.load_registry()
            for a in reg["agents"]:
                if a["id"] == agent_id:
                    try:
                        a["state"] = "running"
                        a["pid"] = proc.pid
                        a["pgid"] = proc.pid
                        a["pid_start_time"] = sam_proc.read_pid_start_time(proc.pid)
                        a["updated_at"] = datetime.now(timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%SZ")
                        sam_registry.save_registry(reg)
                    except Exception:
                        sam_proc.killpg(proc.pid, signal.SIGKILL)
                        return _emit(1, "resume PID persist failed", as_json)
                    break

        result = {
            "status": "ok",
            "agent_id": agent_id,
            "name": agent_name,
            "run_id": run_count,
            "pid": proc.pid,
            "session_path": agent.get("session_path"),
            "session_continued": True,
        }
        if as_json:
            print(json.dumps(result))
        else:
            print(f"Resumed agent {agent_id} (run {run_count}, pid {proc.pid})")
        return 0

    except sam_locks.LockTimeout as e:
        return _emit(1, f"lock timeout: {e}", as_json)
    except Exception as e:
        return _emit(1, str(e), as_json)


def _emit(code, message, as_json):
    if as_json:
        print(json.dumps({"status": "error", "code": code, "message": message}),
              file=sys.stderr)
    else:
        print(f"sam: {message}", file=sys.stderr)
    return code
