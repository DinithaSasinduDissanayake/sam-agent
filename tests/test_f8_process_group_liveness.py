#!/usr/bin/env python3
"""Tests for F8: Process group tier-2 resource probe and moving verdicts.

Verifies:
1. Probe inspects the entire process group (pgid), detecting busy children
   even when the group leader is blocked/idle.
2. Probe detects idle process groups (only sleep) as no movement.
3. Summarize liveness reports "alive, moving (cpu +N)" vs "alive, no movement for 1s".
"""

import os
import signal
import subprocess
import time
import pytest

from sam import activity as sam_activity
from sam import proc as sam_proc


def test_group_probe_detects_moving_child():
    # Leader spawns a background infinite busy loop in the same process group
    proc = subprocess.Popen(
        ["sh", "-c", "sleep 1000 & while :; do :; done"],
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
    # Moving verdict
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
    assert liv_moving["verdict"] == "alive, moving (cpu +42)"
    assert liv_moving["signal"] == "no task signal"

    # No movement verdict
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
    assert liv_idle["verdict"] == "alive, no movement for 1s"
    assert liv_idle["signal"] == "no task signal"
