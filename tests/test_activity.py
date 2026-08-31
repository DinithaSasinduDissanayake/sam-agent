#!/usr/bin/env python3
"""Tests for the conservative activity layer (sam/activity.py) and the
opt-in `sam status --detail` / `--watch` enrichment.

Uses temp SAM_HOME and synthetic session.jsonl / output.log fixtures.
Never touches the live registry, never spawns agents.
"""

import argparse
import json
import os
import time
from datetime import datetime, timezone

import pytest

_TEST_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_TEST_DIR)
import sys
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))

from sam import activity as sam_activity
from sam import config as sam_config
from sam import registry as sam_registry


# ── Fixture helpers ───────────────────────────────────────────────────────────

@pytest.fixture
def sam_home(tmp_path, monkeypatch):
    """Temp SAM_HOME pointing at an isolated directory."""
    home = tmp_path / "sam-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("SAM_HOME", str(home))
    return home


def NOW():
    return time.time()


def iso(ts_epoch):
    return datetime.fromtimestamp(ts_epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


def header_entry(ts_epoch):
    return {"type": "session", "version": 3, "id": "uuid",
            "timestamp": iso(ts_epoch), "cwd": "/tmp"}


def msg_entry(role, ts_epoch, content=None, stop=None, usage=None,
              tool_call_id=None, tool_name=None, idx=0):
    """Build a pi session `message` entry with a controlled timestamp."""
    msg = {"role": role, "timestamp": int(ts_epoch * 1000)}
    if content is not None:
        msg["content"] = content
    if stop:
        msg["stopReason"] = stop
    if usage is not None:
        msg["usage"] = usage
    if tool_call_id:
        msg["toolCallId"] = tool_call_id
    if tool_name:
        msg["toolName"] = tool_name
    return {"type": "message", "id": "e%d" % idx, "parentId": None,
            "timestamp": iso(ts_epoch), "message": msg}


def text_block(s):
    return {"type": "text", "text": s}


def think_block(s):
    return {"type": "thinking", "thinking": s}


def tool_call(tid, name):
    return {"type": "toolCall", "id": tid, "name": name, "arguments": {}}


def write_session(path, entries):
    """Write entries as JSONL; returns the encoded lines (incl. newline)."""
    lines = [json.dumps(e).encode("utf-8") + b"\n" for e in entries]
    path.write_bytes(b"".join(lines))
    return lines


def write_registry(home, agents):
    reg = {"version": 1, "agents": agents}
    (home / "registry.json").write_text(json.dumps(reg))


def make_agent(aid="sam-test-1", name="tester", state="running", pid=None,
               pid_start_time=None, session_path=None, log_path=None,
               result_path=None, updated_at="2026-07-31T00:00:00Z"):
    return {
        "id": aid, "name": name, "parent_id": None, "root_id": aid,
        "depth": 0, "run_id": 1, "model": "test-model", "state": state,
        "pid": pid, "pgid": pid, "pid_start_time": pid_start_time,
        "log_path": log_path, "result_path": result_path,
        "session_path": session_path, "task_path": "/tmp/task.md",
        "cwd": "/tmp", "created_at": updated_at, "updated_at": updated_at,
        "exit_code": None, "exit_signal": None, "duration_ms": None,
        "restart_count": 0, "killed_reason": None, "launch_deadline_at": None,
    }


def run_status(home, json_out=False, detail=False, watch=None,
               stall_seconds=300, ref=None, name=None, all_=False):
    from sam.commands.status import run
    args = argparse.Namespace(json=json_out, id_or_name=ref, name=name,
                              all=all_, detail=detail, watch=watch,
                              stall_seconds=stall_seconds)
    return run(args)


# ── session_stats: parsing, windows, bounded reads ───────────────────────────

class TestSessionStats:
    def test_missing_file(self, tmp_path):
        stats = sam_activity.session_stats(str(tmp_path / "nope.jsonl"),
                                           now=NOW())
        assert stats["exists"] is False
        assert "error" in stats
        assert stats["parse_errors"] == 0

    def test_none_path(self):
        stats = sam_activity.session_stats(None, now=NOW())
        assert stats["exists"] is False
        assert "error" in stats

    def test_header_only(self, tmp_path):
        p = tmp_path / "session.jsonl"
        write_session(p, [header_entry(NOW() - 100)])
        stats = sam_activity.session_stats(str(p), now=NOW())
        assert stats["exists"] is True
        # The header is itself the last persisted entry, but carries no
        # message role/tool evidence.
        assert stats["last_event_at"] is not None
        assert stats["last_event_age"] is not None
        assert stats["last_event_age"] > 90
        assert stats["last_event_role"] is None
        assert stats["tool_pending"] is False
        assert stats["recent_event_count_30s"] == 0
        assert stats["parse_errors"] == 0

    def test_recent_event_windows_and_bytes(self, tmp_path):
        now = NOW()
        entries = [
            header_entry(now - 100),
            msg_entry("assistant", now - 40, content=[text_block("old")],
                      stop="toolUse", idx=1),
            msg_entry("toolResult", now - 10, tool_call_id="c1",
                      tool_name="bash", idx=2),
            msg_entry("assistant", now - 1, content=[text_block("new")],
                      stop="stop", usage={"totalTokens": 50, "reasoning": 5},
                      idx=3),
        ]
        p = tmp_path / "session.jsonl"
        lines = write_session(p, entries)
        stats = sam_activity.session_stats(str(p), now=now)
        # 5s window: only the now-1 entry.
        assert stats["recent_event_count_5s"] == 1
        assert stats["recent_event_bytes_5s"] == len(lines[3]) - 1
        # 30s window: now-10 and now-1 entries (not now-40).
        assert stats["recent_event_count_30s"] == 2
        assert stats["recent_event_bytes_30s"] == \
            (len(lines[2]) - 1) + (len(lines[3]) - 1)
        # last event is the newest entry (file order).
        assert stats["last_event_role"] == "assistant"
        assert stats["last_event_stop_reason"] == "stop"
        # Microsecond truncation of the ISO timestamp can push the
        # computed age a hair above the nominal 1s, so use a tolerance.
        assert stats["last_event_age"] is not None
        assert 0.0 <= stats["last_event_age"] < 2.0

    def test_last_event_fields(self, tmp_path):
        now = NOW()
        entries = [
            header_entry(now - 100),
            msg_entry("assistant", now - 2,
                      content=[text_block("x"), tool_call("c1", "web_search")],
                      stop="toolUse", idx=1),
        ]
        p = tmp_path / "session.jsonl"
        write_session(p, entries)
        stats = sam_activity.session_stats(str(p), now=now)
        assert stats["last_event_role"] == "assistant"
        assert stats["last_event_stop_reason"] == "toolUse"
        assert stats["last_event_tool_names"] == ["web_search"]
        assert set(stats["last_event_content_types"]) == {"text", "toolCall"}

    def test_usage_and_thinking_tokens(self, tmp_path):
        now = NOW()
        entries = [
            header_entry(now - 100),
            msg_entry("assistant", now - 20,
                      usage={"totalTokens": 100, "reasoning": 30}, idx=1),
            msg_entry("assistant", now - 2,
                      usage={"totalTokens": 50, "reasoning": 10}, idx=2),
            msg_entry("toolResult", now - 1, tool_call_id="c1", idx=3),
        ]
        p = tmp_path / "session.jsonl"
        write_session(p, entries)
        stats = sam_activity.session_stats(str(p), now=now)
        assert stats["usage_tokens_total"] == 150
        assert stats["usage_tokens_30s"] == 150
        assert stats["usage_tokens_5s"] == 50
        assert stats["usage_thinking_tokens_total"] == 40
        assert stats["usage_thinking_tokens_30s"] == 40
        assert stats["usage_thinking_tokens_5s"] == 10
        # toolResult carries no usage and is never counted as tokens.
        assert stats["estimated_tokens_5s"] is None
        assert stats["estimated_tokens_30s"] is None

    def test_estimated_tokens_separate_from_usage(self, tmp_path):
        now = NOW()
        # 11 chars -> (11 + 3) // 4 = 3 estimated tokens.
        entries = [
            header_entry(now - 100),
            msg_entry("assistant", now - 3, content=[text_block("hello world")],
                      idx=1),
        ]
        p = tmp_path / "session.jsonl"
        write_session(p, entries)
        stats = sam_activity.session_stats(str(p), now=now)
        assert stats["usage_tokens_5s"] is None
        assert stats["usage_tokens_30s"] is None
        assert stats["estimated_tokens_5s"] == 3
        assert stats["estimated_tokens_30s"] == 3
        assert stats["token_note"]
        # No silently-combined field exists.
        assert "tokens_5s" not in stats
        assert "tokens_30s" not in stats

    def test_mixed_usage_and_estimate_never_combined(self, tmp_path):
        now = NOW()
        entries = [
            header_entry(now - 100),
            msg_entry("assistant", now - 4,
                      usage={"totalTokens": 200, "reasoning": 40}, idx=1),
            msg_entry("assistant", now - 2, content=[text_block("abcdefgh")],
                      idx=2),  # 8 chars -> 2 estimated
        ]
        p = tmp_path / "session.jsonl"
        write_session(p, entries)
        stats = sam_activity.session_stats(str(p), now=now)
        assert stats["usage_tokens_30s"] == 200
        assert stats["usage_thinking_tokens_30s"] == 40
        assert stats["estimated_tokens_30s"] == 2
        assert stats["usage_tokens_30s"] + stats["estimated_tokens_30s"] != \
            stats["usage_tokens_total"]

    def test_malformed_lines_skipped(self, tmp_path):
        now = NOW()
        p = tmp_path / "session.jsonl"
        p.write_bytes(
            json.dumps(header_entry(now - 100)).encode() + b"\n"
            + b"this is not json\n"
            + b'{"type":"message","id":"cut'  # truncated mid-line, no newline
        )
        stats = sam_activity.session_stats(str(p), now=now)
        assert stats["parse_errors"] == 2
        assert stats["exists"] is True
        # Only the header parsed successfully; role evidence stays None.
        assert stats["last_event_role"] is None
        assert stats["recent_event_count_30s"] == 0

    def test_bounded_tail(self, tmp_path):
        now = NOW()
        # Oldest first: pi appends chronologically, so the newest entries
        # sit at EOF and a tail read must still see them.
        entries = [header_entry(now - 1000)]
        entries += [
            msg_entry("assistant", now - 300 + i,
                      content=[text_block("entry %d" % i)],
                      usage={"totalTokens": 10, "reasoning": 1},
                      idx=i + 1)
            for i in range(300)
        ]
        p = tmp_path / "session.jsonl"
        write_session(p, entries)
        stats = sam_activity.session_stats(str(p), now=now, max_bytes=2048)
        assert stats["truncated"] is True
        # The last entry (newest, file order) is still the true last entry
        # even under truncation (the tail ends at EOF).
        assert stats["last_event_role"] == "assistant"
        assert stats["last_event_age"] is not None
        assert 0.0 <= stats["last_event_age"] < 2.0
        # The tail contains entries from the last ~30s (newest are at EOF).
        assert stats["recent_event_count_30s"] >= 1
        assert stats["usage_tokens_total"] is not None

    def test_future_timestamps_excluded(self, tmp_path):
        now = NOW()
        entries = [
            header_entry(now - 100),
            msg_entry("assistant", now + 60,
                      usage={"totalTokens": 500, "reasoning": 100}, idx=1),
        ]
        p = tmp_path / "session.jsonl"
        write_session(p, entries)
        stats = sam_activity.session_stats(str(p), now=now)
        # Usage exists in the tail (total), but the entry is outside every
        # window, so windowed usage fields stay None.
        assert stats["usage_tokens_total"] == 500
        assert stats["usage_tokens_5s"] is None
        assert stats["usage_tokens_30s"] is None
        assert stats["recent_event_count_5s"] == 0
        # last_event_age is clamped to 0 for a future timestamp.
        assert stats["last_event_age"] == 0.0


# ── log_stats ────────────────────────────────────────────────────────────────

class TestLogStats:
    def test_missing(self, tmp_path):
        lg = sam_activity.log_stats(str(tmp_path / "output.log"), now=NOW())
        assert lg["exists"] is False
        assert "error" in lg

    def test_sentinels(self, tmp_path):
        now = NOW()
        p = tmp_path / "output.log"
        p.write_bytes(b"##PI_BEGIN_abcdef12\nhello\n##PI_END_abcdef12\n")
        lg = sam_activity.log_stats(str(p), now=now)
        assert lg["exists"] is True
        assert lg["began"] is True
        assert lg["ended"] is True
        assert lg["size"] == p.stat().st_size
        assert lg["mtime_age"] is not None

    def test_no_sentinels(self, tmp_path):
        p = tmp_path / "output.log"
        p.write_bytes(b"plain text\n")
        lg = sam_activity.log_stats(str(p), now=NOW())
        assert lg["began"] is False
        assert lg["ended"] is False


# ── watch clamping + two-sample deltas ───────────────────────────────────────

class TestWatch:
    def test_clamp_watch_seconds(self):
        assert sam_activity.clamp_watch_seconds(None) == sam_activity.WATCH_DEFAULT
        assert sam_activity.clamp_watch_seconds(0) == 1
        assert sam_activity.clamp_watch_seconds(-3) == 1
        assert sam_activity.clamp_watch_seconds(60) == 30
        assert sam_activity.clamp_watch_seconds(10) == 10
        assert sam_activity.clamp_watch_seconds("x") == sam_activity.WATCH_DEFAULT
        assert sam_activity.clamp_watch_seconds(True) == sam_activity.WATCH_DEFAULT

    def test_watch_interval_clamped_inside(self, tmp_path):
        p = tmp_path / "f.bin"
        p.write_bytes(b"")
        seen = []

        def sleep_fn(secs):
            seen.append(secs)

        sam_activity.watch_deltas({"f": str(p)}, 60, sleep_fn=sleep_fn)
        assert seen == [30]

    def test_watch_measured(self, tmp_path):
        p = tmp_path / "f.bin"
        p.write_bytes(b"12345")

        def grow(secs):
            with open(p, "ab") as f:
                f.write(b"abcdef")

        d = sam_activity.watch_deltas({"f": str(p)}, 2, sleep_fn=grow)
        assert d["f"]["status"] == "measured"
        assert d["f"]["growth_bytes"] == 6
        assert d["f"]["size_before"] == 5
        assert d["f"]["size_after"] == 11

    def test_watch_shrunk(self, tmp_path):
        p = tmp_path / "f.bin"
        p.write_bytes(b"1234567890")

        def shrink(secs):
            with open(p, "wb") as f:
                f.write(b"12")

        d = sam_activity.watch_deltas({"f": str(p)}, 2, sleep_fn=shrink)
        assert d["f"]["status"] == "shrunk"
        assert d["f"]["growth_bytes"] is None

    def test_watch_replaced(self, tmp_path):
        p = tmp_path / "f.bin"
        p.write_bytes(b"1234567890")
        q = tmp_path / "q.bin"
        q.write_bytes(b"x")

        def replace(secs):
            os.replace(q, p)

        d = sam_activity.watch_deltas({"f": str(p)}, 2, sleep_fn=replace)
        assert d["f"]["status"] == "replaced"
        assert d["f"]["growth_bytes"] is None

    def test_watch_missing(self, tmp_path):
        p = tmp_path / "f.bin"
        d = sam_activity.watch_deltas({"f": str(p)}, 2, sleep_fn=lambda s: None)
        assert d["f"]["status"] == "missing"
        assert d["f"]["growth_bytes"] is None

    def test_watch_none_path(self):
        d = sam_activity.watch_deltas({"f": None}, 2, sleep_fn=lambda s: None)
        assert d["f"]["status"] == "missing"


# ── classify: PID/lifecycle distinctions + conservative states ───────────────

def _old_session():
    return {
        "exists": True, "last_event_age": 4000.0, "tool_pending": False,
        "pending_tool_call_ids": [],
    }


def _old_log():
    return {"exists": True, "mtime_age": 4000.0}


class TestClassify:
    def test_lifecycle_passthrough(self):
        agent = make_agent()
        for state in ("completed", "failed", "killed", "spawning", "unknown"):
            cls = sam_activity.classify(agent, state, _old_session(),
                                        _old_log(), now=NOW())
            assert cls["activity_state"] == state
            assert cls["evidence"]

    def test_unknown_never_stalled(self):
        """Dead PID / no result stays unknown even with old signals."""
        agent = make_agent()
        cls = sam_activity.classify(agent, "unknown", _old_session(),
                                    _old_log(), now=NOW())
        assert cls["activity_state"] == "unknown"

    def test_possibly_stalled_requires_verified_live(self):
        """Only a verified-live lifecycle may be possibly_stalled."""
        agent = make_agent()
        cls = sam_activity.classify(agent, "running", _old_session(),
                                    _old_log(), now=NOW(),
                                    stall_seconds=300)
        assert cls["activity_state"] == "possibly_stalled"
        assert any("pid alive" in e for e in cls["evidence"])

    def test_tool_pending_over_stall(self):
        """Long-running tools must not be labeled stalled."""
        agent = make_agent()
        session = dict(_old_session())
        session["tool_pending"] = True
        session["pending_tool_call_ids"] = ["c1"]
        cls = sam_activity.classify(agent, "running", session, _old_log(),
                                    now=NOW())
        assert cls["activity_state"] == "tool_pending"

    def test_tool_pending_requires_ids(self):
        agent = make_agent()
        session = dict(_old_session())
        session["tool_pending"] = True
        session["pending_tool_call_ids"] = []
        cls = sam_activity.classify(agent, "running", session, _old_log(),
                                    now=NOW())
        assert cls["activity_state"] == "possibly_stalled"

    def test_active_recent_event(self):
        agent = make_agent()
        session = {"exists": True, "last_event_age": 10.0,
                   "tool_pending": False, "pending_tool_call_ids": []}
        log = {"exists": False}
        cls = sam_activity.classify(agent, "running", session, log,
                                    now=NOW(), active_window=30,
                                    stall_seconds=300)
        assert cls["activity_state"] == "active_recent_event"

    def test_waiting_or_idle_between_windows(self):
        agent = make_agent()
        session = {"exists": True, "last_event_age": 120.0,
                   "tool_pending": False, "pending_tool_call_ids": []}
        log = {"exists": False}
        cls = sam_activity.classify(agent, "running", session, log,
                                    now=NOW(), active_window=30,
                                    stall_seconds=300)
        assert cls["activity_state"] == "waiting_or_idle"

    def test_possibly_stalled_beyond_threshold(self):
        agent = make_agent()
        session = {"exists": True, "last_event_age": 400.0,
                   "tool_pending": False, "pending_tool_call_ids": []}
        log = {"exists": False}
        cls = sam_activity.classify(agent, "running", session, log,
                                    now=NOW(), active_window=30,
                                    stall_seconds=300)
        assert cls["activity_state"] == "possibly_stalled"

    def test_recent_log_only_counts(self):
        agent = make_agent()
        session = {"exists": False}
        log = {"exists": True, "mtime_age": 10.0}
        cls = sam_activity.classify(agent, "running", session, log,
                                    now=NOW(), active_window=30)
        assert cls["activity_state"] == "active_recent_event"

    def test_no_files_recent_start_waiting(self):
        updated = iso(NOW() - 60)
        agent = make_agent(updated_at=updated)
        cls = sam_activity.classify(agent, "running",
                                    {"exists": False}, {"exists": False},
                                    now=NOW(), stall_seconds=300)
        assert cls["activity_state"] == "waiting_or_idle"

    def test_no_files_old_start_possibly_stalled(self):
        updated = iso(NOW() - 1000)
        agent = make_agent(updated_at=updated)
        cls = sam_activity.classify(agent, "running",
                                    {"exists": False}, {"exists": False},
                                    now=NOW(), stall_seconds=300)
        assert cls["activity_state"] == "possibly_stalled"


# ── compute_agent_activity ───────────────────────────────────────────────────

class TestComputeAgentActivity:
    def test_full_block(self, tmp_path):
        now = NOW()
        session_p = tmp_path / "session.jsonl"
        log_p = tmp_path / "output.log"
        write_session(session_p, [
            header_entry(now - 100),
            msg_entry("assistant", now - 2,
                      content=[tool_call("c1", "bash")], stop="toolUse", idx=1),
        ])
        log_p.write_bytes(b"##PI_BEGIN_abcd1234\n")
        agent = make_agent(session_path=str(session_p), log_path=str(log_p))
        act = sam_activity.compute_agent_activity(agent, "running", now=now)
        assert act["activity_state"] == "tool_pending"
        assert act["session"]["tool_pending"] is True
        assert act["log"]["began"] is True
        assert act["lifecycle_state"] == "running"
        assert "watch" not in act

    def test_watch_attached_when_requested(self, tmp_path):
        p = tmp_path / "session.jsonl"
        p.write_bytes(b"")
        agent = make_agent(session_path=str(p), log_path=None)

        def noop(secs):
            return None

        act = sam_activity.compute_agent_activity(
            agent, "running", watch=60, sleep_fn=noop, now=NOW())
        assert act["watch"]["interval_seconds"] == 30
        assert act["watch"]["session"]["status"] in ("measured", "missing")
        assert act["watch"]["log"]["status"] == "missing"


# ── sam status command: backward compat + opt-in enrichment ──────────────────

class TestStatusCommand:
    def _live_agent(self, sam_home, session_entries=None):
        """An agent whose PID is the test process (verified alive) with a
        recent session event, so lifecycle resolves to "running" and the
        classifier reports active_recent_event."""
        from sam.proc import read_pid_start_time
        session_p = None
        if session_entries is None:
            session_entries = [header_entry(NOW() - 2),
                               msg_entry("assistant", NOW() - 2,
                                         content=[text_block("hi")],
                                         stop="stop", idx=1)]
        if session_entries:
            session_p = sam_home / "session.jsonl"
            write_session(session_p, session_entries)
        agent = make_agent(
            state="running", pid=os.getpid(),
            pid_start_time=read_pid_start_time(os.getpid()),
            session_path=str(session_p) if session_p else None,
        )
        write_registry(sam_home, [agent])
        return agent

    def test_default_json_no_activity(self, sam_home, capsys):
        self._live_agent(sam_home)
        code = run_status(sam_home, json_out=True)
        out = capsys.readouterr().out
        assert code == 0
        data = json.loads(out)
        assert isinstance(data, list)
        assert data[0]["resolved_state"] == "running"
        assert "activity" not in data[0]

    def test_detail_json_has_activity(self, sam_home, capsys):
        self._live_agent(sam_home)
        code = run_status(sam_home, json_out=True, detail=True)
        data = json.loads(capsys.readouterr().out)
        assert code == 0
        assert "activity" in data[0]
        assert data[0]["activity"]["activity_state"] == "active_recent_event"
        assert data[0]["activity"]["session"]["exists"] is True
        assert data[0]["activity"]["evidence"]
        # lifecycle fields preserved on the agent entry itself
        assert data[0]["resolved_state"] == "running"

    def test_default_text_header_unchanged(self, sam_home, capsys):
        self._live_agent(sam_home)
        code = run_status(sam_home, json_out=False)
        lines = capsys.readouterr().out.splitlines()
        assert code == 0
        assert "ID" in lines[0] and "NAME" in lines[0]
        assert "STATE" in lines[0] and "PID" in lines[0]
        assert "ACTIVITY" not in lines[0]
        assert lines[1] == "-" * 60
        assert "running" in lines[2]

    def test_detail_text_has_activity_column(self, sam_home, capsys):
        self._live_agent(sam_home)
        code = run_status(sam_home, json_out=False, detail=True)
        lines = capsys.readouterr().out.splitlines()
        assert code == 0
        assert "ACTIVITY" in lines[0]
        assert "active_recent_event" in lines[2]

    def test_single_agent_detail_block(self, sam_home, capsys):
        self._live_agent(sam_home)
        code = run_status(sam_home, json_out=False, detail=True,
                          ref="sam-test-1")
        out = capsys.readouterr().out
        assert code == 0
        assert "Activity: active_recent_event" in out
        assert "lifecycle: running" in out
        assert "evidence:" in out
        assert "recent events (5s/30s)" in out

    def test_watch_clamped_at_status_level(self, sam_home, capsys, monkeypatch):
        self._live_agent(sam_home)
        monkeypatch.setattr(sam_activity.time, "sleep", lambda s: None)
        code = run_status(sam_home, json_out=True, watch=60)
        data = json.loads(capsys.readouterr().out)
        assert code == 0
        watch = data[0]["activity"]["watch"]
        assert watch["interval_seconds"] == 30

    def test_malformed_session_status_ok(self, sam_home, capsys):
        bad = sam_home / "session.jsonl"
        bad.write_bytes(b"garbage\nmore garbage\n")
        write_registry(sam_home, [
            make_agent(state="running", session_path=str(bad))])
        code = run_status(sam_home, json_out=True, detail=True)
        data = json.loads(capsys.readouterr().out)
        assert code == 0
        act = data[0]["activity"]
        assert act["session"]["exists"] is True
        assert act["session"]["parse_errors"] == 2
        # pid is None + running => lifecycle unknown (dead PID / no result)
        assert act["activity_state"] == "unknown"

    def test_all_flag_preserved(self, sam_home, capsys):
        """--all still shows terminal agents; default still hides them."""
        write_registry(sam_home, [
            make_agent(state="completed", result_path=None)])
        run_status(sam_home, json_out=True)
        default_data = json.loads(capsys.readouterr().out)
        assert default_data == []
        run_status(sam_home, json_out=True, all_=True)
        all_data = json.loads(capsys.readouterr().out)
        assert len(all_data) == 1
        assert all_data[0]["resolved_state"] == "completed"

    def test_status_not_found_unknown_ref(self, sam_home, capsys):
        write_registry(sam_home, [make_agent(state="completed")])
        code = run_status(sam_home, json_out=True, ref="missing-agent")
        assert code == 1
        err = capsys.readouterr().err
        assert "agent not found" in err
