"""Item 7 acceptance tests: proc-tier liveness.

Pipeline: tier 1 pgid+start-time check → tier 2 resource-delta movement
→ tier 3 heartbeat bonus. Wording: silent-but-alive workers report
`alive (no task signal Xm)` and are never labeled with a bare "stalled"
verdict anywhere (status detail, summarize, quick/dashboard).
"""

import argparse
import importlib.util
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

_TEST_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_TEST_DIR)
if _PROJECT_DIR not in __import__("sys").path:
    __import__("sys").path.insert(0, _PROJECT_DIR)

from sam import activity as sam_activity
from sam import proc as sam_proc
from sam import registry as sam_registry

ROOT = Path(_PROJECT_DIR)


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


def _session_entry(ts, text="hi"):
    return {"timestamp": _iso(ts),
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": text}]}}


def _write_session(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))


def _live_fields():
    pid = os.getpid()
    return {"pid": pid, "pid_start_time": sam_proc.read_pid_start_time(pid),
            "pgid": sam_proc.pgid_of(pid)}


def _agent(**over):
    a = {"id": "a1", "name": "silence-one", "state": "running",
         "run_id": 1, "harness": "pi",
         "created_at": "2026-10-01T00:00:00Z",
         "updated_at": "2026-10-01T00:00:00Z",
         "session_path": None, "log_path": None,
         "pid": None, "pgid": None, "pid_start_time": None}
    a.update(_live_fields())
    a.update(over)
    return a


def _silence_fixture(tmp_path, event_age=400.0, mtime_age=None):
    """Session with an old event; mtime old too unless heartbeat is wanted."""
    now = time.time()
    sp = tmp_path / "session.jsonl"
    _write_session(sp, [_session_entry(now - event_age)])
    touch = now - (event_age if mtime_age is None else mtime_age)
    os.utime(str(sp), (touch, touch))
    return sp


# ── Tier 1: proc_liveness ─────────────────────────────────────────────────────

class TestProcLiveness:
    def test_own_pid_ok(self):
        liv = sam_proc.proc_liveness(os.getpid(),
                                     sam_proc.read_pid_start_time(
                                         os.getpid()),
                                     sam_proc.pgid_of(os.getpid()))
        assert liv["ok"] is True
        assert liv["reason"] == "alive"
        assert liv["start_time_match"] is True

    def test_missing_pid(self):
        liv = sam_proc.proc_liveness(None)
        assert liv["ok"] is False
        assert liv["reason"] == "no pid recorded"

    def test_dead_pid(self):
        liv = sam_proc.proc_liveness(2 ** 30, 1, 2 ** 30)
        assert liv["ok"] is False
        assert liv["reason"] == "pid dead"

    def test_start_time_mismatch(self):
        liv = sam_proc.proc_liveness(os.getpid(), 12345,
                                     sam_proc.pgid_of(os.getpid()))
        assert liv["ok"] is False
        assert "start-time mismatch" in liv["reason"]

    def test_start_time_missing_in_registry(self):
        liv = sam_proc.proc_liveness(os.getpid(), None,
                                     sam_proc.pgid_of(os.getpid()))
        assert liv["ok"] is False

    def test_pgid_mismatch(self):
        wrong = sam_proc.pgid_of(os.getpid()) + 1
        liv = sam_proc.proc_liveness(os.getpid(),
                                     sam_proc.read_pid_start_time(
                                         os.getpid()),
                                     wrong)
        assert liv["ok"] is False
        assert "pgid mismatch" in liv["reason"]

    def test_zombie_is_not_alive(self):
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        try:
            time.sleep(0.05)  # let the child exit (stays unreaped = zombie)
            liv = sam_proc.proc_liveness(
                pid, sam_proc.read_pid_start_time(pid), None)
            assert liv["ok"] is False
            assert liv["zombie"] is True
            assert "zombie" in liv["reason"]
        finally:
            os.waitpid(pid, 0)

    def test_pgid_optional_when_not_recorded(self):
        liv = sam_proc.proc_liveness(os.getpid(),
                                     sam_proc.read_pid_start_time(
                                         os.getpid()),
                                     None)
        assert liv["ok"] is True
        assert liv["pgid_match"] is None


# ── Tier 2: resource_delta ────────────────────────────────────────────────────

