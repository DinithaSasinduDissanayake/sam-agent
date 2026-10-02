"""S7: harness registration, model resolution, launcher selection."""
import os
import sys

import pytest

from sam import config as sam_config
from sam import harness as sam_harness
from sam import util as sam_util
from tests.xplat import HARNESSES


def test_all_harnesses_are_registered():
    assert tuple(sam_config.HARNESSES) == HARNESSES
    for h in HARNESSES:
        assert sam_config.resolve_harness(h, None) == h
        assert sam_harness.get_harness(h).name == h
    with pytest.raises(ValueError):
        sam_config.resolve_harness("nope", None)


def test_runner_selection(monkeypatch):
    monkeypatch.delenv("SAM_RUNNER", raising=False)
    for h in ("opencode", "claude", "codex"):
        assert sam_config.uses_runner(h) is True
    assert sam_config.uses_runner("pi") is (os.name == "nt")
    assert sam_config.uses_runner("agy") is (os.name == "nt")
    monkeypatch.setenv("SAM_RUNNER", "generic")
    assert sam_config.uses_runner("pi") is True
    assert sam_config.runner_path().is_file()
    assert sam_config.launcher_allowed(sam_config.runner_path()) is True
    assert sam_config.launcher_allowed(sys.executable) is False
    assert sam_config.launcher_path("claude") == sam_config.runner_path()
    # `sam init` copies wrapper scripts to wrapper_path: for pi/agy it must never be the runner
    assert sam_config.wrapper_path(harness="pi").name == "pi-wrapper"
    assert sam_config.wrapper_path(harness="agy").name == "agy-wrapper"


def test_sam_init_does_not_overwrite_the_runner():
    from tests.xplat import sam
    before = sam_config.runner_path().read_bytes()
    assert sam(["init"], dict(os.environ)).returncode == 0
    assert sam_config.runner_path().read_bytes() == before
    assert b"sam-runner" in before


def test_launcher_argv_for_runner(monkeypatch):
    monkeypatch.setenv("SAM_RUNNER", "generic")
    h = sam_harness.get_harness("opencode")
    argv = h.build_argv(sam_config.launcher_path("opencode"), "sam-1", "m",
                        "S", "T", "R", effort="low", resume=True)
    full = sam_util.launcher_argv(argv, "opencode")
    assert full[:4] == [sys.executable, str(sam_config.runner_path()), "--harness", "opencode"]
    assert full[4:] == ["--agent-id", "sam-1", "--model", "m", "--session", "S",
                        "--task", "T", "--result", "R", "--effort", "low", "--resume"]


def test_reasoning_flag_rules_keep_the_legacy_messages():
    assert sam_harness.reasoning_error("pi", "high", None) is None
    assert sam_harness.reasoning_error("pi", None, "low") == "--effort requires --harness agy"
    assert (sam_harness.reasoning_error("agy", "high", None)
            == "--thinking cannot be used with --harness agy; use --effort")
    assert sam_harness.reasoning_error("opencode", None, "low") is None
    assert "opencode" in sam_harness.reasoning_error("opencode", "high", None)
    assert sam_harness.uses_effort("pi") is False and sam_harness.uses_effort("codex") is True


def test_model_defaults_and_sam_model_scoping(monkeypatch):
    cfg = sam_config.load_config()
    monkeypatch.delenv("SAM_MODEL", raising=False)
    assert sam_config.resolve_model(None, "opencode", cfg) == "opencode/muse-spark-1.3-contributor-free"
    assert sam_config.resolve_model(None, "claude", cfg) == "sonnet"
    assert sam_config.resolve_model(None, "agy", cfg) == sam_config.AGY_DEFAULT_MODEL
    assert sam_config.resolve_model("x/y", "claude", cfg) == "x/y"
    # a child of an agy worker must not hand its gemini model to another harness
    monkeypatch.setenv("SAM_MODEL", "gemini-3.8-flash-low")
    monkeypatch.setenv("SAM_MODEL_HARNESS", "agy")
    assert sam_config.resolve_model(None, "agy", cfg) == "gemini-3.8-flash-low"
    assert sam_config.resolve_model(None, "opencode", cfg) == "opencode/muse-spark-1.3-contributor-free"
    monkeypatch.delenv("SAM_MODEL_HARNESS")
    assert sam_config.resolve_model(None, "opencode", cfg) == "gemini-3.8-flash-low"   # legacy behaviour


def test_child_env_marks_the_model_harness():
    env = sam_util.build_child_env("sam-1", "m", 0, harness="claude")
    assert env["SAM_MODEL"] == "m" and env["SAM_MODEL_HARNESS"] == "claude"
    env = sam_util.build_child_env("sam-1", "m", 0)
    assert "SAM_MODEL_HARNESS" not in env


def test_pointer_harness_session_rules(tmp_path):
    h = sam_harness.get_harness("opencode")
    assert h.resume_session("/a/session.jsonl", tmp_path, for_resume=True) == "/a/session.jsonl"
    assert h.resume_session("/a/session.jsonl", tmp_path, for_resume=False) == str(tmp_path / "session.jsonl")
    agy = sam_harness.get_harness("agy")
    assert agy.resume_session("/a/session.jsonl", tmp_path, for_resume=False) == "/a/session.jsonl"
