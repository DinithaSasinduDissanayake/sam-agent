"""Item 4 acceptance tests: retry queue + breaker (one unit) + doctor --window.

Agreed rows covered:
  1. doctor --window: spawns <15s apart -> spacing violation listed;
     >=15s -> SPACING OK verdict (the zero-violation trace the review
     asked for).
  4. 429 run -> state awaiting_retry with not_before; breaker defers
     fresh work of that model but NEVER gates the queued retry itself;
     sam resume on a queued run returns already_queued (exit 5).
Plus the operator contract around the queue: cancel via kill/retry
--cancel, override-reason escapes are logged.
"""

import argparse
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sam import config as sam_config
from sam import registry as sam_registry
from sam import retry as sam_retry
from sam import state as sam_state

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


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_infra_run(home, aid, name, error, duration_ms=65000,
                    model="m-mod", hint="failed", exit_code=3,
                    state="running", response=None):
    root = home / "agents" / aid
    run = root / "run-001"
    run.mkdir(parents=True)
    task = run / "task.md"
    task.write_text("original work\n")
    session = root / "session.jsonl"
    session.write_text("c-123\n")  # raw conversation_id pointer format
    log = run / "output.log"
    log.write_text(f"start\nAGY_ERROR: {error}\n")
    result = {
        "final_state_hint": hint,
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "error": error,
        "result": response,
        "started_at": time.time() - duration_ms / 1000,
        "ended_at": time.time(),
        "agent_id": aid,
        "run_id": 1,
    }
    (run / "result.json").write_text(json.dumps(result))
    entry = {
        "id": aid, "name": name, "harness": "agy", "state": state,
        "session_path": str(session), "task_path": str(task),
        "cwd": str(home), "model": model, "run_id": 1, "run_count": 1,
        "restart_count": 0, "thinking": None, "effort": None,
        "created_at": _iso(datetime.now(timezone.utc) - timedelta(minutes=5)),
        "run_started_at": _iso(datetime.now(timezone.utc)
                               - timedelta(minutes=5)),
        "log_path": str(log), "result_path": str(run / "result.json"),
        "pid": None, "pgid": None, "pid_start_time": None,
    }
    return entry


def _save(entry):
    sam_registry.save_registry({"version": 1, "agents": [entry]})


def _load(aid):
    for a in sam_registry.load_registry().get("agents", []):
        if a.get("id") == aid:
            return a
    return None


def _spawn_ns(tmp_path, **over):
    task = tmp_path / "task.md"
    task.write_text("# work\n")
    ns = argparse.Namespace(name="fresh", task=str(task), model="m-mod",
                            harness="agy", thinking=None, effort=None,
                            cwd=None, json=True, no_space=False,
                            override_reason=None)
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


# ── promotion: 429 / startup-network / quality ──────────────────────────────

def test_429_run_promotes_to_awaiting_retry(home):
    from sam.commands import status as status_cmd
    entry = _seed_infra_run(
        home, "a-429", "worker",
        error='Individual quota reached. RESOURCE_EXHAUSTED. '
              '"error_code":429. Resets in 18m6s')
    _save(entry)
    before = time.time()
    status_cmd._writeback_terminals({"a-429": ("failed", 1, None)})
    got = _load("a-429")
    assert got["state"] == "awaiting_retry"
    nb = got["retry_not_before"]
    assert before + 1086 <= nb <= time.time() + 30 + 1100
    assert got["retry_kind"] == "quota"
    item = sam_retry.find_for("a-429")
    assert item and item["kind"] == "quota"
    assert item["reset_advisory_s"] == 1086
    assert item["not_before"] == nb
    # terminal-for-now: resolves back to itself, no result re-derivation
    assert sam_state.resolve_agent_state(got, 1) == "awaiting_retry"
    assert "awaiting_retry" in sam_state.TERMINAL_STATES


def test_startup_network_death_enqueues(home):
    from sam.commands import status as status_cmd
    entry = _seed_infra_run(
        home, "a-net", "net-worker",
        error="dial tcp 1.2.3.4:443: connection reset by peer",
        duration_ms=65000)
    _save(entry)
    before = time.time()
    status_cmd._writeback_terminals({"a-net": ("failed", 1, None)})
    got = _load("a-net")
    assert got["state"] == "awaiting_retry"
    item = sam_retry.find_for("a-net")
    assert item["kind"] == "startup-network"
    # default backoff 300 + jitter(0..30)
    assert before + 300 <= item["not_before"] <= time.time() + 330


