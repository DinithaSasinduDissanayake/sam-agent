#!/usr/bin/env python3
"""Tests for concurrent spawn limiter contention and fail-open fix (F2).

Validates that when multiple processes simultaneously request a spawn slot,
proper flock waiting prevents contention-induced fail-open:
exactly one process is granted the slot, and the others receive deferrals
with retry_after_s > 10.
"""

import multiprocessing as mp
import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest
from sam import proc as sam_proc


def _barrier_worker(barrier, sam_home_str, idx, queue):
    os.environ["SAM_HOME"] = sam_home_str
    try:
        barrier.wait(timeout=5.0)
    except Exception as e:
        queue.put((idx, {"error": str(e)}))
        return
    res = sam_proc.acquire_spawn_slot(
        name=f"proc-agent-{idx}",
        task=f"/tmp/task-{idx}.md",
        model="test-model",
        wait_s=0,
    )
    queue.put((idx, res))


def run_one_round(tmp_path_factory, num_procs=4):
    sam_home = Path(tempfile.mkdtemp(prefix="sam-f2-"))
    try:
        barrier = mp.Barrier(num_procs)
        queue = mp.Queue()
        procs = []
        for i in range(num_procs):
            p = mp.Process(target=_barrier_worker, args=(barrier, str(sam_home), i, queue))
            p.start()
            procs.append(p)

        results = []
        for _ in range(num_procs):
            results.append(queue.get(timeout=10.0))

        for p in procs:
            p.join(timeout=5.0)

        granted = [r for idx, r in results if r.get("granted")]
        deferred = [r for idx, r in results if not r.get("granted")]

        assert len(granted) == 1, f"Expected exactly 1 granted, got {len(granted)}: {results}"
        assert len(deferred) == num_procs - 1, f"Expected {num_procs - 1} deferred, got {len(deferred)}"
        for d in deferred:
            assert d.get("retry_after_s", 0) > 10, f"Expected retry_after_s > 10, got {d}"
            assert not d.get("fail_open", False)
        assert not granted[0].get("fail_open", False)
    finally:
        shutil.rmtree(sam_home, ignore_errors=True)


def test_concurrent_spawn_slot_contention_no_fail_open():
    """Start 4 processes behind a Barrier calling acquire_spawn_slot(wait_s=0).

    Repeated 50 times against fresh SAM_HOMEs. Exactly 1 must be granted,
    and 3 must get retry_after_s > 10 without fail_open.
    """
    for _ in range(50):
        run_one_round(None, num_procs=4)
