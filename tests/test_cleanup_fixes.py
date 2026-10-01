"""Small review fixes: F22, F9, F18, F24, F25, F30, M3, dead code."""

import argparse
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from sam import config as sam_config
from sam import proc as sam_proc
from sam import registry as sam_registry
from sam import retry as sam_retry
from sam import state as sam_state
from sam import util as sam_util
from sam.commands import logs as logs_cmd

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "sam-home"
    monkeypatch.setenv("SAM_HOME", str(h))
    sam_config.init_sam_home()
    return h


def test_logs_error_is_clean_when_log_path_is_directory(home, capsys):
    run = home / "agents" / "l1" / "run-001"
    (run / "output.log").mkdir(parents=True)
    sam_registry.save_registry({"version": 1, "agents": [{
        "id": "l1", "name": "logdir", "state": "completed", "run_id": 1,
        "log_path": str(run / "output.log"), "result_path": str(run / "result.json")}]})
    rc = logs_cmd.run(argparse.Namespace(id_or_name="l1", name=None, n=50,
                                         follow=False, raw=False, json=False))
    assert rc == 1
    err = capsys.readouterr().err
    assert "sam_locks" not in err
    assert "Is a directory" in err


def test_duplicate_guard_releases_after_advised_wait(home):
    now = time.time()
    state = {"last_spawn": now - 100, "recent": [{
        "name": "dup", "task": "/t.md", "model": "m", "ts": now - 20,
        "granted": False, "retry_after_s": 15, "reason": "spacing"}]}
    (home / ".spawn_state.json").write_text(json.dumps(state))
    res = sam_proc.acquire_spawn_slot("dup", "/t.md", "m", wait_s=0, running=0)
    assert res["granted"] is True
    assert res["duplicate_suppressed"] is False

