#!/usr/bin/env python3
"""Tests for unified launch gate, retry --due spacing, and doctor launches.jsonl audit (F4).

Verifies that:
1. `retry --due` respects spacing via launch_gate: subsequent due items do not burst
   concurrently; items 2-3 are deferred when spacing is held.
2. `launches.jsonl` records all launches (spawn, resume, restart, retry) with float ts.
3. `sam doctor` reads `launches.jsonl` and reports a resume 2s after a spawn as a VIOLATION.
"""

import argparse
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sam import config as sam_config
from sam import proc as sam_proc
from sam import registry as sam_registry
from sam import retry as sam_retry
from sam.commands import doctor as doctor_cmd
from sam.commands import init_cmd
from sam.commands import retry as retry_cmd
from sam.commands import spawn as spawn_cmd


@pytest.fixture
def sam_home(tmp_path, monkeypatch):
    sh = tmp_path / "sam"
    sh.mkdir()
    monkeypatch.setenv("SAM_HOME", str(sh))
    init_cmd.run(argparse.Namespace(json=True, sam_home=str(sh), force=False, harness="pi"))
    return sh


def _seed_agent_with_session(sam_home, agent_id, name, model="pi-model"):
    agent_dir = sam_home / "agents" / agent_id
    run_dir = agent_dir / "run-001"
    run_dir.mkdir(parents=True, exist_ok=True)
    task_file = agent_dir / "task.md"
    task_file.write_text("sample task")
    session_file = agent_dir / "session.jsonl"
    session_file.write_text("session line\n")
    result_file = run_dir / "result.json"
    result_file.write_text(json.dumps({
        "status": "failed",
        "exit_code": 1,
        "error": "RESOURCE_EXHAUSTED 429",
        "final_state_hint": "failed",
    }))

    reg = sam_registry.load_registry()
    entry = {
        "id": agent_id,
        "name": name,
        "harness": "pi",
        "model": model,
        "state": "awaiting_retry",
        "pid": None,
        "pgid": None,
        "session_path": str(session_file),
        "task_path": str(task_file),
        "result_path": str(result_file),
        "current_run_dir": str(run_dir),
        "run_count": 1,
        "run_id": 1,
        "created_at": "2026-10-01T12:00:00Z",
        "updated_at": "2026-10-01T12:00:00Z",
        "depth": 0,
    }
    reg["agents"].append(entry)
    sam_registry.save_registry(reg)
    return entry


def test_retry_due_spacing_and_launches_log(sam_home, monkeypatch):
    monkeypatch.setenv("SAM_SLOT_WAIT_S", "0")

    # Seed 3 agents and enqueue all 3 as due in the past
    now = time.time()
    for i in (1, 2, 3):
        aid = f"agent-due-{i}"
        name = f"due-agent-{i}"
        _seed_agent_with_session(sam_home, aid, name)
        sam_retry.enqueue(aid, name, "pi-model", "quota", not_before=now - 100 + i)

    class DummyProc:
        pid = 88888
        def poll(self):
            return None

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: DummyProc())

    # Run retry --due
    args = argparse.Namespace(
        id_or_name=None,
        name=None,
        due=True,
        cancel=False,
        override_reason=None,
        json=False,
    )
    rc = retry_cmd.run(args)
    # The first item fired (rc 0), items 2 and 3 deferred due to spacing
    assert rc == 0

    # Verify launches.jsonl
    launches = sam_proc.load_launches(sam_home)
    assert len(launches) == 1, f"Expected 1 launch fired, got {len(launches)}"
    assert launches[0]["name"] == "due-agent-1"
    assert launches[0]["kind"] == "retry"

    # Queue should still contain items 2 and 3
    remaining = sam_retry.load_queue()
    remaining_ids = {r["agent_id"] for r in remaining}
    assert "agent-due-2" in remaining_ids
    assert "agent-due-3" in remaining_ids
    assert "agent-due-1" not in remaining_ids


