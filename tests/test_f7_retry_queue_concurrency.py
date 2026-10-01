#!/usr/bin/env python3
"""Tests for F7: Retry queue concurrency, atomic writes, and corrupt file handling.

Verifies:
1. 20 concurrent threads doing enqueue/remove without losing updates or corrupting queue.
2. Corrupt queue file is renamed to retry_queue.corrupt-<ts>, surfaces in doctor, and is not wiped by status.
3. An orphaned awaiting_retry agent with no queue item is treated as failed and allowed to resume.
"""

import argparse
import io
import json
import os
import threading
import time
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import pytest

from sam import config as sam_config
from sam import registry as sam_registry
from sam import retry as sam_retry
from sam.commands import doctor as doctor_cmd, init_cmd, resume as resume_cmd, status as status_cmd


@pytest.fixture
def sam_env(tmp_path, monkeypatch):
    sam_home = tmp_path / "sam"
    sam_home.mkdir()
    monkeypatch.setenv("SAM_HOME", str(sam_home))
    init_cmd.run(argparse.Namespace(json=True, sam_home=str(sam_home), force=False, harness="pi"))
    return sam_home


def test_concurrent_enqueue_and_remove(sam_env):
    n_threads = 20
    threads = []
    errors = []

    def worker(idx):
        try:
            # Enqueue a permanent agent
            sam_retry.enqueue(f"perm-{idx}", f"name-perm-{idx}", "model-x", "rate_limit", time.time() + 300)

            # Enqueue a transient agent and remove it
            sam_retry.enqueue(f"temp-{idx}", f"name-temp-{idx}", "model-x", "rate_limit", time.time() + 300)
            removed = sam_retry.remove(f"temp-{idx}")
            assert removed is True
        except Exception as e:
            errors.append(e)

    for i in range(n_threads):
        t = threading.Thread(target=worker, args=(i,))
        threads.append(t)

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Errors occurred during concurrent operations: {errors}"

    items = sam_retry.load_queue()
    perm_ids = {i["agent_id"] for i in items}
    expected_ids = {f"perm-{i}" for i in range(n_threads)}
    assert perm_ids == expected_ids
    assert len(items) == n_threads


def test_corrupt_retry_queue_handling(sam_env):
    q_file = sam_retry.queue_path()
    q_file.parent.mkdir(parents=True, exist_ok=True)
    q_file.write_text("{this is corrupted invalid json content!!!")

    # load_queue raises and renames
    with pytest.raises(RuntimeError) as exc_info:
        sam_retry.load_queue()
    assert "retry queue corrupt" in str(exc_info.value)
    assert not q_file.exists()

    corrupt_files = list(q_file.parent.glob("retry_queue.corrupt-*"))
    assert len(corrupt_files) == 1

    # doctor surfaces the corrupt queue
    out = io.StringIO()
    with redirect_stdout(out):
        rc = doctor_cmd.run(argparse.Namespace(window=1.0, json=True, sam_home=str(sam_env)))
    assert rc == 0
    doc = json.loads(out.getvalue())
    report = doc.get("doctor", doc)
    assert len(report.get("corrupt_retry_queues", [])) >= 1

    # status does not wipe it
    out = io.StringIO()
    with redirect_stdout(out):
        status_cmd.run(argparse.Namespace(json=True, all=True, quiet=False, watch=None, id_or_name=None))
    corrupt_files_after = list(q_file.parent.glob("retry_queue.corrupt-*"))
    assert len(corrupt_files_after) == 1


def test_resume_orphaned_awaiting_retry_allowed(sam_env, monkeypatch):
    # Seed an agent in awaiting_retry but with no queue item
    agent_id = "agent-orphan"
    agent_dir = sam_env / "agents" / agent_id
    run_dir = agent_dir / "run-001"
    run_dir.mkdir(parents=True, exist_ok=True)
    task_file = agent_dir / "task.md"
    task_file.write_text("task content")
    session_file = agent_dir / "session.jsonl"
    session_file.write_text("session line\n")

    entry = {
        "id": agent_id,
        "name": "orphan",
        "harness": "pi",
        "model": "model-1",
        "task_path": str(task_file),
        "session_path": str(session_file),
        "created_at": "2026-10-01T12:00:00Z",
        "state": "awaiting_retry",
        "run_id": 1,
        "run_count": 1,
        "current_run_dir": str(run_dir),
    }
    reg = sam_registry.load_registry()
    reg["agents"].append(entry)
    sam_registry.save_registry(reg)

    # Mock Popen so we don't really spawn
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: argparse.Namespace(pid=os.getpid()))

    # Attempting resume should NOT return 5 (already_queued); it treats it as failed and proceeds
    new_task = sam_env / "new_task.md"
    new_task.write_text("continuation")
    args = argparse.Namespace(
        id_or_name=agent_id,
        task=str(new_task),
        model=None,
        harness=None,
        thinking=None,
        effort=None,
        no_space=True,
        json=True,
    )
    rc = resume_cmd.run(args)
    assert rc == 0
