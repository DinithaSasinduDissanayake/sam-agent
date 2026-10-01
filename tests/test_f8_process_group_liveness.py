#!/usr/bin/env python3
"""Tests for F8 & N6: Process group tier-2 resource probe, per-PID tracking,
movement side-field verdicts, and cross-call probe persistence.

Verifies:
1. Probe inspects the entire process group (pgid), detecting busy children
   even when the group leader is blocked/idle (yes > /dev/null & wait).
2. Probe detects idle process groups (only sleep) as no movement.
3. Summarize liveness reports verdict "alive" with movement side-field ("moving" vs "still").
4. Per-PID tracking prevents exited child processes from hiding sibling activity.
5. Cross-call probe samples persist to $SAM_HOME/probe/<id>.json for zero-sleep probes.
"""

import json
import os
import signal
import subprocess
import time
import pytest

from sam import activity as sam_activity
from sam import proc as sam_proc


def test_group_probe_detects_moving_child():
    # Leader is blocked in wait(2), while child process runs yes > /dev/null
    proc = subprocess.Popen(
        ["sh", "-c", "yes > /dev/null & wait"],
        start_new_session=True,
    )
    pgid = os.getpgid(proc.pid)
    try:
        # Probe group
        delta = sam_proc.resource_delta(proc.pid, 0.3, pgid=pgid)
        assert delta is not None
        assert delta["moving"] is True
        assert delta["cpu_ticks"] > 0
    finally:
        try:
            os.killpg(pgid, signal.SIGKILL)
            proc.wait(timeout=2)
        except Exception:
            pass


def test_group_probe_detects_idle_sleep():
    proc = subprocess.Popen(
        ["sleep", "1000"],
        start_new_session=True,
    )
    pgid = os.getpgid(proc.pid)
    try:
        delta = sam_proc.resource_delta(proc.pid, 0.2, pgid=pgid)
        assert delta is not None
        assert delta["moving"] is False
        assert delta["cpu_ticks"] == 0
    finally:
        try:
            os.killpg(pgid, signal.SIGKILL)
            proc.wait(timeout=2)
        except Exception:
            pass


def test_summarize_liveness_verdicts_moving_vs_not():
    # Moving verdict: primary verdict is strictly "alive", movement side-field is "moving"
    act_moving = {
        "activity_state": "silent",
        "proc": {"ok": True},
        "resource_delta": {
            "interval_seconds": 1.0,
            "cpu_ticks": 42,
            "io_read_bytes": 100,
            "io_write_bytes": 0,
            "moving": True,
        },
    }
    liv_moving = sam_activity.summarize_liveness(act_moving)
    assert liv_moving["verdict"] == "alive"
    assert liv_moving["movement"] == "moving"
    assert liv_moving["signal"] == "no task signal"

    # No movement verdict: primary verdict is strictly "alive", movement side-field is "still"
    act_idle = {
        "activity_state": "silent",
        "proc": {"ok": True},
        "resource_delta": {
            "interval_seconds": 1.0,
            "cpu_ticks": 0,
            "io_read_bytes": 0,
            "io_write_bytes": 0,
            "moving": False,
        },
    }
    liv_idle = sam_activity.summarize_liveness(act_idle)
    assert liv_idle["verdict"] == "alive"
    assert liv_idle["movement"] == "still"
    assert liv_idle["signal"] == "no task signal"


def test_child_exit_with_busy_sibling_detected_as_moving():
    # Per N6: child exits between samples, sibling process continues doing work.
    # Total sum across raw samples would drop (cpu from 250 -> 80), but per-PID matching
    # tracks that child 1 gained +30 ticks and child 2 exited.
    sample_a = {
        "100": {"starttime": 1000, "cpu": 50, "io_read": 0, "io_write": 0},
        "101": {"starttime": 1005, "cpu": 200, "io_read": 500, "io_write": 100},
    }
    sample_b = {
        "100": {"starttime": 1000, "cpu": 80, "io_read": 0, "io_write": 0},
    }
    delta = sam_proc.compute_sample_delta(sample_a, sample_b, interval_seconds=1.0)
    assert delta is not None
    assert delta["moving"] is True
    assert delta["cpu_ticks"] == 30
    assert delta["io_read_bytes"] == 0
    assert delta["io_write_bytes"] == 0


def test_cross_call_persisted_probe_samples(tmp_path, monkeypatch):
    monkeypatch.setenv("SAM_HOME", str(tmp_path))
    agent_id = "test-agent-persist"
    pfile = tmp_path / "probe" / f"{agent_id}.json"

    proc = subprocess.Popen(
        ["sleep", "1000"],
        start_new_session=True,
    )
    pgid = os.getpgid(proc.pid)
    try:
        # First call: saves initial sample
        d1 = sam_proc.resource_delta(proc.pid, 0.05, pgid=pgid, agent_id=agent_id)
        assert d1 is not None
        assert pfile.is_file()

        # Artificially age the saved sample to 1.0 second ago
        with open(pfile, "r") as f:
            data = json.load(f)
        data["ts"] = time.time() - 1.0
        with open(pfile, "w") as f:
            json.dump(data, f)

        # Second call with interval_seconds=0.0: should use persisted sample without sleeping
        t0 = time.time()
        d2 = sam_proc.resource_delta(proc.pid, 0.0, pgid=pgid, agent_id=agent_id)
        elapsed = time.time() - t0
        assert elapsed < 0.1
        assert d2 is not None
        assert d2["interval_seconds"] >= 0.9
    finally:
        try:
            os.killpg(pgid, signal.SIGKILL)
            proc.wait(timeout=2)
        except Exception:
            pass
