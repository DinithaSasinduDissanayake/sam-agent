"""Item 5 acceptance tests: wait inversion.

- nonzero `--timeout N` detaches (exit 0, state running, deprecation
  warning) — never waits, never signals
- `--timeout 0` / default pins wait-forever (rendezvous unchanged)
- `--kill-after N` is the explicit kill opt-in (exit 4, state killed)
- already-terminal agents still rendezvous regardless of --timeout
"""

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

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "sam-home"
    monkeypatch.setenv("SAM_HOME", str(home))
    for key in ("SAM_HARNESS", "SAM_MODEL", "SAM_AGENT_ID", "SAM_DEPTH",
                "SAM_ROOT_ID"):
        monkeypatch.delenv(key, raising=False)
    sam_config.init_sam_home()
    for name in ("pi", "agy"):
        wrapper = home / "bin" / f"{name}-wrapper"
        shutil.copyfile(ROOT / "wrapper" / f"{name}_wrapper.py", wrapper)
        wrapper.chmod(0o700)
    return home


def _seed(home, entry, result=None):
    if result is not None:
        rp = home / "agents" / entry["id"] / "run-001" / "result.json"
        rp.parent.mkdir(parents=True, exist_ok=True)
        result.setdefault("agent_id", entry["id"])
        result.setdefault("run_id", entry.get("run_id", 1))
        rp.write_text(json.dumps(result))
        entry["result_path"] = str(rp)
    sam_registry.save_registry({"version": 1, "agents": [entry]})


def _base_entry(aid="w1", name="worker"):
    return {
        "id": aid, "name": name, "harness": "pi", "state": "running",
        "model": "m", "run_id": 1, "run_count": 1,
        "session_path": None, "task_path": None,
        "created_at": "2026-10-01T00:00:00Z",
        "run_started_at": "2026-10-01T00:00:00Z",
        "pid": os.getpid(), "pgid": os.getpgrp(),
        "pid_start_time": sam_proc.read_pid_start_time(os.getpid()),
    }


def _ns(**over):
    base = dict(id_or_name="w1", name=None, timeout=0, kill_after=None,
                json=True)
    base.update(over)
    return argparse.Namespace(**base)


# ── nonzero --timeout detaches ───────────────────────────────────────────────

def test_timeout_nonzero_detaches_with_warning(home, capsys):
    from sam.commands import wait as wait_cmd
    _seed(home, _base_entry())
    rc = wait_cmd.run(_ns(timeout=5))
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["status"] == "running"
    assert payload["detached"] is True
    assert payload["result"] is None
    assert "deprecation" in captured.err
    assert "--kill-after" in captured.err
    # agent untouched: still running in the registry
    entry = sam_registry.load_registry()["agents"][0]
    assert entry["state"] == "running"


def test_already_terminal_rendezvous_even_with_timeout(home, capsys):
    from sam.commands import wait as wait_cmd
    entry = _base_entry()
    entry["state"] = "completed"
    _seed(home, entry, {"final_state_hint": "completed", "exit_code": 0,
                        "result": "done", "ended_at": time.time()})
    rc = wait_cmd.run(_ns(timeout=5))
    assert rc == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == "completed"
    assert "deprecation" not in captured.err  # no detach, no warning


# ── --timeout 0 (and the default) wait forever ───────────────────────────────

@pytest.mark.parametrize("omit_timeout", [False, True])
def test_timeout_zero_waits_until_terminal(home, monkeypatch, capsys,
                                           omit_timeout):
    from sam.commands import wait as wait_cmd
    _seed(home, _base_entry(),
          {"final_state_hint": "completed", "exit_code": 0,
           "result": "done", "ended_at": time.time()})

    states = iter(["running", "completed"])
    monkeypatch.setattr("sam.state.resolve_agent_state",
                        lambda *a: next(states))
    monkeypatch.setattr(wait_cmd.time, "sleep", lambda s: None)

    ns = _ns()
    if omit_timeout:
        del ns.timeout          # getattr fallback must be wait-forever
    rc = wait_cmd.run(ns)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "completed"
    assert "detached" not in payload


# ── --kill-after N kills (exit 4, state killed) ──────────────────────────────

def test_kill_after_terminates_real_child(home, monkeypatch, capsys):
    from sam.commands import wait as wait_cmd
    child = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        entry = _base_entry()
        entry["pid"] = child.pid
        entry["pgid"] = child.pid  # start_new_session => pgid == pid
        entry["pid_start_time"] = sam_proc.read_pid_start_time(child.pid)
        _seed(home, entry)

        ticks = {"n": 0}

        def _mono():
            ticks["n"] += 1
            return 0 if ticks["n"] == 1 else 2  # stable: locks also call it
        monkeypatch.setattr(wait_cmd.time, "monotonic", _mono)
        rc = wait_cmd.run(_ns(timeout=0, kill_after=1))
        assert rc == 4
        err = capsys.readouterr().err
        error = json.loads(err)
        assert error["code"] == 4
        assert "terminated" in error["message"]

        entry = sam_registry.load_registry()["agents"][0]
        assert entry["state"] == "killed"
        assert entry["killed_reason"] == "wait_kill_after"
        # child actually received the signal
        child.wait(timeout=5)
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_kill_after_zero_means_no_kill_bound(home, monkeypatch, capsys):
    """--kill-after 0 = off: no deadline, falls through to wait-forever."""
    from sam.commands import wait as wait_cmd
    _seed(home, _base_entry(),
          {"final_state_hint": "completed", "exit_code": 0,
           "result": "done", "ended_at": time.time()})
    states = iter(["running", "completed"])
    monkeypatch.setattr("sam.state.resolve_agent_state",
                        lambda *a: next(states))
    monkeypatch.setattr(wait_cmd.time, "sleep", lambda s: None)
    rc = wait_cmd.run(_ns(timeout=0, kill_after=0))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "completed"
