#!/usr/bin/env python3
"""SAM spawn — Spawn sub-agent via 12-step transaction.

Spec: reviews-phase-f-batch2.md — GLM-5.2 §1 + Grok-4.5 shared helpers
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_SAM_PKG = _THIS_DIR.parent
if str(_SAM_PKG) not in sys.path:
    sys.path.insert(0, str(_SAM_PKG))

from sam import config as sam_config
from sam import harness as sam_harness
from sam import locks as sam_locks
from sam import proc as sam_proc
from sam import registry as sam_registry
from sam import state as sam_state
from sam import util as sam_util

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _emit_error(code, message, as_json):
    if as_json:
        print(json.dumps({"status": "error", "code": code, "message": message}), file=sys.stderr)
    else:
        print(f"sam: {message}", file=sys.stderr)
    return code


def _fmt_eta(ts):
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).strftime(
            "%H:%M:%SZ")
    except (TypeError, ValueError, OSError):
        return "?"


def generate_agent_id():
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%d-%H%M%S")
    suffix = uuid.uuid4().hex[:6]
    return f"sam-{ts}-{suffix}"


def validate_spawn_inputs(args, config, harness=None):
    name = args.name
    if name is None or not _NAME_RE.match(name):
        raise ValueError(f"name must match {_NAME_RE.pattern}, got {name!r}")

    task = Path(args.task).expanduser().resolve()
    if not task.is_file():
        raise FileNotFoundError(f"task file not found: {task}")

    if harness is None:
        harness = sam_config.resolve_harness(getattr(args, "harness", None), config)
    model = sam_config.resolve_model(
        getattr(args, "model", None), harness, config)
    if not model:
        raise ValueError("no model configured")

    if args.cwd:
        cwd = Path(args.cwd).expanduser().resolve()
    else:
        cwd = task.parent.resolve()
    if not cwd.is_dir():
        raise NotADirectoryError(f"cwd is not a directory: {cwd}")

    return {"name": name, "task": task, "model": model, "cwd": str(cwd)}


def check_depth(config):
    try:
        depth = int(os.environ.get("SAM_DEPTH", "0"))
    except ValueError:
        depth = 0
    max_depth = int(config.get("defaults", {}).get("max_depth", 4))
    if depth >= max_depth:
        raise ValueError(f"max delegation depth ({max_depth}) reached")
    return depth


build_child_env = sam_util.build_child_env


def allocate_paths(paths, agent_id, run_id):
    run_name = f"run-{run_id:03d}"
    agent_dir = Path(paths["agents"]) / agent_id
    run_dir = agent_dir / run_name
    return {
        "agent_dir": str(agent_dir),
        "run_dir": str(run_dir),
        "log_path": str(run_dir / "output.log"),
        "result_path": str(run_dir / "result.json"),
        "session_path": str(agent_dir / "session.jsonl"),
        "task_path": str(run_dir / "task.md"),
    }


def run(args):
    as_json = getattr(args, "json", False)

    try:
        config = sam_config.load_config()
        try:
            harness = sam_config.resolve_harness(getattr(args, "harness", None), config)
        except ValueError as e:
            return _emit_error(2, str(e), as_json)
        thinking = getattr(args, "thinking", None)
        effort = getattr(args, "effort", None)
        flag_error = sam_harness.validate_reasoning_flags(harness, thinking, effort)
        if flag_error:
            return _emit_error(2, flag_error, as_json)
        inputs = validate_spawn_inputs(args, config, harness)
        depth = check_depth(config)

        name = inputs["name"]
        task_path = inputs["task"]
        model = inputs["model"]
        cwd = inputs["cwd"]
        parent_id = os.environ.get("SAM_AGENT_ID")
        root_id = os.environ.get("SAM_ROOT_ID")

        # Global launch-spacing guard (IISA review item 1): steady-state
        # headcount is fine, same-instant spawn bursts kill. Enforced in
        # code because nested spawners ignore skill prose. Blocks up to
        # ~45 s for a slot, then defers with re-run instructions.
        no_space = bool(getattr(args, "no_space", False))
        override_reason = (getattr(args, "override_reason", None)
                           or "").strip() or None

        from sam import retry as sam_retry

        # Same-name backstop: a queued infra-retry owns that name until it
        # fires or is cancelled (the registry entry is terminal, so the
        # active-name check below would not catch it).
        pending = sam_retry.find_for_name(name)
        if pending:
            return _emit_error(
                5,
                f"already_queued: '{name}' has a pending infra-retry "
                f"(fires ~{_fmt_eta(pending.get('not_before'))}). "
                f"Wait for it, cancel with `sam retry {name} --cancel`, "
                f"or force a different name.",
                as_json)

        gate_res = sam_proc.launch_gate(
            name=name,
            task=str(task_path),
            model=model,
            kind="spawn",
            override_reason=override_reason,
            no_space=no_space,
            is_infra_retry=False,
        )
        if not gate_res["granted"]:
            return sam_proc.emit_gate_rejection(gate_res, as_json)

        spawn_waited_s = gate_res["waited_s"]
        spacing_bypassed = gate_res["bypassed"]
        spawn_fail_open = gate_res["fail_open"]

        # 1-15: Lock sequence
        try:
            with sam_locks.name_lock(name, timeout=10):
                with sam_locks.registry_lock(exclusive=True, timeout=10):
                    registry = sam_registry.load_registry()
                    agents = registry.get("agents", [])

                    # Check name collision
                    active = sam_registry.find_active_by_name(
                        agents, name, sam_state.TERMINAL_STATES)
                    if active:
                        if as_json:
                            print(json.dumps({
                                "status": "error", "code": 2,
                                "message": f"name already active: {name}"
                            }), file=sys.stderr)
                        else:
                            print(f"sam: name already active: {name}", file=sys.stderr)
                        return 2

                    agent_id = generate_agent_id()
                    run_id = 1
                    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    # Spawning recovery window: now + 30s (a worker that has
                    # not persisted its PID by the deadline resolves as
                    # failed, not stuck-spawning forever).
                    deadline = (datetime.now(timezone.utc) + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")

                    s_paths = {
                        "agents": str(sam_config.agents_dir()),
                        "tasks": str(sam_config.tasks_dir()),
                    }
                    paths = allocate_paths(s_paths, agent_id, run_id)

                    entry = {
                        "id": agent_id, "name": name, "parent_id": parent_id,
                        "root_id": root_id, "depth": depth,
                        "run_id": run_id, "model": model,
                        "harness": harness,
                        "thinking": thinking, "effort": effort,
                        "state": "spawning", "pid": None, "pgid": None,
                        "pid_start_time": None,
                        "log_path": paths["log_path"],
                        "result_path": paths["result_path"],
                        "session_path": paths["session_path"],
                        "task_path": paths["task_path"],
                        "cwd": cwd,
                        "created_at": now_str, "updated_at": now_str,
                        "run_started_at": now_str,
                        "exit_code": None, "exit_signal": None,
                        "duration_ms": None, "restart_count": 0,
                        "killed_reason": None,
                        "launch_deadline_at": deadline,
                        "spawn_waited_s": spawn_waited_s,
                        "spacing_bypassed": spacing_bypassed,
                        "fail_open": spawn_fail_open,
                        "quota_override_reason": override_reason,
                }
                    registry["agents"].append(entry)
                    sam_registry.save_registry(registry)

        except sam_locks.LockTimeout:
            return _emit_error(8, f"could not acquire lock for '{name}'", as_json)

        # 16-29: Post-lock operations
        try:
            run_dir = Path(paths["run_dir"])
            run_dir.mkdir(mode=0o700, parents=True, exist_ok=False)

            # Copy task file
            dest_task = Path(paths["task_path"])
            sam_util.snapshot_task_file(task_path, dest_task)

            # Find and validate wrapper
            wrapper = sam_config.wrapper_path(harness=harness)
            if not wrapper.is_file():
                return _emit_error(1, "wrapper not installed; run sam init first", as_json)
            if wrapper.resolve().name not in set(sam_config.HARNESS_WRAPPERS.values()):
                return _emit_error(1, "allowlist validation failed", as_json)

            argv = sam_harness.get_harness(harness).build_argv(
                wrapper, agent_id, model, paths["session_path"],
                paths["task_path"], paths["result_path"],
                thinking=thinking, effort=effort, resume=False)

            env = build_child_env(agent_id, model, depth, harness=harness)
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
                run_id=run_id,
                kind="spawn",
                bypassed=spacing_bypassed,
                fail_open=spawn_fail_open,
                model=model,
                name=name,
                harness=harness,
            )

            # Update registry to running
            with sam_locks.registry_lock(exclusive=True, timeout=10):
                registry = sam_registry.load_registry()
                for a in registry["agents"]:
                    if a["id"] == agent_id:
                        a["state"] = "running"
                        a["pid"] = proc.pid
                        a["pgid"] = proc.pid
                        a["pid_start_time"] = sam_proc.read_pid_start_time(proc.pid)
                        a["launch_deadline_at"] = None
                        a["updated_at"] = datetime.now(timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%SZ")
                        break
                sam_registry.save_registry(registry)

        except Exception as e:
            # Popen or post-Popen failed — mark as failed
            try:
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
            except Exception:
                pass
            return _emit_error(1, f"spawn failed: {e}", as_json)

        result = {
            "status": "ok",
            "agent_id": agent_id,
            "name": name,
            "pid": proc.pid,
        }
        if as_json:
            print(json.dumps(result))
        else:
            print(f"Spawned agent {agent_id} (pid {proc.pid})")
        return 0

    except (ValueError, FileNotFoundError, NotADirectoryError) as e:
        return _emit_error(1, str(e), as_json)
    except Exception as e:
        return _emit_error(1, str(e), as_json)
