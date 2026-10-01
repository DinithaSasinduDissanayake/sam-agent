"""Unit tests for OpencodeHarness and shared reasoning-flag validation."""

import pytest

from sam import harness as sam_harness
from sam.commands import logs as logs_cmd


def test_get_harness_opencode():
    h = sam_harness.get_harness("opencode")
    assert h.name == "opencode"
    assert h.sentinel_tag == "OPENCODE"
    assert h.session_kind == "pointer"


def test_build_argv_effort_and_resume():
    h = sam_harness.get_harness("opencode")
    argv = h.build_argv("/w/opencode-wrapper", "a1", "opencode/m", "/s", "/t", "/r",
                        effort="low", resume=True)
    assert argv == ["/w/opencode-wrapper", "--agent-id", "a1", "--model", "opencode/m",
                    "--session", "/s", "--task", "/t", "--result", "/r",
                    "--effort", "low", "--resume"]
    plain = h.build_argv("/w/opencode-wrapper", "a1", "opencode/m", "/s", "/t", "/r")
    assert "--effort" not in plain and "--resume" not in plain


def test_build_argv_rejects_thinking():
    with pytest.raises(ValueError):
        sam_harness.get_harness("opencode").build_argv(
            "/w", "a1", "m", "/s", "/t", "/r", thinking="low")


def test_build_argv_rejects_bad_effort():
    with pytest.raises(ValueError):
        sam_harness.get_harness("opencode").build_argv(
            "/w", "a1", "m", "/s", "/t", "/r", effort="xhigh")


def test_resume_session_semantics(tmp_path):
    h = sam_harness.get_harness("opencode")
    assert h.resume_session("/agent/session.jsonl", tmp_path / "run-002",
                            for_resume=True) == "/agent/session.jsonl"
    assert h.resume_session("/agent/session.jsonl", tmp_path / "run-002",
                            for_resume=False) == str(tmp_path / "run-002" / "session.jsonl")


def test_validate_reasoning_flags_matrix():
    v = sam_harness.validate_reasoning_flags
    assert v("pi", "low", None) is None
    assert v("pi", None, "low") == "--effort requires --harness agy or --harness opencode"
    assert v("agy", None, "high") is None
    assert v("agy", "low", None) == "--thinking cannot be used with --harness agy; use --effort"
    assert v("opencode", None, "minimal") is None
    assert v("opencode", None, "max") is None
    assert v("opencode", "low", None) == "--thinking cannot be used with --harness opencode; use --effort"
    assert v("opencode", None, "xhigh") == (
        "--effort for --harness opencode must be one of: minimal, low, medium, high, max")


def test_opencode_sentinels_are_stripped(tmp_path):
    log = tmp_path / "output.log"
    log.write_text('##OPENCODE_BEGIN_deadbeef\n{"type":"text"}\n##OPENCODE_END_deadbeef\n')
    lines = sam_harness.get_harness("opencode").logs(str(log))
    assert lines == ['{"type":"text"}\n']
    assert logs_cmd._SENTINEL_RE.match("##OPENCODE_END_deadbeef")