def test_midflight_network_stays_failed(home):
    from sam.commands import status as status_cmd
    entry = _seed_infra_run(
        home, "a-mid", "mid-worker",
        error="EOF from stream after network connection",
        duration_ms=900000)
    _save(entry)
    status_cmd._writeback_terminals({"a-mid": ("failed", 1, None)})
    assert _load("a-mid")["state"] == "failed"
    assert sam_retry.find_for("a-mid") is None


def test_quality_failure_not_promoted(home):
    from sam.commands import status as status_cmd
    entry = _seed_infra_run(
        home, "a-q", "quality", error="logic bug in deliverable",
        duration_ms=400000)
    _save(entry)
    status_cmd._writeback_terminals({"a-q": ("failed", 1, None)})
    assert _load("a-q")["state"] == "failed"
    assert sam_retry.find_for("a-q") is None


def test_partial_never_enqueues():
    result = {"final_state_hint": "partial", "exit_code": 3,
              "duration_ms": 90000,
              "error": 'Individual quota reached. "error_code":429',
              "result": "work was captured"}
    kind, _ = sam_retry.detect_infra_failure(result)
    assert kind is None


def test_reset_parse_variants():
    assert sam_retry.parse_reset_seconds("Resets in 18m6s") == 1086
    assert sam_retry.parse_reset_seconds("21m23s") == 1283
    assert sam_retry.parse_reset_seconds("Resets in 45s") == 45
    assert sam_retry.parse_reset_seconds("no hint here") is None


def test_reset_backoff_is_capped():
    nb = sam_retry.compute_not_before(
        "quota", "Resets in 120m0s", now=1_000_000.0)
    assert 1_000_000 + 1800 <= nb <= 1_000_000 + 1830  # cap + jitter


# ── breaker: fresh work defers, queued retry never gated ─────────────────────

def test_breaker_defers_fresh_spawn(home, tmp_path, capsys):
    from sam.commands import spawn as spawn_cmd
    sam_retry.enqueue("a-other", "other", "m-mod", "quota",
                      time.time() + 600, reason="test window")
    rc = spawn_cmd.run(_spawn_ns(tmp_path))
    assert rc == 6
    payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert payload["status"] == "deferred"
    assert payload["reason"] == "quota_window"
    assert payload["retry_after_s"] >= 500
    assert "NOT an error" in payload["message"]
    assert "Queued infra-retries" in payload["message"]


def test_breaker_override_is_logged(home, tmp_path, monkeypatch):
    from sam.commands import spawn as spawn_cmd
    sam_retry.enqueue("a-other", "other", "m-mod", "quota",
                      time.time() + 600, reason="test window")
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **k: argparse.Namespace(pid=os.getpid()))
    # --no-space keeps the slot limiter from deferring this test launch
    rc = spawn_cmd.run(_spawn_ns(tmp_path, no_space=True,
                                 override_reason="canary burst"))
    assert rc == 0
    entry = sam_registry.load_registry()["agents"][0]
    assert entry["quota_override_reason"] == "canary burst"


def test_same_name_with_queued_retry_refused(home, tmp_path, capsys):
    from sam.commands import spawn as spawn_cmd
    sam_retry.enqueue("a-n1", "n1", "other-model", "quota",
                      time.time() - 10, reason="due but unfired")
    rc = spawn_cmd.run(_spawn_ns(tmp_path, name="n1"))
    assert rc == 5
    err = capsys.readouterr().err
    assert "already_queued" in err


def test_resume_on_queued_run_returns_already_queued(home, capsys, monkeypatch):
    from sam.commands import resume as resume_cmd
    entry = _seed_infra_run(home, "a-r", "resumer",
                            error='RESOURCE_EXHAUSTED Resets in 5m0s')
    entry["state"] = "awaiting_retry"
    entry["retry_not_before"] = time.time() + 300
    _save(entry)
    sam_retry.enqueue("a-r", "resumer", "m-mod", "quota",
                      time.time() + 300, reason="test")
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **k: argparse.Namespace(pid=os.getpid()))
    task = home / "new.md"
    task.write_text("next\n")
    args = argparse.Namespace(id_or_name="a-r", task=str(task), model=None,
                              thinking=None, effort=None, harness=None,
                              json=True)
    rc = resume_cmd.run(args)
    assert rc == 5
    assert "already_queued" in capsys.readouterr().err


