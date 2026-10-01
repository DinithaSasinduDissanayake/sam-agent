"""Retry classification for opencode results (infra_hint) + reset parsing (F23) + CLI flow."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from sam import harness as sam_harness
from sam import registry as sam_registry
from sam import retry as sam_retry

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "tests" / "fixtures" / "fake_opencode.py"


def test_detect_trusts_quota_hint():
    r = {"final_state_hint": "failed", "exit_code": 1, "duration_ms": 5000,
         "infra_hint": "quota", "error": "429 Too Many Requests"}
    assert sam_retry.detect_infra_failure(r)[0] == "quota"


def test_detect_hint_none_ignores_log_markers():
    r = {"final_state_hint": "failed", "exit_code": 1, "duration_ms": 5000,
         "infra_hint": None, "error": "SyntaxError"}
    log = '{"type":"text","part":{"text":"RESOURCE_EXHAUSTED code 429"}}'
    assert sam_retry.detect_infra_failure(r, log_text=log) == (None, "")


def test_detect_startup_network_hint_respects_duration():
    r = {"final_state_hint": "failed", "exit_code": 1, "duration_ms": 5000,
         "infra_hint": "startup-network", "error": "ECONNRESET"}
    assert sam_retry.detect_infra_failure(r)[0] == "startup-network"
    r["duration_ms"] = 500000
    assert sam_retry.detect_infra_failure(r) == (None, "")


def test_detect_without_hint_key_unchanged_for_agy():
    r = {"final_state_hint": "failed", "exit_code": 3, "duration_ms": 5000,
         "error": "RESOURCE_EXHAUSTED Individual quota reached"}
    assert sam_retry.detect_infra_failure(r)[0] == "quota"


def test_reset_parse_hours():
    p = sam_retry.parse_reset_seconds
    assert p("Resets in 1h2m3s") == 3723
    assert p("Resets in 2h") == 7200
    assert p("Resets in 18m6s") == 1086
    assert p("21m23s") == 1283
    assert p("Resets in 45s") == 45
    assert p("no hint here") is None


def cli(*argv, timeout=30):
    return subprocess.run([sys.executable, "-m", "sam.cli", *argv, "--json"],
                          cwd=ROOT, capture_output=True, text=True, timeout=timeout)


def last_json(proc):
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.fixture
def ocenv(tmp_path, monkeypatch):
    home = tmp_path / "sam-home"
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    exe = fakebin / "opencode"
    exe.write_text(FAKE.read_text())
    exe.chmod(0o755)
    monkeypatch.setenv("SAM_HOME", str(home))
    monkeypatch.setenv("PATH", str(fakebin) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_OC_DUMP", str(tmp_path / "dump.json"))
    task = tmp_path / "task.md"
    task.write_text("Reply with exactly: HELLO\n")
    assert cli("init").returncode == 0
    return argparse.Namespace(home=home, task=task, dump=tmp_path / "dump.json")


def test_cli_wait_promotes_opencode_429_and_retry_fires(ocenv, monkeypatch):
    monkeypatch.setenv("FAKE_OC_MODE", "error429")
    r = cli("spawn", "--name", "oc-429", "--task", str(ocenv.task), "--harness", "opencode",
            "--model", "opencode/fake-m", "--no-space")
    assert r.returncode == 0, r.stderr
    w = cli("wait", "oc-429")
    assert last_json(w)["status"] == "awaiting_retry"
    q = last_json(cli("retry"))
    assert q["count"] == 1 and q["queue"][0]["kind"] == "quota"
    monkeypatch.setenv("FAKE_OC_MODE", "ok")
    monkeypatch.setenv("FAKE_OC_SESSION", "ses_fake0009")
    (ocenv.home / ".spawn_state.json").write_text(json.dumps({"last_spawn": 0, "recent": []}))
    f = cli("retry", "oc-429", "--override-reason", "test fire")
    assert f.returncode == 0, f.stderr
    assert cli("wait", "oc-429").returncode == 0
    argv = json.loads(ocenv.dump.read_text())["argv"]
    agent = sam_registry.load_registry()["agents"][0]
    assert agent["state"] == "completed"
    pointer = Path(agent["session_path"]).read_text().strip()
    if sam_harness.OPENCODE_RESUME_SUPPORTED:
        # The 429 run had already named its session: the retry continues it.
        assert argv[argv.index("--session") + 1] == "ses_fake0001"
        assert pointer == "ses_fake0001"
    else:
        assert "--session" not in argv
        assert pointer == "ses_fake0009"


def test_cli_wait_generic_403_stays_failed(ocenv, monkeypatch):
    monkeypatch.setenv("FAKE_OC_MODE", "error403")
    r = cli("spawn", "--name", "oc-403", "--task", str(ocenv.task), "--harness", "opencode",
            "--model", "opencode/fake-m", "--no-space")
    assert r.returncode == 0, r.stderr
    w = cli("wait", "oc-403")
    assert w.returncode == 1
    assert last_json(w)["status"] == "failed"
    assert last_json(cli("retry"))["count"] == 0