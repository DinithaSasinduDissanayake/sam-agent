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
