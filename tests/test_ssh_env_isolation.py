#!/usr/bin/env python3
"""Tests for SSH environment isolation (F1).

Verifies that SSH_CLIENT, SSH_CONNECTION, and SSH_TTY are stripped from child
environments (agy_wrapper, build_child_env, spawn, resume, restart),
while SSH_AUTH_SOCK is preserved and DBUS_SESSION_BUS_ADDRESS is configured.
"""

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sam import config as sam_config
from sam import util as sam_util
from sam.commands import spawn as spawn_cmd
from sam.commands import resume as resume_cmd
from sam.commands import restart as restart_cmd


_WRAPPER = Path(__file__).resolve().parent.parent / "wrapper" / "agy_wrapper.py"


def _load_agy_wrapper():
    spec = importlib.util.spec_from_file_location("agy_wrapper", str(_WRAPPER))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_build_child_env_strips_ssh_and_preserves_auth_sock(monkeypatch):
    monkeypatch.setenv("SSH_CLIENT", "1.2.3.4 5678 22")
    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 5678 10.0.0.1 22")
    monkeypatch.setenv("SSH_TTY", "/dev/pts/9")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ssh-agent.sock")

    env = sam_util.build_child_env("agent-test", "test-model", 0)

    assert "SSH_CLIENT" not in env
    assert "SSH_CONNECTION" not in env
    assert "SSH_TTY" not in env
    assert env.get("SSH_AUTH_SOCK") == "/tmp/ssh-agent.sock"
    assert env.get("SAM_AGENT_ID") == "agent-test"
    assert env.get("SAM_MODEL") == "test-model"


def test_build_child_env_sets_dbus_if_missing(monkeypatch):
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    bus_path = f"/run/user/{os.getuid()}/bus"

    env = sam_util.build_child_env("agent-test", "test-model", 0)
    if os.path.exists(bus_path):
        assert env.get("DBUS_SESSION_BUS_ADDRESS") == f"unix:path={bus_path}"


def test_agy_wrapper_strips_ssh_keys_from_child(tmp_path, monkeypatch):
    dump_file = tmp_path / "env_dump.json"
    fake_agy = tmp_path / "bin" / "agy"
    fake_agy.parent.mkdir(parents=True)
    fake_agy.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"with open({str(dump_file)!r}, 'w') as f:\n"
        "    json.dump(dict(os.environ), f)\n"
        "print(json.dumps({'conversation_id': 'c-1', 'status': 'SUCCESS', 'response': 'ok'}))\n"
    )
    fake_agy.chmod(0o755)

    monkeypatch.setenv("PATH", f"{fake_agy.parent}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SSH_CLIENT", "1.2.3.4 5678 22")
    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 5678 10.0.0.1 22")
    monkeypatch.setenv("SSH_TTY", "/dev/pts/3")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ssh-agent-test.sock")

    run_dir = tmp_path / "run-001"
    run_dir.mkdir()
    task_file = tmp_path / "task.md"
    task_file.write_text("Hello agy")
    session_file = tmp_path / "session.jsonl"
    result_file = run_dir / "result.json"

    agy_mod = _load_agy_wrapper()
    argv = [
        "agy_wrapper.py",
        "--agent-id", "agy-f1",
        "--model", "dummy-model",
        "--session", str(session_file),
        "--task", str(task_file),
        "--result", str(result_file),
    ]
    monkeypatch.setattr(sys, "argv", argv)

    try:
        agy_mod.main()
    except SystemExit as exc:
        assert exc.code == 0

    assert dump_file.exists(), "Fake agy did not run and dump env"
    child_env = json.loads(dump_file.read_text())
    assert "SSH_CLIENT" not in child_env
    assert "SSH_CONNECTION" not in child_env
    assert "SSH_TTY" not in child_env
    assert child_env.get("SSH_AUTH_SOCK") == "/tmp/ssh-agent-test.sock"


