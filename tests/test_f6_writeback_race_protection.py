#!/usr/bin/env python3
"""Tests for F6: Writeback race protection on unlocked snapshots.

Verifies:
Terminal states computed from an unlocked snapshot are NOT written back
if run_id or pid changed under the lock (e.g. concurrent resume or retry started run 2).
"""

import argparse
import contextlib
import json
import os
from pathlib import Path
import pytest

from sam import config as sam_config
from sam import locks as sam_locks
from sam import registry as sam_registry
from sam import retry as sam_retry
from sam.commands import init_cmd, kill as kill_cmd, status as status_cmd, wait as wait_cmd


@pytest.fixture
def sam_env(tmp_path, monkeypatch):
    sam_home = tmp_path / "sam"
    sam_home.mkdir()
    monkeypatch.setenv("SAM_HOME", str(sam_home))
    init_cmd.run(argparse.Namespace(json=True, sam_home=str(sam_home), force=False, harness="pi"))
    return sam_home


def _seed_agent(sam_home, agent_id, name, run_id=1, pid=1234, state="running"):
    agent_dir = sam_home / "agents" / agent_id
    run_dir = agent_dir / f"run-{run_id:03d}"
    run_dir.mkdir(parents=True, exist_ok=True)
    task_file = agent_dir / "task.md"
    task_file.write_text(f"task for {agent_id}")
    result_file = run_dir / "result.json"
    result_file.write_text(json.dumps({
        "status": "completed",
        "final_state_hint": "completed",
        "result": "done",
        "exit_code": 0,
    }))
    entry = {
        "id": agent_id,
        "name": name,
        "harness": "pi",
        "model": "model-1",
        "task_path": str(task_file),
        "created_at": "2026-10-01T12:00:00Z",
        "updated_at": "2026-10-01T12:00:00Z",
        "state": state,
        "run_id": run_id,
        "run_count": run_id,
        "current_run_dir": str(run_dir),
        "result_path": str(result_file),
        "pid": pid,
        "pgid": pid,
        "pid_start_time": 1000,
    }
    reg = sam_registry.load_registry()
    reg["agents"].append(entry)
    sam_registry.save_registry(reg)
    return entry


def test_status_writeback_does_not_clobber_concurrent_resume(sam_env, monkeypatch):
    agent = _seed_agent(sam_env, "agent-race", "race-agent", run_id=1, pid=1234, state="running")

    # Snapshot resolves run 1 as completed.
    updates = {"agent-race": ("completed", 1, 1234)}

    # Monkeypatch registry_lock to simulate concurrent resume before lock acquisition
    real_registry_lock = sam_locks.registry_lock

    @contextlib.contextmanager
    def race_lock(*args, **kwargs):
        with real_registry_lock(*args, **kwargs):
            # Concurrent resume bumped run_id and set running
            reg = sam_registry.load_registry()
            for a in reg["agents"]:
                if a["id"] == "agent-race":
                    a["run_id"] = 2
                    a["run_count"] = 2
                    a["pid"] = 5678
                    a["state"] = "running"
            sam_registry.save_registry(reg)
            yield

    monkeypatch.setattr(sam_locks, "registry_lock", race_lock)

    # Perform writeback
    status_cmd._writeback_terminals(updates)

    # Verify agent-race in registry is still running with run_id=2
    final_reg = sam_registry.load_registry()
    a = [x for x in final_reg["agents"] if x["id"] == "agent-race"][0]
    assert a["run_id"] == 2
    assert a["pid"] == 5678
    assert a["state"] == "running"