class TestResourceDelta:
    def test_moving_sequence(self, monkeypatch):
        seq = [{"state": "S", "cpu_ticks": 100, "io_read_bytes": 0,
                "io_write_bytes": 0},
               {"state": "R", "cpu_ticks": 150, "io_read_bytes": 4096,
                "io_write_bytes": 1024}]
        monkeypatch.setattr(sam_proc, "read_proc_resource",
                            lambda pid: seq.pop(0))
        d = sam_proc.resource_delta(123, 0.5, sleep_fn=lambda s: None)
        assert d["moving"] is True
        assert d["cpu_ticks"] == 50
        assert d["io_read_bytes"] == 4096

    def test_no_movement(self, monkeypatch):
        same = {"state": "S", "cpu_ticks": 100, "io_read_bytes": 7,
                "io_write_bytes": 8}
        monkeypatch.setattr(sam_proc, "read_proc_resource",
                            lambda pid: dict(same))
        d = sam_proc.resource_delta(123, 0.5, sleep_fn=lambda s: None)
        assert d["moving"] is False
        assert d["cpu_ticks"] == 0

    def test_dead_between_samples(self, monkeypatch):
        monkeypatch.setattr(sam_proc, "read_proc_resource",
                            lambda pid: None)
        assert sam_proc.resource_delta(123, 0,
                                       sleep_fn=lambda s: None) is None

    def test_real_process_busy_interval_moves(self):
        def burn(seconds):
            end = time.time() + seconds
            x = 0
            while time.time() < end:
                x += 1
        d = sam_proc.resource_delta(os.getpid(), 0.15, sleep_fn=burn)
        assert d is not None
        assert d["moving"] is True  # CPU ticks advanced during the burn


# ── compute_agent_activity pipeline ──────────────────────────────────────────

class TestComputeProcPipeline:
    def test_silent_live_pid_stays_silent_with_evidence(self, tmp_path):
        sp = _silence_fixture(tmp_path, event_age=400.0, mtime_age=400.0)
        agent = _agent(session_path=str(sp))
        act = sam_activity.compute_agent_activity(
            agent, "running", now=time.time(), sleep_fn=lambda s: None)
        assert act["activity_state"] == "silent"
        assert act["proc"]["ok"] is True
        ev = "\n".join(act["evidence"])
        assert "proc alive: pid + start-time match" in ev
        assert "resource probe" in ev
        assert "heartbeat bonus" not in ev

    def test_silent_dead_pid_downgrades_to_unknown(self, tmp_path):
        sp = _silence_fixture(tmp_path, event_age=400.0, mtime_age=400.0)
        agent = _agent(session_path=str(sp), pid=2 ** 30,
                       pid_start_time=1, pgid=2 ** 30)
        act = sam_activity.compute_agent_activity(
            agent, "running", now=time.time(), sleep_fn=lambda s: None)
        assert act["activity_state"] == "unknown"
        assert act["proc"]["ok"] is False
        assert any("proc check failed" in e for e in act["evidence"])
        liv = sam_activity.summarize_liveness(act)
        assert liv["verdict"] == "unknown"

    def test_heartbeat_bonus_upgrades_to_idle(self, tmp_path):
        # old event timestamp, but the session file was touched recently
        sp = _silence_fixture(tmp_path, event_age=400.0, mtime_age=20.0)
        agent = _agent(session_path=str(sp))
        act = sam_activity.compute_agent_activity(
            agent, "running", now=time.time(), sleep_fn=lambda s: None)
        assert act["activity_state"] == "waiting_or_idle"
        assert any("heartbeat bonus" in e for e in act["evidence"])
        assert sam_activity.summarize_liveness(act)["verdict"] == "idle"

    def test_alive_verdict_wording(self, tmp_path):
        sp = _silence_fixture(tmp_path, event_age=480.0, mtime_age=480.0)
        agent = _agent(session_path=str(sp))
        act = sam_activity.compute_agent_activity(
            agent, "running", now=time.time(), sleep_fn=lambda s: None)
        liv = sam_activity.summarize_liveness(act)
        assert liv["verdict"] == "alive"
        assert liv["signal"] == "no task signal"
        assert abs(liv["age"] - 480) < 5
        from sam.commands import status as status_cmd
        cell = status_cmd._fmt_liveness(liv)
        assert cell == "alive (no task signal 8m)"

    def test_active_pid_keeps_active(self, tmp_path):
        sp = tmp_path / "session.jsonl"
        _write_session(sp, [_session_entry(time.time() - 2)])
        agent = _agent(session_path=str(sp))
        act = sam_activity.compute_agent_activity(
            agent, "running", now=time.time(), sleep_fn=lambda s: None)
        assert act["activity_state"] == "active_recent_event"
        assert act["proc"]["ok"] is True
        assert sam_activity.summarize_liveness(act)["verdict"] == "active"


