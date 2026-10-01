"""F10: kill reaches TERM-resistant group members; killed runs get ended_at."""

import argparse
import os
import subprocess
import sys
import time

import pytest

from sam import config as sam_config
from sam import proc as sam_proc
from sam import registry as sam_registry
from sam.commands import kill as kill_cmd


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "sam-home"
    monkeypatch.setenv("SAM_HOME", str(h))
    sam_config.init_sam_home()
    return h


def _start_detached_group(tmp_path):
    """Leader (not our child, so it is reaped elsewhere) + a TERM-ignoring member."""
    leader_py = tmp_path / "leader.py"
    leader_py.write_text(
        "import subprocess, time\n"
        "subprocess.Popen(['sh', '-c', \"trap '' TERM; while :; do sleep 0.2; done\"])\n"
        "time.sleep(120)\n")
    launcher = subprocess.run(
        [sys.executable, "-c",
         "import subprocess, sys; "
         "p = subprocess.Popen([sys.executable, sys.argv[1]], start_new_session=True, "
         "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL); "
         "print(p.pid)",
         str(leader_py)],
        capture_output=True, text=True, timeout=10)
    leader = int(launcher.stdout.strip())
    time.sleep(0.5)
    return leader


def _entry(home, aid, pid, start_time):
    run = home / "agents" / aid / "run-001"
    run.mkdir(parents=True)
    return {"id": aid, "name": aid, "harness": "opencode", "state": "running",
            "pid": pid, "pgid": pid, "pid_start_time": start_time,
            "run_id": 1, "run_count": 1,
            "result_path": str(run / "result.json"), "log_path": str(run / "output.log"),
            "created_at": "2026-10-01T00:00:00Z", "run_started_at": "2026-10-01T00:00:00Z"}


def test_group_alive_false_for_missing_group():
    assert sam_proc.group_alive(None) is False
    assert sam_proc.group_alive(0) is False
    assert sam_proc.group_alive(2 ** 22 + 12345) is False


def test_kill_terminates_term_resistant_child_and_stamps_ended_at(home, tmp_path):
    leader = _start_detached_group(tmp_path)
    assert sam_proc.group_alive(leader)
    entry = _entry(home, "k1", leader, sam_proc.read_pid_start_time(leader))
    sam_registry.save_registry({"version": 1, "agents": [entry]})
    rc = kill_cmd.run(argparse.Namespace(id_or_name="k1", name=None, json=True))
    assert rc == 0
    assert not sam_proc.group_alive(leader)
    agent = sam_registry.load_registry()["agents"][0]
    assert agent["state"] == "killed"
    assert agent["ended_at"]


def test_kill_unknown_stale_stamps_ended_at_from_evidence(home):
    p = subprocess.Popen(["true"])
    p.wait()
    entry = _entry(home, "k2", p.pid, 1)
    log = home / "agents" / "k2" / "run-001" / "output.log"
    log.write_text("x\n")
    os.utime(log, (1_700_000_000, 1_700_000_000))
    sam_registry.save_registry({"version": 1, "agents": [entry]})
    rc = kill_cmd.run(argparse.Namespace(id_or_name="k2", name=None, json=True))
    assert rc == 0
    agent = sam_registry.load_registry()["agents"][0]
    assert agent["state"] == "killed"
    assert agent["killed_reason"] == "unknown_stale"
    assert agent["ended_at"] == "2023-11-14T22:13:20Z"