def test_breaker_never_gates_queued_retry(home, tmp_path, monkeypatch, capsys):
    """Fresh work blocked by an open window; a DUE queued retry still fires."""
    from sam.commands import spawn as spawn_cmd
    from sam.commands import retry as retry_cmd

    due = _seed_infra_run(home, "a-due", "due-worker",
                          error='RESOURCE_EXHAUSTED Resets in 1m0s')
    due["state"] = "awaiting_retry"
    _save(due)
    sam_retry.enqueue("a-due", "due-worker", "m-mod", "quota",
                      time.time() - 10, reason="due")
    # Second window still open for the same model
    sam_retry.enqueue("a-later", "later-worker", "m-mod", "quota",
                      time.time() + 600, reason="open window")

    # 1) fresh same-model spawn is deferred by the breaker
    rc = spawn_cmd.run(_spawn_ns(tmp_path, name="fresh-one"))
    assert rc == 6
    assert "quota_window" in capsys.readouterr().err

    # 2) the queued retry fires anyway (resume via infra path, Popen mocked)
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **k: argparse.Namespace(pid=os.getpid()))
    args = argparse.Namespace(id_or_name=None, name="due-worker",
                              cancel=False, due=False, override_reason=None,
                              json=True)
    rc = retry_cmd.run(args)
    assert rc == 0, capsys.readouterr().err
    fired = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert fired["status"] == "ok" and fired["infra_retry"] is True
    assert fired["run_id"] == 2

    # dequeued, relaunched, retry fields cleared
    assert sam_retry.find_for("a-due") is None
    got = _load("a-due")
    assert got["state"] == "running"
    assert got["run_id"] == 2
    assert "retry_not_before" not in got
    # the other window is still open for fresh work
    assert sam_retry.active_window("m-mod") is not None


def test_retry_before_not_before_needs_override(home, tmp_path, capsys):
    from sam.commands import retry as retry_cmd
    entry = _seed_infra_run(home, "a-wait", "waiter",
                            error='RESOURCE_EXHAUSTED Resets in 18m0s')
    entry["state"] = "awaiting_retry"
    _save(entry)
    item = sam_retry.enqueue("a-wait", "waiter", "m-mod", "quota",
                             time.time() + 600, reason="future")
    monkeypatch_target = argparse.Namespace(
        id_or_name="waiter", name=None, cancel=False, due=False,
        override_reason=None, json=True)
    from unittest import mock
    with mock.patch("subprocess.Popen",
                    lambda *a, **k: argparse.Namespace(pid=os.getpid())):
        rc = retry_cmd.run(monkeypatch_target)
    assert rc == 5
    assert "already_queued" in capsys.readouterr().err
    assert sam_retry.find_for("a-wait") is not None  # untouched

    # override fires it and is logged
    args = argparse.Namespace(id_or_name="waiter", name=None, cancel=False,
                              due=False, override_reason="quota looks clear",
                              json=True)
    with mock.patch("subprocess.Popen",
                    lambda *a, **k: argparse.Namespace(pid=os.getpid())):
        rc = retry_cmd.run(args)
    assert rc == 0, capsys.readouterr().err
    assert json.loads(capsys.readouterr().out.strip()
                      .splitlines()[-1])["fired_early"] is True
    assert _load("a-wait")["state"] == "running"


# ── operator contract: cancel paths ──────────────────────────────────────────

def test_kill_cancels_queued_retry(home, capsys):
    from sam.commands import kill as kill_cmd
    entry = _seed_infra_run(home, "a-c", "cancelme",
                            error='RESOURCE_EXHAUSTED Resets in 18m0s')
    entry["state"] = "awaiting_retry"
    _save(entry)
    sam_retry.enqueue("a-c", "cancelme", "m-mod", "quota",
                      time.time() + 600, reason="test")
    args = argparse.Namespace(id_or_name="a-c", name=None, json=False)
    assert kill_cmd.run(args) == 0
    assert "Cancelled queued retry" in capsys.readouterr().out
    got = _load("a-c")
    assert got["state"] == "killed"
    assert got["killed_reason"] == "retry_cancelled"
    assert sam_retry.find_for("a-c") is None


def test_retry_cancel_subcommand(home, capsys):
    from sam.commands import retry as retry_cmd
    entry = _seed_infra_run(home, "a-x", "dropme",
                            error='RESOURCE_EXHAUSTED Resets in 18m0s')
    entry["state"] = "awaiting_retry"
    _save(entry)
    sam_retry.enqueue("a-x", "dropme", "m-mod", "quota",
                      time.time() + 600, reason="test")
    args = argparse.Namespace(id_or_name="dropme", name=None, cancel=True,
                              due=False, override_reason=None, json=True)
    assert retry_cmd.run(args) == 0
    assert _load("a-x")["state"] == "killed"
    assert sam_retry.find_for("a-x") is None