# ── status --detail end-to-end wording ───────────────────────────────────────

@pytest.fixture
def sam_home(tmp_path, monkeypatch):
    home = tmp_path / "sam-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("SAM_HOME", str(home))
    monkeypatch.setattr(sam_proc.time, "sleep", lambda s: None)
    return home


def test_status_detail_never_says_stalled(sam_home, tmp_path, capsys):
    from sam.commands import status as status_cmd
    sp = _silence_fixture(tmp_path, event_age=400.0, mtime_age=400.0)
    agent = _agent(session_path=str(sp))
    from sam import config as sam_config
    sam_config.init_sam_home()
    (Path(sam_home) / "registry.json").write_text(
        json.dumps({"version": 1, "agents": [agent]}))

    code = status_cmd.run(argparse.Namespace(
        id_or_name="silence-one", name=None, all=False, archived=False,
        limit=None, fields=None, detail=True, watch=None,
        stall_seconds=300, json=False))
    out = capsys.readouterr().out
    assert code == 0
    assert "Liveness: alive (no task signal" in out
    assert "Activity: silent" in out
    assert "stalled" not in out


def test_status_detail_text_silent_single_agent(sam_home, tmp_path, capsys):
    from sam.commands import status as status_cmd
    sp = _silence_fixture(tmp_path, event_age=400.0, mtime_age=400.0)
    agent = _agent(session_path=str(sp))
    from sam import config as sam_config
    sam_config.init_sam_home()
    (Path(sam_home) / "registry.json").write_text(
        json.dumps({"version": 1, "agents": [agent]}))
    code = status_cmd.run(argparse.Namespace(
        id_or_name="silence-one", name=None, all=False, archived=False,
        limit=None, fields=None, detail=True, watch=None,
        stall_seconds=300, json=True))
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 0
    liv = payload["activity"]["liveness"]
    assert liv["verdict"] == "alive"
    assert liv["signal"] == "no task signal"
    assert "stalled" not in json.dumps(payload)


# ── dashboard (TUI) path ──────────────────────────────────────────────────────

def _load_tui():
    spec = importlib.util.spec_from_file_location(
        "sam_tui_item7", ROOT / "wrapper" / "sam-tui.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_tui_act_alive_cell(tmp_path):
    tui = _load_tui()
    log = tmp_path / "output.log"
    log.write_bytes(b"x")
    old = time.time() - 480
    os.utime(str(log), (old, old))
    entry = {"log_path": str(log), "session_path": None,
             **_live_fields()}
    cell = tui._fmt_act(entry, "running")
    assert cell.startswith("~")
    assert cell.endswith("m")
    assert "stalled" not in tui.LEGEND


def test_tui_act_dead_pid_cell(tmp_path):
    tui = _load_tui()
    log = tmp_path / "output.log"
    log.write_bytes(b"x")
    old = time.time() - 480
    os.utime(str(log), (old, old))
    entry = {"log_path": str(log), "session_path": None,
             "pid": 2 ** 30, "pid_start_time": 1, "pgid": 2 ** 30}
    cell = tui._fmt_act(entry, "running")
    # dead pid renders the neutral passthrough, never an alive "~" cell
    assert cell == "-"
    assert not cell.startswith("~")


# ── vocabulary sweep: no bare "stalled" anywhere ─────────────────────────────

def test_no_bare_stalled_verdicts():
    states = ("active_recent_event", "waiting_or_idle", "silent",
              "tool_pending", "completed", "failed", "killed", "partial",
              "awaiting_retry", "spawning", "unknown", "error",
              "possibly_stalled")  # legacy label input too
    for st in states:
        act = {"activity_state": st,
               "session": {"exists": True, "last_event_age": 900},
               "log": {"exists": True, "mtime_age": 900}}
        liv = sam_activity.summarize_liveness(act)
        assert "stalled" not in liv["verdict"], st
    for st in ("completed", "failed", "killed", "partial",
               "awaiting_retry", "spawning", "unknown"):
        liv = sam_activity.quick_liveness({}, st)
        assert "stalled" not in liv["verdict"], st
