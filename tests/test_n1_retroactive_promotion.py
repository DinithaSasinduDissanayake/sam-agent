#!/usr/bin/env python3
"""Tests for N1: retroactive promotion prevention, recency guard, migration epoch."""

import argparse
import json
import time
from datetime import datetime, timezone, timedelta
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


def _seed_agent(sam_home, agent_id, name, model, state, ended_at=None, created_at=None):
    agent_dir = sam_home / "agents" / agent_id
    run_dir = agent_dir / "run-001"
    run_dir.mkdir(parents=True, exist_ok=True)
    task_file = agent_dir / "task.md"
    task_file.write_text("sample task")
    session_file = agent_dir / "session.jsonl"
    session_file.write_text("session\n")
    result_file = run_dir / "result.json"

    res_data = {
        "exit_code": 1,
        "status": "failed",
        "final_state_hint": "failed",
        "duration_ms": 2500,
        "error": "RESOURCE_EXHAUSTED Individual quota reached. Resets in 0m30s",
    }
    if ended_at is not None:
        res_data["ended_at"] = ended_at
    result_file.write_text(json.dumps(res_data))

    reg = sam_registry.load_registry()
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    c_at = created_at or now_iso
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
        "created_at": c_at,
        "updated_at": c_at,
        "depth": 0,
    }
    reg["agents"].append(entry)
    sam_registry.save_registry(reg)
    return result_file


def test_historical_failed_agent_not_promoted_by_spawn_or_wait(sam_env, capsys, tmp_path):
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _seed_agent(
        sam_env, "agent-failed-yesterday", "failed-yesterday",
        model="gemini-flash", state="failed",
        ended_at=yesterday, created_at=yesterday,
    )

    # Calling spawn for a different agent with the same model
    task_file = tmp_path / "new_task.md"
    task_file.write_text("new task")
    args = argparse.Namespace(
        name="fresh-agent",
        task=str(task_file),
        harness="pi",
        model="gemini-flash",
        thinking=None,
        effort=None,
        cwd=None,
        no_space=True,
        json=True,
        sam_home=str(sam_env),
    )
    rc = spawn_cmd.run(args)
    assert rc != 6, f"Spawn should not be deferred by historical failure, got rc={rc}"
    assert sam_retry.find_for("agent-failed-yesterday") is None

    # Call wait on the historical agent
    w_args = argparse.Namespace(
        id_or_name="failed-yesterday",
        name=None,
        agent=None,
        timeout=0,
        kill_after=None,
        json=True,
    )
    wait_cmd.run(w_args)
    reg = sam_registry.load_registry()
    agent = next(a for a in reg["agents"] if a["id"] == "agent-failed-yesterday")
    assert agent["state"] == "failed"
    assert sam_retry.find_for("agent-failed-yesterday") is None


def test_killed_and_retry_cancelled_not_repromoted_on_wait(sam_env, capsys):
    _seed_agent(
        sam_env, "agent-killed", "killed-agent",
        model="gemini-flash", state="killed",
    )
    _seed_agent(
        sam_env, "agent-cancelled", "cancelled-agent",
        model="gemini-flash", state="killed",
    )
    reg = sam_registry.load_registry()
    for a in reg["agents"]:
        if a["id"] == "agent-cancelled":
            a["killed_reason"] = "retry_cancelled"
    sam_registry.save_registry(reg)

    # Wait on killed agent
    args_killed = argparse.Namespace(
        id_or_name="killed-agent",
        name=None,
        agent=None,
        timeout=0,
        kill_after=None,
        json=True,
    )
    wait_cmd.run(args_killed)
    reg = sam_registry.load_registry()
    ak = next(a for a in reg["agents"] if a["id"] == "agent-killed")
    assert ak["state"] == "killed"
    assert sam_retry.find_for("agent-killed") is None

    # Wait on retry_cancelled agent
    args_cancelled = argparse.Namespace(
        id_or_name="cancelled-agent",
        name=None,
        agent=None,
        timeout=0,
        kill_after=None,
        json=True,
    )
    wait_cmd.run(args_cancelled)
    reg = sam_registry.load_registry()
    ac = next(a for a in reg["agents"] if a["id"] == "agent-cancelled")
    assert ac["state"] == "killed"
    assert ac.get("killed_reason") == "retry_cancelled"
    assert sam_retry.find_for("agent-cancelled") is None


def test_reconcile_epoch_protects_pre_epoch_runs(sam_env):
    # Set reconcile epoch to 1 hour ago
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1))
    epoch_file = sam_env / ".reconcile_epoch"
    epoch_file.write_text(one_hour_ago.strftime("%Y-%m-%dT%H:%M:%SZ"))

    # Seed an agent ended 90 minutes ago (before epoch, but within 2 hours)
    ninety_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _seed_agent(
        sam_env, "agent-pre-epoch", "pre-epoch",
        model="gemini-flash", state="running",
        ended_at=ninety_min_ago, created_at=ninety_min_ago,
    )

    promotions = sam_retry.reconcile_pending()
    assert len(promotions) == 0
    assert sam_retry.find_for("agent-pre-epoch") is None

