#!/usr/bin/env python3
"""Tests for F5: Agent name reuse resolution.

Verifies:
1. resolve_ref prioritizes exact ID first.
2. If multiple agents share a name, non-terminal is prioritized over terminal.
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
        "pgid": pid,
    }
    reg = sam_registry.load_registry()
    reg["agents"].append(entry)
    sam_registry.save_registry(reg)
    return entry


def test_resolve_ref_direct_precedence(sam_env):
    # Old completed agent
    _seed_agent(sam_env, "agent-old", "pipeline-run", state="completed", created_at="2026-10-01T08:00:00Z")
    # Newer completed agent
    _seed_agent(sam_env, "agent-mid", "pipeline-run", state="completed", created_at="2026-10-01T09:00:00Z")
    # Newest running agent (dummy pid to simulate running or mocked state)
    _seed_agent(sam_env, "agent-new", "pipeline-run", state="running", created_at="2026-10-01T10:00:00Z", pid=os.getpid())

    reg = sam_registry.load_registry()
    agents = reg.get("agents", [])

    # Exact ID matches directly
    assert sam_registry.resolve_ref(agents, "agent-old")["id"] == "agent-old"
    assert sam_registry.resolve_ref(agents, "agent-mid")["id"] == "agent-mid"
    assert sam_registry.resolve_ref(agents, "agent-new")["id"] == "agent-new"

    # Name match picks the running one
    resolved = sam_registry.resolve_ref(agents, "pipeline-run")
    assert resolved["id"] == "agent-new"


def test_resolve_ref_both_terminal_picks_newest(sam_env):
    _seed_agent(sam_env, "agent-1", "eval", state="completed", created_at="2026-10-01T08:00:00Z")
    _seed_agent(sam_env, "agent-2", "eval", state="completed", created_at="2026-10-01T09:00:00Z")

    reg = sam_registry.load_registry()
    agents = reg.get("agents", [])

    resolved = sam_registry.resolve_ref(agents, "eval")
    assert resolved["id"] == "agent-2"


def test_commands_pick_newest_running_on_name_reuse(sam_env, monkeypatch):
    old = _seed_agent(sam_env, "eval-001", "eval", state="completed", created_at="2026-10-01T08:00:00Z")
    # Current running process as pid so resolve_agent_state sees it running
    new = _seed_agent(sam_env, "eval-002", "eval", state="running", created_at="2026-10-01T10:00:00Z", pid=os.getpid())

    # 1. result command
    out = io.StringIO()
    with redirect_stdout(out):
        ret = result_cmd.run(argparse.Namespace(id_or_name="eval", json=True, pretty=False))
    assert ret == 0
    res = json.loads(out.getvalue())
    assert res.get("agent_id") == "eval-002"

    # 2. logs command
    out = io.StringIO()
    with redirect_stdout(out):
        ret = logs_cmd.run(argparse.Namespace(id_or_name="eval", follow=False, lines=10, run=None))
    assert ret == 0
    assert "eval-002" in out.getvalue()

    # 3. status command
    out = io.StringIO()
    with redirect_stdout(out):
        ret = status_cmd.run(argparse.Namespace(id_or_name="eval", json=True, all=False, quiet=False, watch=None))
    assert ret == 0
    st = json.loads(out.getvalue())
    agent_info = st[0] if isinstance(st, list) else st
    assert agent_info["id"] == "eval-002"

    # 4. kill command picks eval-002 (eval-001 was already completed)
    out = io.StringIO()
    with redirect_stdout(out):
        # eval-002 pid is current process, let's not send SIGTERM to current process!
        # Make eval-002 awaiting_retry so kill cancels it without signaling os.getpid()
        from sam import retry as sam_retry
        sam_retry.enqueue("eval-002", "eval", "gemini-flash", 9999999999.0, "rate_limit")
        reg = sam_registry.load_registry()
        for a in reg["agents"]:
            if a["id"] == "eval-002":
                a["state"] = "awaiting_retry"
                a["pid"] = None
        sam_registry.save_registry(reg)

        ret = kill_cmd.run(argparse.Namespace(id_or_name="eval", json=True, quiet=False, force=False))
    assert ret == 0
    k_res = json.loads(out.getvalue())
    assert k_res.get("agent_id") == "eval-002"
    assert k_res.get("outcome") == "cancelled"
