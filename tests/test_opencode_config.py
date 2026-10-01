"""opencode harness registration, per-harness model defaults, SAM_MODEL scoping (F13)."""

import json

import pytest

from sam import config as sam_config
from sam import util as sam_util


def test_harness_wrappers_has_opencode(tmp_path):
    assert sam_config.HARNESS_WRAPPERS["opencode"] == "opencode-wrapper"
    assert sam_config.wrapper_path(tmp_path, "opencode").name == "opencode-wrapper"


def test_resolve_model_opencode_default_ignores_pi_model():
    cfg = {"defaults": {"model": "pi-only-model"}}
    assert sam_config.resolve_model(None, "opencode", cfg) == sam_config.OPENCODE_DEFAULT_MODEL


def test_resolve_model_opencode_config_override():
    cfg = {"defaults": {"opencode_model": "opencode/zzz-free"}}
    assert sam_config.resolve_model(None, "opencode", cfg) == "opencode/zzz-free"


def test_sam_model_scoped_to_its_harness(monkeypatch):
    monkeypatch.setenv("SAM_MODEL", "gemini-x")
    monkeypatch.setenv("SAM_MODEL_HARNESS", "agy")
    assert sam_config.resolve_model(None, "opencode", {}) == sam_config.OPENCODE_DEFAULT_MODEL
    assert sam_config.resolve_model(None, "agy", {}) == "gemini-x"


def test_sam_model_without_marker_still_applies(monkeypatch):
    monkeypatch.setenv("SAM_MODEL", "legacy-x")
    monkeypatch.delenv("SAM_MODEL_HARNESS", raising=False)
    assert sam_config.resolve_model(None, "opencode", {}) == "legacy-x"
    assert sam_config.resolve_model(None, "pi", {}) == "legacy-x"


def test_build_child_env_sets_and_clears_model_harness(monkeypatch):
    env = sam_util.build_child_env("a1", "m", 0, harness="opencode")
    assert env["SAM_MODEL_HARNESS"] == "opencode"
    monkeypatch.setenv("SAM_MODEL_HARNESS", "agy")
    env = sam_util.build_child_env("a1", "m", 0)
    assert "SAM_MODEL_HARNESS" not in env


def test_config_rejects_non_string_opencode_model(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({
        "defaults": {"model": "m", "max_restarts": 1, "max_depth": 4,
                     "harness": "pi", "opencode_model": 5},
        "security": {"inherit_env": True}}))
    with pytest.raises(sam_config.ConfigCorrupt):
        sam_config.load_config(tmp_path)


def test_resolve_harness_accepts_opencode(monkeypatch):
    monkeypatch.setenv("SAM_HARNESS", "opencode")
    assert sam_config.resolve_harness(None, {}) == "opencode"
