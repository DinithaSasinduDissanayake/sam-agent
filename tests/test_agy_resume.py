#!/usr/bin/env python3
"""Strict agy resume tests: fake envelope pass + miss cases (stdlib only)."""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

_WRAPPER = Path(__file__).resolve().parent.parent / "wrapper" / "agy_wrapper.py"


def load_wrapper():
    spec = importlib.util.spec_from_file_location("agy_wrapper", str(_WRAPPER))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_layout(tmp_path, pointer_text=None):
    agent_root = tmp_path / "agent"
    run_dir = agent_root / "run-001"
    run_dir.mkdir(parents=True)
    task = tmp_path / "task.md"
    task.write_text("do thing\n")
    session = agent_root / "conv.txt"
    if pointer_text is not None:
        session.write_text(pointer_text)
    result = run_dir / "result.json"
    return agent_root, run_dir, task, session, result


class FakeStdout:
    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    def read(self, n):
        if self._pos >= len(self._data):
            return b""
        chunk = self._data[self._pos:self._pos + n]
        self._pos += n
        return chunk


class FakeChild:
    def __init__(self, data: bytes, returncode=0):
        self.stdout = FakeStdout(data)
        self.returncode = returncode
        self.argv = None

    def wait(self):
        return self.returncode


def run_main(monkeypatch, capsys, argv, output_bytes, returncode=0):
    mod = load_wrapper()
    calls = {"popen": 0}

    def fake_which(name):
        return "/usr/bin/agy" if name == "agy" else None

    def fake_popen(*a, **k):
        calls["popen"] += 1
        child = FakeChild(output_bytes, returncode)
        child.argv = a[0] if a else k.get("args")
        return child

    monkeypatch.setattr(mod.shutil, "which", fake_which)
    monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exc:
        mod.main()
    err = capsys.readouterr().err
    return mod, exc.value.code, err, calls


def test_canonical_preferred_over_alias():
    mod = load_wrapper()
    raw = json.dumps({"id": "alias-1", "conversation_id": "conv-exact",
                      "response": "hi"}).encode()
    assert mod._extract_conversation_id(raw) == "conv-exact"
    val, key = mod._extract_conversation_id_with_key(raw)
    assert (val, key) == ("conv-exact", "conversation_id")


def test_alias_warns_to_stderr(tmp_path, monkeypatch, capsys):
    _, _, task, session, result = make_layout(tmp_path, "conv-old\n")
    raw = (json.dumps({"sessionId": "conv-alias", "response": "ok"}) + "\n").encode()
    argv = ["agy-wrapper", "--agent-id", "a1", "--model", "m",
            "--session", str(session), "--task", str(task),
            "--result", str(result), "--resume"]
    _, code, err, calls = run_main(monkeypatch, capsys, argv, raw)
    assert code == 0
    assert calls["popen"] == 1
    assert "alias key 'sessionId'" in err
    assert session.read_text().strip() == "conv-alias"


def test_resume_missing_pointer_exits_3_no_agy_call(tmp_path, monkeypatch, capsys):
    _, _, task, session, result = make_layout(tmp_path, None)
    argv = ["agy-wrapper", "--agent-id", "a1", "--model", "m",
            "--session", str(session), "--task", str(task),
            "--result", str(result), "--resume"]
    _, code, err, calls = run_main(monkeypatch, capsys, argv, b"")
    assert code == 3
    assert "no conversation_id, use spawn not resume" in err
    assert calls["popen"] == 0


def test_resume_empty_pointer_exits_3(tmp_path, monkeypatch, capsys):
    _, _, task, session, result = make_layout(tmp_path, "   \n")
    argv = ["agy-wrapper", "--agent-id", "a1", "--model", "m",
            "--session", str(session), "--task", str(task),
            "--result", str(result), "--resume"]
    _, code, err, calls = run_main(monkeypatch, capsys, argv, b"")
    assert code == 3
    assert "no conversation_id, use spawn not resume" in err
    assert calls["popen"] == 0


def test_resume_envelope_miss_exits_3_preserves_pointer(tmp_path, monkeypatch, capsys):
    _, _, task, session, result = make_layout(tmp_path, "conv-old\n")
    raw = b"plain log line, no json envelope\n"
    argv = ["agy-wrapper", "--agent-id", "a1", "--model", "m",
            "--session", str(session), "--task", str(task),
            "--result", str(result), "--resume"]
    _, code, err, calls = run_main(monkeypatch, capsys, argv, raw)
    assert code == 3
    assert "resume not continued" in err
    assert session.read_text().strip() == "conv-old"  # pointer untouched
    data = json.loads(result.read_text())
    assert data["conversation_id"] == "conv-old"


def test_resume_pass_updates_pointer(tmp_path, monkeypatch, capsys):
    _, _, task, session, result = make_layout(tmp_path, "conv-old\n")
    raw = (json.dumps({"conversation_id": "conv-new",
                       "response": "done"}) + "\n").encode()
    argv = ["agy-wrapper", "--agent-id", "a1", "--model", "m",
            "--session", str(session), "--task", str(task),
            "--result", str(result), "--resume"]
    _, code, err, calls = run_main(monkeypatch, capsys, argv, raw)
    assert code == 0
    assert session.read_text().strip() == "conv-new"
    data = json.loads(result.read_text())
    assert data["conversation_id"] == "conv-new"


def test_spawn_miss_warns_not_exit_3(tmp_path, monkeypatch, capsys):
    _, _, task, session, result = make_layout(tmp_path, None)
    raw = b"no envelope here\n"
    argv = ["agy-wrapper", "--agent-id", "a1", "--model", "m",
            "--session", str(session), "--task", str(task),
            "--result", str(result)]
    _, code, err, calls = run_main(monkeypatch, capsys, argv, raw)
    assert code == 0  # spawn tolerates miss with warning
    assert "fresh conversation" in err
