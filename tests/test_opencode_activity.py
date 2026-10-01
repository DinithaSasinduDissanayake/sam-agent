"""opencode liveness: event-stream heartbeat in status --detail."""

import json
import os

from sam import activity as sam_activity
from sam import harness as sam_harness

NOW = 1_800_000_000.0


def _ev(t, age, **kw):
    d = {"type": t, "timestamp": int((NOW - age) * 1000), "sessionID": "ses_a"}
    d.update(kw)
    return d


def _write_log(path, events):
    lines = ["##OPENCODE_BEGIN_deadbeef"] + [json.dumps(e) for e in events]
    path.write_text("\n".join(lines) + "\n")


def test_event_stats_parses_tail(tmp_path):
    log = tmp_path / "output.log"
    _write_log(log, [_ev("step_start", 40), _ev("tool_use", 20),
                     _ev("step_finish", 3, part={"tokens": {"input": 10, "output": 5, "reasoning": 2}}),
                     _ev("text", 2, part={"text": "hi"})])
    st = sam_activity.opencode_event_stats(str(log), now=NOW)
    assert st["events_found"] is True and st["event_count"] == 4
    assert st["last_event_type"] == "text"
    assert abs(st["last_event_age"] - 2.0) < 0.01
    assert st["recent_event_count_5s"] == 2
    assert st["recent_event_count_30s"] == 3
    assert st["tool_events"] == 1 and st["step_finish_events"] == 1
    assert st["session_id"] == "ses_a"
    assert st["usage_tokens_total"] == 17
    assert st["usage_output_tokens_total"] == 5
    assert st["usage_reasoning_tokens_total"] == 2


def test_running_agent_classified_from_event_age(tmp_path):
    log = tmp_path / "output.log"
    _write_log(log, [_ev("text", 3, part={"text": "x"})])
    os.utime(log, (NOW - 1000, NOW - 1000))  # only the event age can make it active
    agent = {"id": "o1", "harness": "opencode", "log_path": str(log),
             "session_path": str(tmp_path / "missing-pointer")}
    act = sam_harness.get_harness("opencode").activity(agent, "running", now=NOW)
    assert act["activity_state"] == "active_recent_event"
    assert act["session"]["harness"] == "opencode"
    assert act["events"]["event_count"] == 1


def test_terminal_lifecycle_passthrough(tmp_path):
    agent = {"id": "o1", "harness": "opencode", "log_path": str(tmp_path / "none.log"),
             "session_path": None}
    act = sam_harness.get_harness("opencode").activity(agent, "completed", now=NOW)
    assert act["activity_state"] == "completed"


def test_compute_agent_activity_dispatches_to_opencode(tmp_path):
    log = tmp_path / "output.log"
    _write_log(log, [_ev("text", 3, part={"text": "x"})])
    agent = {"id": "o1", "harness": "opencode", "log_path": str(log), "session_path": None}
    out = sam_activity.compute_agent_activity(agent, "completed", now=NOW)
    assert "events" in out
    assert out["events"]["event_count"] == 1


def test_log_stats_tag_param(tmp_path):
    log = tmp_path / "output.log"
    log.write_text("##OPENCODE_BEGIN_deadbeef\nx\n##OPENCODE_END_deadbeef\n")
    oc = sam_activity.log_stats(str(log), now=NOW, tag="OPENCODE")
    assert oc["began"] is True and oc["ended"] is True
    pi = sam_activity.log_stats(str(log), now=NOW)
    assert pi["began"] is False and pi["ended"] is False