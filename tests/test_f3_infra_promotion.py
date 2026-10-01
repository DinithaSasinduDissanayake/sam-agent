#!/usr/bin/env python3
"""Tests for infra failure promotion in wait and spawn breaker reconciliation (F3).

Verifies that:
(a) Running `sam wait` on a dead agent whose result.json has a 429 quota error
    promotes the agent to `awaiting_retry`, returns exit 0, and enqueues to retry queue.
(b) Without calling status or wait, `sam spawn` with the same model reconciles the
    unreconciled 429 result, trips the circuit breaker, and exits 6 (quota_window).
"""

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

from sam import config as sam_config
from sam import registry as sam_registry
from sam import retry as sam_retry
from sam.commands import init_cmd, spawn as spawn_cmd, wait as wait_cmd


@pytest.fixture
def sam_env(tmp_path, monkeypatch):
    sam_home = tmp_path / "sam"
    sam_home.mkdir()
    monkeypatch.setenv("SAM_HOME", str(sam_home))
    init_cmd.run(argparse.Namespace(json=True, sam_home=str(sam_home), force=False, harness="pi"))
    return sam_home


def _seed_agent(sam_home, agent_id, name, model, state="running"):
    agent_dir = sam_home / "agents" / agent_id
    run_dir = agent_dir / "run-001"
    run_dir.mkdir(parents=True, exist_ok=True)
    task_file = agent_dir / "task.md"
    task_file.write_text("sample task")
    session_file = agent_dir / "session.jsonl"
    session_file.write_text("session\n")
    result_file = run_dir / "result.json"

    # Dead PID (999999) so liveness checks treat it as stopped
    reg = sam_registry.load_registry()
    entry = {
        "id": agent_id,
        "name": name,
        "harness": "agy",
        "model": model,
        "state": state,
        "pid": 999999,
        "pgid": 999999,
        "pid_start_time": 1000,
        "session_path": str(session_file),
        "task_path": str(task_file),
        "result_path": str(result_file),
        "run_count": 1,
        "run_id": 1,
        "created_at": "2026-10-01T12:00:00Z",
        "updated_at": "2026-10-01T12:00:00Z",
        "depth": 0,
    }
    reg["agents"].append(entry)
    sam_registry.save_registry(reg)
    return result_file


def test_wait_promotes_429_to_awaiting_retry(sam_env, capsys):
    res_path = _seed_agent(sam_env, "agent-429-wait", "test-429-wait", "gemini-flash")
    res_path.write_text(json.dumps({
        "exit_code": 1,
        "status": "failed",
        "final_state_hint": "failed",
        "duration_ms": 2500,
        "error": "RESOURCE_EXHAUSTED Individual quota reached. Resets in 0m30s"
    }))

    args = argparse.Namespace(
        id_or_name="test-429-wait",
        name=None,
        agent=None,
        timeout=0,
        kill_after=None,
        json=True,
    )
    rc = wait_cmd.run(args)
    assert rc == 0, "wait on infra failure should exit 0 with awaiting_retry"

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["status"] == "awaiting_retry"
    assert data["exit_code"] == 0

    # Registry assertion
    reg = sam_registry.load_registry()
    agent = next(a for a in reg["agents"] if a["id"] == "agent-429-wait")
    assert agent["state"] == "awaiting_retry"
    assert "retry_not_before" in agent

    # Queue assertion
    queue_item = sam_retry.find_for("agent-429-wait")
    assert queue_item is not None
    assert queue_item["kind"] == "quota"


def test_spawn_reconciles_and_trips_breaker(sam_env, capsys, tmp_path):
    res_path = _seed_agent(sam_env, "agent-429-spawn", "test-429-spawn", "gemini-breaker")
    res_path.write_text(json.dumps({
        "exit_code": 1,
        "status": "failed",
        "final_state_hint": "failed",
        "duration_ms": 1500,
        "error": "RESOURCE_EXHAUSTED Individual quota reached. Resets in 0m45s"
    }))

    task_file = tmp_path / "fresh_task.md"
    task_file.write_text("fresh task")

    # Call spawn with same model without any intermediate status or wait call
    args = argparse.Namespace(
        name="fresh-agent",
        task=str(task_file),
        harness="pi",
        model="gemini-breaker",
        thinking=None,
        effort=None,
        cwd=None,
        no_space=False,
        json=True,
        sam_home=str(sam_env),
    )
    rc = spawn_cmd.run(args)
    assert rc == 6, f"Expected spawn exit 6 (quota window deferral), got {rc}"

    captured = capsys.readouterr()
    err_data = json.loads(captured.err)
    assert err_data["status"] == "deferred"
    assert err_data["reason"] == "quota_window"
    assert err_data["model"] == "gemini-breaker"

    # Registry and queue of the dead agent must now be reconciled to awaiting_retry
    reg = sam_registry.load_registry()
    agent = next(a for a in reg["agents"] if a["id"] == "agent-429-spawn")
    assert agent["state"] == "awaiting_retry"
    assert sam_retry.find_for("agent-429-spawn") is not None
