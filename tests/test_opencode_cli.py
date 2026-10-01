"""Real CLI + real opencode wrapper + fake opencode binary (no model calls)."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from sam import config as sam_config
from sam import harness as sam_harness
from sam import registry as sam_registry

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "tests" / "fixtures" / "fake_opencode.py"


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
    monkeypatch.setenv("FAKE_OC_MODE", "ok")
    work = tmp_path / "work"
    work.mkdir()
    task = work / "task.md"
    task.write_text("Reply with exactly: HELLO\n")
    r = cli("init")
    assert r.returncode == 0, r.stderr
    return argparse.Namespace(home=home, tmp=tmp_path, task=task, work=work,
                              dump=tmp_path / "dump.json")


def dump(ocenv):
    return json.loads(ocenv.dump.read_text())


def spawn(ocenv, name, *extra):
    return cli("spawn", "--name", name, "--task", str(ocenv.task), "--harness", "opencode",
               *extra)


def test_spawn_wait_result_with_effort(ocenv):
    r = spawn(ocenv, "oc-one", "--model", "opencode/fake-m", "--effort", "low", "--no-space")
    assert r.returncode == 0, r.stderr
    w = cli("wait", "oc-one")
    assert w.returncode == 0, w.stderr
    assert last_json(w)["status"] == "completed"
    assert last_json(cli("result", "oc-one"))["result"] == "fake answer"
    agent = sam_registry.load_registry()["agents"][0]
    assert agent["harness"] == "opencode"
    assert agent["effort"] == "low" and agent["thinking"] is None
    argv = dump(ocenv)["argv"]
    assert argv[:3] == ["run", "--format", "json"]
    assert "--auto" in argv
    assert argv[argv.index("-m") + 1] == "opencode/fake-m"
    assert argv[argv.index("--variant") + 1] == "low"
    assert "--session" not in argv
    assert Path(agent["session_path"]).read_text().strip() == "ses_fake0001"
    launches = [json.loads(l) for l in (ocenv.home / "launches.jsonl").read_text().splitlines()]
    assert launches[-1]["harness"] == "opencode"


def test_spawn_default_model_and_scoped_sam_model(ocenv, monkeypatch):
    monkeypatch.setenv("SAM_MODEL", "gemini-3.8-flash-low")
    monkeypatch.setenv("SAM_MODEL_HARNESS", "agy")
    r = spawn(ocenv, "oc-default", "--no-space")
    assert r.returncode == 0, r.stderr
    assert cli("wait", "oc-default").returncode == 0
    agent = sam_registry.load_registry()["agents"][0]
    assert agent["model"] == sam_config.OPENCODE_DEFAULT_MODEL
    d = dump(ocenv)
    assert d["argv"][d["argv"].index("-m") + 1] == sam_config.OPENCODE_DEFAULT_MODEL
    assert d["env"]["SAM_MODEL_HARNESS"] == "opencode"
    assert d["env"]["SAM_MODEL"] == sam_config.OPENCODE_DEFAULT_MODEL


def test_spawn_rejects_thinking_and_bad_effort(ocenv):
    r = spawn(ocenv, "oc-bad1", "--thinking", "low", "--no-space")
    assert r.returncode == 2
    assert "--thinking cannot be used with --harness opencode; use --effort" in r.stderr
    r = spawn(ocenv, "oc-bad2", "--effort", "xhigh", "--no-space")
    assert r.returncode == 2
    assert "--effort for --harness opencode must be one of: minimal, low, medium, high, max" in r.stderr
    r = cli("spawn", "--name", "oc-bad3", "--task", str(ocenv.task), "--harness", "pi",
            "--effort", "low", "--no-space")
    assert r.returncode == 2
    assert "--effort requires --harness agy or --harness opencode" in r.stderr
    assert sam_registry.load_registry()["agents"] == []


@pytest.mark.skipif(not sam_harness.OPENCODE_RESUME_SUPPORTED,
                    reason="discovery D6: opencode resume unsupported")
def test_resume_continues_session(ocenv):
    assert spawn(ocenv, "oc-res", "--model", "opencode/fake-m", "--no-space").returncode == 0
    assert cli("wait", "oc-res").returncode == 0
    follow = ocenv.work / "follow.md"
    follow.write_text("Follow up\n")
    r = cli("resume", "oc-res", "--task", str(follow), "--no-space")
    assert r.returncode == 0, r.stderr
    assert cli("wait", "oc-res").returncode == 0
    argv = dump(ocenv)["argv"]
    assert argv[argv.index("--session") + 1] == "ses_fake0001"
    agent = sam_registry.load_registry()["agents"][0]
    data = json.loads(Path(agent["result_path"]).read_text())
    assert data["session_continued"] is True
    assert data["session_id"] == "ses_fake0001"
    assert agent["run_id"] == 2


@pytest.mark.skipif(sam_harness.OPENCODE_RESUME_SUPPORTED,
                    reason="discovery D6: opencode resume supported")
def test_resume_refused_when_unsupported(ocenv):
    assert spawn(ocenv, "oc-res", "--model", "opencode/fake-m", "--no-space").returncode == 0
    assert cli("wait", "oc-res").returncode == 0
    follow = ocenv.work / "follow.md"
    follow.write_text("Follow up\n")
    r = cli("resume", "oc-res", "--task", str(follow), "--no-space")
    assert r.returncode == 1
    assert "resume is not supported for harness opencode" in r.stderr
    assert sam_registry.load_registry()["agents"][0]["run_id"] == 1


def test_restart_starts_fresh_session(ocenv, monkeypatch):
    assert spawn(ocenv, "oc-rst", "--model", "opencode/fake-m", "--no-space").returncode == 0
    assert cli("wait", "oc-rst").returncode == 0
    monkeypatch.setenv("FAKE_OC_SESSION", "ses_fake0002")
    r = cli("restart", "oc-rst", "--no-space")
    assert r.returncode == 0, r.stderr
    assert cli("wait", "oc-rst").returncode == 0
    assert "--session" not in dump(ocenv)["argv"]
    agent = sam_registry.load_registry()["agents"][0]
    assert agent["session_path"].endswith("run-002/session.jsonl")
    assert Path(agent["session_path"]).read_text().strip() == "ses_fake0002"


def test_init_installs_opencode_wrapper(ocenv):
    target = ocenv.home / "bin" / "opencode-wrapper"
    assert target.read_bytes() == (ROOT / "wrapper" / "opencode_wrapper.py").read_bytes()
    assert os.access(target, os.X_OK)


def test_spawn_deferred_by_spacing_gate(ocenv, monkeypatch):
    monkeypatch.setenv("SAM_SLOT_WAIT_S", "0")
    assert spawn(ocenv, "oc-g1", "--model", "opencode/fake-m").returncode == 0
    r = spawn(ocenv, "oc-g2", "--model", "opencode/fake-m")
    assert r.returncode == 6
    assert "NOT an error" in r.stderr