def test_retry_list_shows_queue(home, capsys):
    from sam.commands import retry as retry_cmd
    sam_retry.enqueue("a-l", "listed", "m-mod", "quota",
                      time.time() + 120, reason="demo")
    args = argparse.Namespace(id_or_name=None, name=None, cancel=False,
                              due=False, override_reason=None, json=True)
    assert retry_cmd.run(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    row = payload["queue"][0]
    assert row["name"] == "listed" and row["due"] is False
    assert row["retry_in_s"] > 0


# ── doctor --window ─────────────────────────────────────────────────────────

def _seed_created(created_dt, aid, name, bypass=False, override=None):
    entry = {
        "id": aid, "name": name, "state": "completed", "harness": "agy",
        "model": "m-mod", "run_id": 1, "run_count": 1,
        "created_at": _iso(created_dt),
        "run_started_at": _iso(created_dt),
        "duration_ms": 1000,
        "spacing_bypassed": bypass,
        "quota_override_reason": override,
        "spawn_waited_s": 0,
        "pid": None, "pgid": None, "pid_start_time": None,
    }
    return entry


def test_doctor_spacing_ok_when_spread_out(home, capsys):
    from sam.commands import doctor as doctor_cmd
    now = datetime.now(timezone.utc)
    a = _seed_created(now - timedelta(seconds=40), "d1", "s1")
    b = _seed_created(now - timedelta(seconds=20), "d2", "s2")
    sam_registry.save_registry({"version": 1, "agents": [a, b]})
    args = argparse.Namespace(window=24.0, json=True)
    assert doctor_cmd.run(args) == 0
    payload = json.loads(capsys.readouterr().out)["doctor"]
    assert payload["spacing_ok"] is True
    assert payload["spacing_violations"] == []
    assert payload["min_gap_s"] >= 15
    assert payload["spawns_in_window"] == 2


def test_doctor_flags_burst_violation_but_not_bypassed(home, capsys):
    from sam.commands import doctor as doctor_cmd
    now = datetime.now(timezone.utc)
    agents = [
        _seed_created(now - timedelta(seconds=60), "b1", "ok1"),
        _seed_created(now - timedelta(seconds=40), "b2", "viol"),
        # 0.3s gap after viol -> violation (not bypassed)
        _seed_created(now - timedelta(seconds=39, milliseconds=700),
                      "b3", "viol2"),
        # canary pair 15.7s later, second one bypassed -> exempt
        _seed_created(now - timedelta(seconds=24), "c1", "canary1"),
        _seed_created(now - timedelta(seconds=23, milliseconds=700),
                      "c2", "canary2", bypass=True),
        _seed_created(now - timedelta(seconds=4), "b5", "ok2"),
    ]
    sam_registry.save_registry({"version": 1, "agents": agents})
    args = argparse.Namespace(window=24.0, json=True)
    assert doctor_cmd.run(args) == 0
    payload = json.loads(capsys.readouterr().out)["doctor"]
    names = [v["name"] for v in payload["spacing_violations"]]
    assert names == ["viol2"]      # only the 0.3s gap; canary pair bypassed
    assert payload["spacing_ok"] is False
    # verdict line in human mode
    args = argparse.Namespace(window=24.0, json=False)
    doctor_cmd.run(args)
    out = capsys.readouterr().out
    assert "VIOLATIONS spacing=1" in out


def test_doctor_reports_overrides_and_queue(home, capsys):
    from sam.commands import doctor as doctor_cmd
    now = datetime.now(timezone.utc)
    a = _seed_created(now - timedelta(minutes=1), "o1", "forced",
                      override="quota looks clear")
    sam_registry.save_registry({"version": 1, "agents": [a]})
    sam_retry.enqueue("o2", "queued-one", "m-mod", "quota",
                      time.time() + 300, reason="demo")
    args = argparse.Namespace(window=1.0, json=True)
    assert doctor_cmd.run(args) == 0
    payload = json.loads(capsys.readouterr().out)["doctor"]
    assert payload["quota_overrides"][0]["reason"] == "quota looks clear"
    assert payload["retry_queue"][0]["name"] == "queued-one"
    assert payload["retry_queue"][0]["due"] is False
