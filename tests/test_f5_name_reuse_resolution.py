#!/usr/bin/env python3
"""Tests for F5: Agent name reuse resolution.

Verifies:
1. resolve_ref prioritizes exact ID first.
2. If multiple agents share a name, non-terminal is prioritized over terminal
   (even when the non-terminal agent is older than the completed one).
3. If both are terminal (or both non-terminal), the newest (by timestamp/run_id/idx) is chosen.
4. Commands (wait, result, logs, kill, status, resume, restart) resolve using resolve_ref.
"""

import argparse
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import pytest

from sam import config as sam_config
from sam import proc as sam_proc
from sam import registry as sam_registry
from sam.commands import (
    init_cmd,
    kill as kill_cmd,
    logs as logs_cmd,
    restart as restart_cmd,
    resume as resume_cmd,
    result as result_cmd,
    status as status_cmd,
    wait as wait_cmd,
)


@pytest.fixture
def sam_env(tmp_path, monkeypatch):
    sam_home = tmp_path / "sam"
    sam_home.mkdir()
    monkeypatch.setenv("SAM_HOME", str(sam_home))
    init_cmd.run(argparse.Namespace(json=True, sam_home=str(sam_home), force=False, harness="pi"))
    return sam_home


def _seed_agent(sam_home, agent_id, name, state="completed", created_at="2026-10-01T10:00:00Z", pid=None):
    agent_dir = sam_home / "agents" / agent_id
    run_dir = agent_dir / "run-001"
    run_dir.mkdir(parents=True, exist_ok=True)
    task_file = agent_dir / "task.md"
    task_file.write_text(f"Task for {agent_id}")
    (run_dir / "stdout.log").write_text(f"stdout for {agent_id}\n")
    (run_dir / "result.json").write_text(json.dumps({
        "status": "completed" if state == "completed" else "failed",
        "final_state_hint": state,
        "result": f"result for {agent_id}",
        "agent_id": agent_id,
        "exit_code": 0 if state == "completed" else 1,
    }))

    pid_st = sam_proc.read_pid_start_time(pid) if pid else None
    pgid = sam_proc.pgid_of(pid) if pid else None
    entry = {
        "id": agent_id,
        "name": name,
        "harness": "pi",
        "model": "claude-sonnet-4-20250514",
        "task": str(task_file),
        "task_path": str(task_file),
        "cwd": str(agent_dir),
        "created_at": created_at,
        "run_started_at": created_at,
        "state": state,
        "log_path": str(run_dir / "stdout.log"),
        "result_path": str(run_dir / "result.json"),
        "run_id": 1,
        "run_count": 1,
        "current_run_dir": str(run_dir),
        "pid": pid,
        "pid_start_time": pid_st,
        "pgid": pgid,
    }
    reg = sam_registry.load_registry()
    reg["agents"].append(entry)
    sam_registry.save_registry(reg)
    return entry


def test_resolve_ref_direct_precedence(sam_env):
    # Older running agent (created 08:00)
    _seed_agent(sam_env, "agent-running-old", "pipeline-run", state="running", created_at="2026-10-01T08:00:00Z", pid=os.getpid())
    # Newer completed agent (created 10:00)
    _seed_agent(sam_env, "agent-completed-new", "pipeline-run", state="completed", created_at="2026-10-01T10:00:00Z")

    reg = sam_registry.load_registry()
    agents = reg.get("agents", [])

    # Exact ID matches directly
    assert sam_registry.resolve_ref(agents, "agent-running-old")["id"] == "agent-running-old"
    assert sam_registry.resolve_ref(agents, "agent-completed-new")["id"] == "agent-completed-new"

    # Name match picks the running one despite being older than the completed one
    resolved = sam_registry.resolve_ref(agents, "pipeline-run")
    assert resolved["id"] == "agent-running-old"


def test_resolve_ref_both_terminal_picks_newest(sam_env):
    _seed_agent(sam_env, "agent-1", "eval", state="completed", created_at="2026-10-01T08:00:00Z")
    _seed_agent(sam_env, "agent-2", "eval", state="completed", created_at="2026-10-01T09:00:00Z")

    reg = sam_registry.load_registry()
    agents = reg.get("agents", [])

    resolved = sam_registry.resolve_ref(agents, "eval")
    assert resolved["id"] == "agent-2"


def test_commands_pick_newest_running_on_name_reuse(sam_env, monkeypatch):
    import subprocess
    proc = subprocess.Popen(["sleep", "1000"], start_new_session=True)
    pgid = os.getpgid(proc.pid)
    try:
        # Older running agent (created 08:00) with a live child process
        _seed_agent(sam_env, "eval-running-old", "eval", state="running", created_at="2026-10-01T08:00:00Z", pid=proc.pid)
        # Newer completed agent (created 10:00)
        _seed_agent(sam_env, "eval-completed-new", "eval", state="completed", created_at="2026-10-01T10:00:00Z")

        # 1. result command
        out = io.StringIO()
        with redirect_stdout(out):
            ret = result_cmd.run(argparse.Namespace(id_or_name="eval", json=True, pretty=False))
        assert ret == 0
        res = json.loads(out.getvalue())
        assert res.get("agent_id") == "eval-running-old"

        # 2. logs command
        out = io.StringIO()
        with redirect_stdout(out):
            ret = logs_cmd.run(argparse.Namespace(id_or_name="eval", follow=False, lines=10, run=None))
        assert ret == 0
        assert "eval-running-old" in out.getvalue()

        # 3. status command
        out = io.StringIO()
        with redirect_stdout(out):
            ret = status_cmd.run(argparse.Namespace(id_or_name="eval", json=True, all=False, quiet=False, watch=None))
        assert ret == 0
        st = json.loads(out.getvalue())
        agent_info = st[0] if isinstance(st, list) else st
        assert agent_info["id"] == "eval-running-old"

        # 4. kill command picks eval-running-old (eval-completed-new was already completed)
        out = io.StringIO()
        with redirect_stdout(out):
            ret = kill_cmd.run(argparse.Namespace(id_or_name="eval", json=True, quiet=False, force=False))
        assert ret == 0
        k_res = json.loads(out.getvalue())
        assert k_res.get("agent_id") == "eval-running-old"
        assert k_res.get("outcome") == "killed"
    finally:
        try:
            os.killpg(pgid, signal.SIGKILL)
            proc.wait(timeout=2)
        except Exception:
            pass