def test_spawn_passes_stripped_env_to_popen(tmp_path, monkeypatch):
    sam_home = tmp_path / "sam"
    sam_home.mkdir()
    monkeypatch.setenv("SAM_HOME", str(sam_home))
    monkeypatch.setenv("SSH_CLIENT", "1.2.3.4 5678 22")
    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 5678 10.0.0.1 22")
    monkeypatch.setenv("SSH_TTY", "/dev/pts/1")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/auth.sock")

    from sam.commands import init_cmd
    init_args = argparse.Namespace(json=True, sam_home=str(sam_home), force=False, harness="pi")
    init_cmd.run(init_args)

    task_file = tmp_path / "task.md"
    task_file.write_text("do work")

    captured_env = {}

    class DummyProc:
        pid = 12345
        def poll(self):
            return None

    def fake_popen(argv, **kwargs):
        captured_env.update(kwargs.get("env", {}))
        return DummyProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    # Avoid acquire_spawn_slot deferral
    monkeypatch.setattr(spawn_cmd.sam_proc, "acquire_spawn_slot", lambda *a, **kw: {"granted": True, "bypassed": False})

    args = argparse.Namespace(
        name="test-spawn-env",
        task=str(task_file),
        harness="pi",
        model="fake-model",
        thinking=None,
        effort=None,
        cwd=None,
        no_space=True,
        json=True,
        sam_home=str(sam_home),
    )
    rc = spawn_cmd.run(args)
    assert rc == 0
    assert "SSH_CLIENT" not in captured_env
    assert "SSH_CONNECTION" not in captured_env
    assert "SSH_TTY" not in captured_env
    assert captured_env.get("SSH_AUTH_SOCK") == "/tmp/auth.sock"


def test_resume_and_restart_pass_stripped_env(tmp_path, monkeypatch):
    sam_home = tmp_path / "sam"
    sam_home.mkdir()
    monkeypatch.setenv("SAM_HOME", str(sam_home))
    monkeypatch.setenv("SSH_CLIENT", "1.2.3.4 5678 22")
    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 5678 10.0.0.1 22")
    monkeypatch.setenv("SSH_TTY", "/dev/pts/1")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/auth.sock")

    from sam.commands import init_cmd
    init_cmd.run(argparse.Namespace(json=True, sam_home=str(sam_home), force=False, harness="pi"))

    task_file = tmp_path / "task.md"
    task_file.write_text("do work")

    # Seed agent in registry
    from sam import registry as sam_registry
    agent_id = "agent-resume-env"
    agent_dir = sam_home / "agents" / agent_id
    run_dir = agent_dir / "run-001"
    run_dir.mkdir(parents=True)
    session_file = agent_dir / "session.jsonl"
    session_file.write_text("session data\n")
    result_file = run_dir / "result.json"
    result_file.write_text(json.dumps({"status": "completed", "result": "done"}))

    reg = {
        "version": 1,
        "agents": [
            {
                "id": agent_id,
                "name": "resume-env-agent",
                "harness": "pi",
                "model": "fake-model",
                "state": "completed",
                "session_path": str(session_file),
                "task_path": str(task_file),
                "result_path": str(result_file),
                "run_count": 1,
                "created_at": "2026-10-01T12:00:00Z",
                "depth": 0,
            }
        ]
    }
    sam_registry.save_registry(reg)

    captured_env = {}
    class DummyProc:
        pid = 12346
        def poll(self):
            return None

    def fake_popen(argv, **kwargs):
        captured_env.clear()
        captured_env.update(kwargs.get("env", {}))
        return DummyProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    # Test resume
    r_args = argparse.Namespace(
        id_or_name="resume-env-agent",
        name=None,
        task=str(task_file),
        model=None,
        harness=None,
        thinking=None,
        effort=None,
        json=True,
        sam_home=str(sam_home),
        _infra_retry=False,
    )
    rc = resume_cmd.run(r_args)
    assert rc == 0
    assert "SSH_CLIENT" not in captured_env
    assert "SSH_CONNECTION" not in captured_env
    assert "SSH_TTY" not in captured_env
    assert captured_env.get("SSH_AUTH_SOCK") == "/tmp/auth.sock"

    # Reset agent state for restart
    reg = sam_registry.load_registry()
    reg["agents"][0]["state"] = "completed"
    sam_registry.save_registry(reg)

    # Test restart
    rst_args = argparse.Namespace(
        id_or_name="resume-env-agent",
        name=None,
        task=str(task_file),
        harness="pi",
        model="fake-model",
        thinking=None,
        effort=None,
        json=True,
        sam_home=str(sam_home),
    )
    rc = restart_cmd.run(rst_args)
    assert rc == 0
    assert "SSH_CLIENT" not in captured_env
    assert "SSH_CONNECTION" not in captured_env
    assert "SSH_TTY" not in captured_env
    assert captured_env.get("SSH_AUTH_SOCK") == "/tmp/auth.sock"
