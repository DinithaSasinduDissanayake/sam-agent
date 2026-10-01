#!/usr/bin/env python3
"""SAM resume — Continue an existing agent's session with a new task.

v0.1.2: Preserves session JSONL (conversation history), allocates new run-NNN/
for log/result, requires --task. No wrapper changes needed — pi handles
resume-vs-create based on session file existence.
"""

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sam import config as sam_config
from sam import harness as sam_harness
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
        agent = sam_registry.resolve_ref(agents, ref)
        if agent is None:
            return _emit(3, f"agent not found: {ref}", as_json)

        agent_id = agent["id"]
        agent_name = agent.get("name", "unnamed")
        task_path = Path(getattr(args, "task", "")).expanduser().resolve()
        if not task_path.is_file():
            return _emit(1, f"task file not found: {task_path}", as_json)

        # Harness: explicit flag → $SAM_HARNESS → stored entry → config → pi
        try:
            if getattr(args, "harness", None) or os.environ.get("SAM_HARNESS"):
                harness = sam_config.resolve_harness(getattr(args, "harness", None), config)
            else:
                harness = agent.get("harness") or sam_config.resolve_harness(None, config)
        except ValueError as e:
            return _emit(2, str(e), as_json)
        model = (getattr(args, "model", None) or agent.get("model")
                 or sam_config.resolve_model(None, harness, config))
        thinking = getattr(args, "thinking", None)
        effort = getattr(args, "effort", None)
        if harness == "agy" and thinking:
            return _emit(2, "--thinking cannot be used with --harness agy; use --effort", as_json)
        if effort and harness != "agy":
            return _emit(2, "--effort requires --harness agy", as_json)

        infra_retry = bool(getattr(args, "_infra_retry", False))
        override_reason = (getattr(args, "override_reason", None) or "").strip() or None
        no_space = bool(getattr(args, "no_space", False))

        # Pre-flight state check before launch gate / slot wait
        resolved = sam_state.resolve_agent_state(
            agent, agent.get("run_id", 1))
        if resolved == "awaiting_retry" and not infra_retry:
            item = _queued_retry(agent_id)
            if item is not None:
                fires = _fmt_fires(item)
                return _emit(5, f"already_queued{fires} "
                                f"(sam retry to fire now, or --cancel)",
                             as_json)
            resolved = "failed"
        if resolved not in sam_state.TERMINAL_STATES and resolved != "unknown":
            return _emit(6, f"agent not terminal (state={resolved})", as_json)

        gate_res = sam_proc.launch_gate(
            name=agent_name,
            task=str(task_path),
            model=model,
            kind="retry" if infra_retry else "resume",
            override_reason=override_reason,
            no_space=no_space,
            is_infra_retry=infra_retry,
        )
        if not gate_res["granted"]:
            return sam_proc.emit_gate_rejection(gate_res, as_json)

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
                    # Infra-retry relaunches ride sam/retry.py's clock; plain
                    # resume on a queued run must not race that clock.
                    infra_retry = bool(getattr(args, "_infra_retry", False))
                    if resolved == "awaiting_retry" and not infra_retry:
                        item = _queued_retry(agent_id)
                        if item is not None:
                            fires = _fmt_fires(item)
                            return _emit(5, f"already_queued{fires} "
                                            f"(sam retry to fire now, or --cancel)",
                                         as_json)
                        resolved = "failed"
                    if resolved not in sam_state.TERMINAL_STATES and resolved != "unknown":
                        return _emit(6, f"agent not terminal (state={resolved})", as_json)

                    # Validate continuation before allocating a run or mutating state.
                    session_path = agent.get("session_path")
                    if harness == "pi" and (
                            not session_path or not os.path.isfile(session_path)):
                        return _emit(1, f"session file not found: {session_path}", as_json)
                    if harness != sam_harness.resolve_harness(agent):
                        return _emit(1, "resume cannot change session harness", as_json)
                    # argv resume flag: agy without a usable pointer may only
                    # happen on an infra retry — fresh conversation, same
                    # agent and task. Plain resume still requires the pointer.
                    resume_flag = True
                    if harness == "agy" and not sam_harness.read_conversation_id(session_path):
                        if not infra_retry:
                            return _emit(1, "no valid conversation_id pointer; use spawn not resume", as_json)
                        resume_flag = False
                    h = sam_harness.get_harness(harness)
                    wrapper = sam_config.wrapper_path(harness=harness)
                    if not wrapper.is_file():
                        return _emit(1, "wrapper not installed; run sam init first", as_json)

                    # Check restart budget (infra retries are SAM-owned and
                    # bypass the operator restart budget; still counted).
                    max_restarts = int(config.get("defaults", {}).get("max_restarts", 1))
                    rc = agent.get("restart_count", 0)
                    if rc >= max_restarts and not infra_retry:
                        return _emit(7, f"max restarts ({max_restarts}) reached", as_json)

                    # Allocate new run
                    run_count = agent.get("run_count", 1) + 1
                    new_run_dir = sam_config.agents_dir() / agent_id / f"run-{run_count:03d}"
                    # Save tasks before the spawning transaction; legacy task_path
                    # records remain readable and their original files stay intact.
                    new_run_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
                    sam_util.retain_previous_task(agent)
                    sam_task_path = new_run_dir / "task.md"
                    sam_util.snapshot_task_file(task_path, sam_task_path)

                    # Update registry: harness decides the session path.
                    # pi resume preserves the session file (history continues);
                    # agy resume preserves the conversation_id pointer.
                    prev_session = agent.get("session_path")
                    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    agent["state"] = "spawning"
                    agent["pid"] = None
                    agent["pgid"] = None
                    agent["pid_start_time"] = None
                    agent["exit_code"] = None
                    agent["exit_signal"] = None
                    agent["killed_reason"] = None
                    agent["duration_ms"] = None
                    for key in ("ended_at", "started_at", "completed_at"):
                        agent.pop(key, None)
                    agent["run_id"] = run_count
                    agent["run_count"] = run_count
                    agent["restart_count"] = rc + 1
                    # session_path via harness.resume (preserves history/pointer)
                    agent["session_path"] = h.resume_session(
                        prev_session, new_run_dir, for_resume=True)
                    agent["log_path"] = str(new_run_dir / "output.log")
                    agent["result_path"] = str(new_run_dir / "result.json")
                    agent["task_path"] = str(sam_task_path)
                    agent["launch_deadline_at"] = (
                        datetime.now(timezone.utc) + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    # model can be updated
                    agent["model"] = model
                    agent["harness"] = harness
                    # Reasoning overrides apply to the continued run. Resume
                    # never changes harness, so only the active harness's
                    # setting is stored and the other's is cleared.
                    if harness == "agy":
                        agent["effort"] = effort
                        agent["thinking"] = None
                    else:
                        agent["thinking"] = thinking
                        agent["effort"] = None
                    agent["run_started_at"] = now_str
                    agent["updated_at"] = now_str

                    sam_registry.save_registry(reg)

        except sam_locks.LockTimeout:
            return _emit(8, f"could not acquire lock for '{agent_name}'", as_json)

        # Build argv and env (same as spawn)
        argv = sam_harness.get_harness(harness).build_argv(
            wrapper,
            agent_id,
            model,
            agent["session_path"],
            str(sam_task_path),
            agent["result_path"],
            thinking=thinking if harness != "agy" else None,
            effort=effort if harness == "agy" else None,
            resume=resume_flag,
        )

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
            sam_proc.record_launch(
                agent_id=agent_id,
                run_id=run_count,
                kind="retry" if infra_retry else "resume",
                bypassed=gate_res.get("bypassed", False),
                fail_open=gate_res.get("fail_open", False),
                model=model,
                name=agent_name,
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
                        a["launch_deadline_at"] = None
                        sam_util.wake_archived_agent(a)
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
            "session_continuation_requested": resume_flag,
        }
        if infra_retry:
            result["infra_retry"] = True
        if as_json:
            print(json.dumps(result))
        else:
            print(f"Resumed agent {agent_id} (run {run_count}, pid {proc.pid})")
        return 0

    except sam_locks.LockTimeout as e:
        return _emit(8, f"lock timeout: {e}", as_json)
    except Exception as e:
        return _emit(1, str(e), as_json)


def _queued_retry(agent_id):
    try:
        from sam import retry as sam_retry
        return sam_retry.find_for(agent_id)
    except Exception:
        return None


def _fmt_fires(item):
    if not item:
        return ""
    try:
        ts = datetime.fromtimestamp(item["not_before"], timezone.utc)
        return f" (fires ~{ts.strftime('%H:%M:%SZ')})"
    except (KeyError, TypeError, ValueError, OSError):
        return ""


def _emit(code, message, as_json):
    if as_json:
        print(json.dumps({"status": "error", "code": code, "message": message}),
              file=sys.stderr)
    else:
        print(f"sam: {message}", file=sys.stderr)
    return code
