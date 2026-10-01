"""ORDER 1/3 regressions: no SAM worker wall-clock timeout, recovery deadline,
reasoning persistence, result fallback, dashboard DONE/AGE.

All workers are deterministic fixtures; no live model calls.
"""

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sam import config, registry
from sam.commands import result as result_cmd
from sam.commands import resume, restart, spawn, status
from sam.commands import wait as wait_cmd

ROOT = Path(__file__).resolve().parents[1]


def load_source(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "sam-home"
    monkeypatch.setenv("SAM_HOME", str(home))
    for key in ("SAM_HARNESS", "SAM_MODEL", "SAM_AGENT_ID", "SAM_DEPTH", "SAM_ROOT_ID"):
        monkeypatch.delenv(key, raising=False)
    config.init_sam_home()
    for name in ("pi", "agy"):
        wrapper = home / "bin" / f"{name}-wrapper"
        shutil.copyfile(ROOT / "wrapper" / f"{name}_wrapper.py", wrapper)
        wrapper.chmod(0o700)
    return home


# ── ORDER 1: no default execution timeout ─────────────────────────────────────

FAKE_AGY = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
Path(os.environ["ARGV_FILE"]).write_text(json.dumps(sys.argv[1:]))
print(json.dumps({"conversation_id": "c1", "status": "SUCCESS",
                  "response": "done"}))
sys.exit(0)
'''


def test_agy_default_argv_has_no_execution_timeout(home, monkeypatch, tmp_path):
    """Default SAM worker argv must not impose a wall-clock cap.

    agy's documented default --print-timeout 0s already means unlimited,
    so SAM passes an explicit 0s (and never a 15m cap).
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    worker = fakebin / "agy"
    worker.write_text(FAKE_AGY)
    worker.chmod(0o700)
    monkeypatch.setenv("PATH", str(fakebin) + os.pathsep + os.environ["PATH"])
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("ARGV_FILE", str(argv_file))
    task = home / "input.md"
    task.write_text("do it\n")
    proc = subprocess.run(
        [sys.executable, "-m", "sam.cli", "spawn", "--name", "t1",
         "--task", str(task), "--model", "fixture", "--harness", "agy",
         "--json"],
        cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, proc.stderr
    deadline = time.monotonic() + 10
    while not argv_file.is_file() and time.monotonic() < deadline:
        time.sleep(0.01)
    argv = json.loads(argv_file.read_text())
    assert argv[argv.index("--print-timeout") + 1] == "0s"
    assert not any("15m" in a for a in argv)


# ── ORDER 3: launch_deadline is a recovery window, not immediate now ─────────

def test_spawn_deadline_is_parseable_recovery_window(home, monkeypatch, capsys):
    task = home / "t.md"
    task.write_text("work\n")
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **k: argparse.Namespace(pid=os.getpid()))
    snapshots = []
    real_save = registry.save_registry
    monkeypatch.setattr(registry, "save_registry",
                        lambda data: (snapshots.append(
                            json.loads(json.dumps(data))), real_save(data)))
    args = argparse.Namespace(name="w1", task=str(task), model="m",
                              thinking=None, effort=None, harness="pi",
                              cwd=None, json=True)
    assert spawn.run(args) == 0
    spawning = snapshots[0]["agents"][0]
    assert spawning["run_started_at"] == spawning["created_at"]
    raw = spawning["launch_deadline_at"]
    assert raw is not None
    dt = datetime.fromisoformat(raw[:-1] + "+00:00") if raw.endswith("Z") else None
    assert dt is not None
    assert 0 < (dt - datetime.now(timezone.utc)).total_seconds() <= 60


# ── ORDER 3: reasoning overrides persist; harness switch clears stale ────────

def seed_terminal(home, kind, thinking=None, effort=None):
    root = home / "agents/a1"
    run = root / "run-001"
    run.mkdir(parents=True)
    task = run / "task.md"
    task.write_text("original\n")
    session = root / "session.jsonl"
    if kind == "agy":
        session.write_text("conv-old\n")
    else:
        session.write_text('{"type":"message"}\n')
    (run / "output.log").write_text("log\n")
    (run / "result.json").write_text(json.dumps(
        {"final_state_hint": "completed", "result": "old"}))
    entry = {"id": "a1", "name": "worker", "harness": kind, "state": "completed",
             "session_path": str(session), "task_path": str(task),
             "cwd": str(home), "model": "m", "run_id": 1, "run_count": 1,
             "restart_count": 0, "thinking": thinking, "effort": effort,
             "created_at": "2020-01-01T00:00:00Z",
             "log_path": str(run / "output.log"),
             "result_path": str(run / "result.json")}
    registry.save_registry({"version": 1, "agents": [entry]})
    return entry


def test_resume_persists_reasoning_override(home, monkeypatch, capsys):
    seed_terminal(home, "agy", effort="low")
    task = home / "follow.md"
    task.write_text("more\n")
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **k: argparse.Namespace(pid=os.getpid()))
    args = argparse.Namespace(id_or_name="a1", task=str(task), model=None,
                              thinking=None, effort="high", harness=None,
                              json=True)
    assert resume.run(args) == 0
    entry = registry.load_registry()["agents"][0]
    assert entry["effort"] == "high"
    assert entry["thinking"] is None
    assert entry["run_started_at"] != entry["created_at"]


def test_restart_harness_switch_clears_stale_reasoning(home, monkeypatch, capsys):
    seed_terminal(home, "agy", effort="low")
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **k: argparse.Namespace(pid=os.getpid()))
    # agy -> pi without --thinking: stale effort must be cleared.
    args = argparse.Namespace(id_or_name="a1", harness="pi", thinking=None,
                              effort=None, json=True)
    assert restart.run(args) == 0
    entry = registry.load_registry()["agents"][0]
    assert entry["harness"] == "pi"
    assert entry["effort"] is None


# ── ORDER 3: result fallback ──────────────────────────────────────────────────

def seed_result_case(home, harness, result_value, log_text=None):
    root = home / "agents/a1"
    run = root / "run-001"
    run.mkdir(parents=True)
    task = run / "task.md"
    task.write_text("t\n")
    session = root / "session.jsonl"
    session.write_text("conv-1\n" if harness == "agy" else "{}\n")
    log = run / "output.log"
    log.write_text(log_text or "")
    (run / "result.json").write_text(json.dumps(
        {"final_state_hint": "completed", "exit_code": 0,
         "result": result_value}))
    entry = {"id": "a1", "name": "worker", "harness": harness,
             "state": "completed", "session_path": str(session),
             "task_path": str(task), "cwd": str(home), "model": "m",
             "run_id": 1, "run_count": 1, "restart_count": 0,
             "log_path": str(log),
             "result_path": str(run / "result.json")}
    registry.save_registry({"version": 1, "agents": [entry]})


def test_result_agy_envelope_fallback(home, capsys):
    seed_result_case(
        home, "agy", None,
        "##AGY_BEGIN_x\n"
        '{"conversation_id":"conv-1","status":"SUCCESS","response":"late answer"}\n'
        "##AGY_END_x\n")
    assert result_cmd.run(argparse.Namespace(id_or_name="a1", json=False)) == 0
    assert capsys.readouterr().out.strip() == "late answer"


def test_result_pi_old_run_reports_unavailable_not_old_answer(home, capsys):
    seed_result_case(home, "pi", None, "full stream\n")
    assert result_cmd.run(argparse.Namespace(id_or_name="a1", json=False)) == 1
    err = capsys.readouterr().err
    assert "unavailable" in err
    assert "full stream" not in err


def test_result_running_reports_still_running(home, monkeypatch, capsys):
    seed_result_case(home, "pi", None, "")
    reg = registry.load_registry()
    reg["agents"][0]["state"] = "running"
    reg["agents"][0]["pid"] = os.getpid()
    reg["agents"][0]["pid_start_time"] = "match"
    Path(reg["agents"][0]["result_path"]).unlink()  # running: no result yet
    registry.save_registry(reg)
    monkeypatch.setattr("sam.state.resolve_agent_state",
                        lambda *a, **k: "running")
    assert result_cmd.run(argparse.Namespace(id_or_name="a1", json=False)) == 1
    assert "still running" in capsys.readouterr().err


# ── ORDER 3: dashboard DONE/AGE ───────────────────────────────────────────────

def test_tui_done_uses_result_ended_at(tmp_path):
    tui = load_source("sam_tui", "wrapper/sam-tui.py")
    run = tmp_path / "run-001"
    run.mkdir()
    ended = time.time() - 90
    (run / "result.json").write_text(json.dumps({"ended_at": ended}))
    entry = {"name": "w", "result_path": str(run / "result.json"),
             "updated_at": datetime.now(timezone.utc).strftime(
                 "%Y-%m-%dT%H:%M:%SZ")}
    done = tui._fmt_done(entry, "completed")
    assert done not in ("-", "")
    assert done.startswith("1m")  # ~90s ago, not "just now" from updated_at
    assert tui._fmt_done({"result_path": str(run / "missing.json")},
                         "completed") == "-"
    assert tui._fmt_done(entry, "running") == "-"


def test_tui_age_prefers_current_run_start():
    tui = load_source("sam_tui", "wrapper/sam-tui.py")
    entry = {"created_at": "2020-01-01T00:00:00Z",
             "run_started_at": datetime.now(timezone.utc).strftime(
                 "%Y-%m-%dT%H:%M:%SZ")}
    assert tui._fmt_age(entry) != "-"
    assert "2020" not in tui._fmt_age(entry) and tui._fmt_age(entry) != "-"
    secs = (datetime.now(timezone.utc) - datetime(
        2020, 1, 1, tzinfo=timezone.utc)).total_seconds()
    assert secs > 86400  # sanity: created_at alone would show years/days


def test_status_age_prefers_current_run_start(home):
    seed_terminal(home, "pi")
    reg = registry.load_registry()
    reg["agents"][0]["run_started_at"] = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    registry.save_registry(reg)
    entry = registry.load_registry()["agents"][0]
    assert status._elapsed_seconds(entry) is not None
    assert status._elapsed_seconds(entry) < 3600


def test_wait_timeout_never_signals_recycled_pid(home, monkeypatch, capsys):
    seed_terminal(home, "pi")
    data = registry.load_registry()
    data["agents"][0].update(state="running", pid=123456, pgid=123456,
                              pid_start_time=1)
    registry.save_registry(data)
    monkeypatch.setattr("sam.state.resolve_agent_state", lambda *a: "running")
    ticks = {"n": 0}

    def _mono():
        ticks["n"] += 1
        return 0 if ticks["n"] == 1 else 2  # stable: locks also call it
    monkeypatch.setattr(wait_cmd.time, "monotonic", _mono)
    monkeypatch.setattr(wait_cmd.sam_proc, "proc_start_time_match", lambda *a: False)
    monkeypatch.setattr(wait_cmd.sam_proc, "killpg", lambda *a: pytest.fail("unsafe signal"))
    # Item 5: kill-on-expiry lives behind --kill-after; --timeout detaches.
    assert wait_cmd.run(argparse.Namespace(id_or_name="a1", timeout=0,
                                           kill_after=1, json=True)) == 4
    error = json.loads(capsys.readouterr().err)
    assert error["code"] == 4 and error["status"] == "error"
