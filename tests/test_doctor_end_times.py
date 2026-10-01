"""Doctor concurrency: dead runs without result.json must not count as running now."""

import os
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from sam import config as sam_config
from sam import proc as sam_proc
from sam import registry as sam_registry
from sam import run_times as sam_run_times
from sam.commands import doctor as doctor_cmd


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "sam-home"
    monkeypatch.setenv("SAM_HOME", str(h))
    sam_config.init_sam_home()
    return h


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dead_pid():
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def test_dead_running_entries_do_not_inflate_concurrency(home):
    now = datetime.now(timezone.utc)
    agents = []
    for i in range(5):
        run = home / "agents" / f"dead{i}" / "run-001"
        run.mkdir(parents=True)
        log = run / "output.log"
        log.write_text("x\n")
        t = (now - timedelta(hours=2) + timedelta(seconds=60)).timestamp()
        os.utime(log, (t, t))
        agents.append({"id": f"dead{i}", "name": f"dead{i}", "state": "running",
                       "pid": _dead_pid(), "pgid": None, "pid_start_time": 1,
                       "run_id": 1, "run_count": 1,
                       "created_at": _iso(now - timedelta(hours=2)),
                       "run_started_at": _iso(now - timedelta(hours=2)),
                       "log_path": str(log), "result_path": str(run / "result.json")})
    me = os.getpid()
    agents.append({"id": "live1", "name": "live1", "state": "running", "pid": me,
                   "pgid": None, "pid_start_time": sam_proc.read_pid_start_time(me),
                   "run_id": 1, "run_count": 1,
                   "created_at": _iso(now - timedelta(seconds=30)),
                   "run_started_at": _iso(now - timedelta(seconds=30)),
                   "log_path": str(home / "none.log"), "result_path": str(home / "none.json")})
    sam_registry.save_registry({"version": 1, "agents": agents})
    sam_proc.record_launch("live1", 1, "spawn", model="m", name="live1",
                           ts=(now - timedelta(seconds=29)).timestamp())
    report = doctor_cmd.collect(window_hours=1.0, now=now)
    assert report["spawns_in_window"] == 1
    assert report["spawn_log"][0]["concurrency_at_spawn"] == 1
    assert report["cap_violations"] == []


def test_live_running_entry_extends_to_now(home):
    now = datetime.now(timezone.utc)
    me = os.getpid()
    entry = {"id": "l", "name": "l", "state": "running", "pid": me,
             "pid_start_time": sam_proc.read_pid_start_time(me), "run_id": 1,
             "run_started_at": _iso(now - timedelta(minutes=5)),
             "result_path": str(home / "none.json"), "log_path": str(home / "none.log")}
    start, end = doctor_cmd._interval(entry, now)
    assert end == now


def test_terminal_entry_without_end_uses_duration_then_evidence(home):
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=1)
    run = home / "agents" / "t" / "run-001"
    run.mkdir(parents=True)
    entry = {"id": "t", "name": "t", "state": "killed", "run_id": 1,
             "run_started_at": _iso(start), "duration_ms": 60000,
             "result_path": str(run / "result.json"), "log_path": str(run / "output.log")}
    s, e = doctor_cmd._interval(entry, now)
    assert (e - s).total_seconds() == 60
    entry["duration_ms"] = None
    log = run / "output.log"
    log.write_text("x\n")
    t = (start + timedelta(seconds=90)).timestamp()
    os.utime(log, (t, t))
    s, e = doctor_cmd._interval(entry, now)
    assert abs((e - s).total_seconds() - 90) < 1
    log.unlink()
    s, e = doctor_cmd._interval(entry, now)
    assert e == s


def test_run_end_evidence_helper(tmp_path):
    log = tmp_path / "output.log"
    res = tmp_path / "result.json"
    entry = {"log_path": str(log), "result_path": str(res)}
    assert sam_run_times.run_end_evidence(entry) is None
    log.write_text("x")
    os.utime(log, (1_700_000_000, 1_700_000_000))
    res.write_text("{}")
    os.utime(res, (1_700_000_100, 1_700_000_100))
    assert sam_run_times.run_end_evidence(entry).timestamp() == 1_700_000_100