def test_reconcile_terminal_does_not_clobber_concurrent_run(sam_env):
    agent = _seed_agent(sam_env, "agent-race-2", "race-2", run_id=1, pid=1234, state="running")

    # Simulate that by the time reconcile_terminal acquires lock, run 2 is in registry
    reg = sam_registry.load_registry()
    for a in reg["agents"]:
        if a["id"] == "agent-race-2":
            a["run_id"] = 2
            a["pid"] = 5678
            a["state"] = "running"
    sam_registry.save_registry(reg)

    # Calling reconcile_terminal with snapshot info from run 1
    state, item = sam_retry.reconcile_terminal("agent-race-2", snap_run_id=1, snap_pid=1234, terminal_state="completed")

    # Assert it was rejected and left as running
    assert state == "running"
    final_reg = sam_registry.load_registry()
    a = [x for x in final_reg["agents"] if x["id"] == "agent-race-2"][0]
    assert a["run_id"] == 2
    assert a["state"] == "running"


def test_kill_does_not_clobber_concurrent_run(sam_env, monkeypatch):
    from sam import proc as sam_proc
    agent = _seed_agent(sam_env, "agent-race-3", "race-3", run_id=1, pid=os.getpid(), state="running")
    # Remove result.json so it stays running
    res_path = Path(agent["result_path"])
    if res_path.exists():
        res_path.unlink()
    reg = sam_registry.load_registry()
    for a in reg["agents"]:
        if a["id"] == "agent-race-3":
            a["pid_start_time"] = sam_proc.read_pid_start_time(os.getpid())
    sam_registry.save_registry(reg)

    real_registry_lock = sam_locks.registry_lock
    lock_count = 0

    @contextlib.contextmanager
    def race_lock_kill(*args, **kwargs):
        nonlocal lock_count
        lock_count += 1
        with real_registry_lock(*args, **kwargs):
            if lock_count == 2:
                # On second lock entry (re-entry to persist killed state),
                # simulate concurrent resume started run 2
                reg = sam_registry.load_registry()
                for a in reg["agents"]:
                    if a["id"] == "agent-race-3":
                        a["run_id"] = 2
                        a["pid"] = 8888
                        a["state"] = "running"
                sam_registry.save_registry(reg)
            yield

    monkeypatch.setattr(sam_locks, "registry_lock", race_lock_kill)
    monkeypatch.setattr(sam_locks, "name_lock", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr("sam.proc.proc_start_time_match", lambda pid, st: True)
    monkeypatch.setattr("sam.proc.killpg", lambda pgid, sig: None)
    monkeypatch.setattr("sam.proc.proc_alive", lambda pgid: False)

    rc = kill_cmd.run(argparse.Namespace(id_or_name="agent-race-3", json=True, quiet=False, force=False))
    assert rc == 0

    # Ensure run 2 was NOT clobbered to "killed"
    final_reg = sam_registry.load_registry()
    a = [x for x in final_reg["agents"] if x["id"] == "agent-race-3"][0]
    assert a["run_id"] == 2
    assert a["pid"] == 8888
    assert a["state"] == "running"


def test_single_agent_status_writeback_race_protection(sam_env, monkeypatch):
    agent = _seed_agent(sam_env, "agent-race-single", "race-single", run_id=1, pid=1234, state="running")

    real_registry_lock = sam_locks.registry_lock
    lock_count = 0

    @contextlib.contextmanager
    def race_lock(*args, **kwargs):
        nonlocal lock_count
        lock_count += 1
        with real_registry_lock(*args, **kwargs):
            if lock_count == 1:
                # Concurrent resume bumped run_id and set running before writeback
                reg = sam_registry.load_registry()
                for a in reg["agents"]:
                    if a["id"] == "agent-race-single":
                        a["run_id"] = 2
                        a["run_count"] = 2
                        a["pid"] = 5678
                        a["state"] = "running"
                sam_registry.save_registry(reg)
            yield

    monkeypatch.setattr(sam_locks, "registry_lock", race_lock)

    args = argparse.Namespace(
        id_or_name="race-single",
        name=None,
        agent=None,
        all=False,
        follow=False,
        json=True,
        detail=False,
        stall_seconds=300,
        watch=None,
    )
    status_cmd.run(args)

    final_reg = sam_registry.load_registry()
    a = [x for x in final_reg["agents"] if x["id"] == "agent-race-single"][0]
    assert a["run_id"] == 2
    assert a["pid"] == 5678
    assert a["state"] == "running"