def test_doctor_detects_resume_spacing_violation(sam_home, monkeypatch, capsys):
    # Seed an agent
    aid = "agent-doc-test"
    _seed_agent_with_session(sam_home, aid, "doc-test-agent")

    t0 = time.time() - 60.0
    # Record spawn launch at t0
    sam_proc.record_launch(aid, 1, "spawn", bypassed=False, fail_open=False,
                           model="test-m", name="doc-test-agent", ts=t0)
    # Record resume launch 2 seconds later (violates 15s spacing)
    sam_proc.record_launch(aid, 2, "resume", bypassed=False, fail_open=False,
                           model="test-m", name="doc-test-agent", ts=t0 + 2.0)

    report = doctor_cmd.collect(window_hours=1.0)
    assert len(report["spacing_violations"]) == 1
    viol = report["spacing_violations"][0]
    assert viol["kind"] == "resume"
    assert viol["gap_s"] == 2.0

    # Test text output
    args = argparse.Namespace(window=1.0, json=False)
    rc = doctor_cmd.run(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "VIOLATION" in out
    assert "[resume]" in out


def test_resume_enforces_cap_with_four_running_agents(sam_home, monkeypatch, capsys):
    monkeypatch.setenv("SAM_SLOT_WAIT_S", "0")
    from sam.commands import resume as resume_cmd
    # Seed 4 live running agents
    reg = sam_registry.load_registry()
    for i in range(1, 5):
        aid = f"live-agent-{i}"
        entry = {
            "id": aid,
            "name": f"live-{i}",
            "harness": "pi",
            "model": "model-1",
            "state": "running",
            "pid": os.getpid(),
            "pgid": os.getpid(),
            "pid_start_time": sam_proc.read_pid_start_time(os.getpid()),
            "run_count": 1,
            "run_id": 1,
            "created_at": "2026-10-01T12:00:00Z",
            "updated_at": "2026-10-01T12:00:00Z",
        }
        reg["agents"].append(entry)
    sam_registry.save_registry(reg)

    # Seed an agent to resume
    _seed_agent_with_session(sam_home, "target-resume", "target-agent")
    reg = sam_registry.load_registry()
    for a in reg["agents"]:
        if a["id"] == "target-resume":
            a["state"] = "failed"
    sam_registry.save_registry(reg)

    args = argparse.Namespace(
        id_or_name="target-agent",
        name=None,
        agent=None,
        task=str(sam_home / "agents" / "target-resume" / "task.md"),
        harness="pi",
        model=None,
        thinking=None,
        effort=None,
        cwd=None,
        no_space=False,
        override_reason=None,
        json=True,
    )
    rc = resume_cmd.run(args)
    assert rc == 6, f"Expected exit 6 (cap deferral), got {rc}"
    captured = capsys.readouterr()
    err_data = json.loads(captured.err)
    assert err_data["status"] == "deferred"
    assert "cap" in err_data["reason"]


def test_retry_due_continues_past_errors_and_dead_letters(sam_home, monkeypatch, capsys):
    monkeypatch.setenv("SAM_SLOT_WAIT_S", "0")
    now = time.time()

    # Agent 1: broken (missing session file so pi resume fails with rc 1)
    _seed_agent_with_session(sam_home, "agent-broken", "broken-agent")
    os.remove(sam_home / "agents" / "agent-broken" / "session.jsonl")
    sam_retry.enqueue("agent-broken", "broken-agent", "pi-model", "quota", not_before=now - 50)

    # Agent 2: valid agent that should fire
    _seed_agent_with_session(sam_home, "agent-ok", "ok-agent")
    sam_retry.enqueue("agent-ok", "ok-agent", "pi-model", "quota", not_before=now - 40)

    class DummyProc:
        pid = 77777
        def poll(self):
            return None

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: DummyProc())

    # Run retry --due
    args = argparse.Namespace(
        id_or_name=None,
        name=None,
        due=True,
        cancel=False,
        override_reason=None,
        json=True,
    )
    rc = retry_cmd.run(args)
    # ok-agent should have fired despite broken-agent failing before it
    launches = sam_proc.load_launches(sam_home)
    launched_names = [l["name"] for l in launches]
    assert "ok-agent" in launched_names, f"Expected ok-agent to fire, launched: {launched_names}"

    # Verify agent-broken had attempt recorded
    queue = sam_retry.load_queue()
    b_item = next(i for i in queue if i["agent_id"] == "agent-broken")
    assert b_item.get("attempts", 0) == 1
    assert b_item.get("last_error") is not None

    # Call --due 2 more times to trigger dead-lettering (after 3 attempts)
    retry_cmd.run(args)
    retry_cmd.run(args)

    # agent-broken should now be removed from queue and moved to dead
    queue_after = sam_retry.load_queue()
    assert not any(i["agent_id"] == "agent-broken" for i in queue_after)
    dead = sam_retry.load_dead()
    assert any(d["agent_id"] == "agent-broken" for d in dead)
