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
    calls = {"popen": 0, "argv": None}

    def fake_which(name):
        return "/usr/bin/agy" if name == "agy" else None

    def fake_popen(*a, **k):
        calls["popen"] += 1
        child = FakeChild(output_bytes, returncode)
        child.argv = a[0] if a else k.get("args")
        calls["argv"] = child.argv
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
                      "status": "SUCCESS", "response": "hi"}).encode()
    assert mod._extract_conversation_id(raw) == "conv-exact"
    val, key = mod._extract_conversation_id_with_key(raw)
    assert (val, key) == ("conv-exact", "conversation_id")


def test_unverified_alias_rejected(tmp_path, monkeypatch, capsys):
    _, _, task, session, result = make_layout(tmp_path, "conv-old\n")
    raw = (json.dumps({"sessionId": "conv-alias", "status": "SUCCESS", "response": "ok"}) + "\n").encode()
    argv = ["agy-wrapper", "--agent-id", "a1", "--model", "m",
            "--session", str(session), "--task", str(task),
            "--result", str(result), "--resume"]
    _, code, err, calls = run_main(monkeypatch, capsys, argv, raw)
    assert code == 3
    assert calls["popen"] == 1
    assert "resume_rejected" in err
    assert session.read_text().strip() == "conv-old"


def test_resume_missing_pointer_exits_3_no_agy_call(tmp_path, monkeypatch, capsys):
    _, _, task, session, result = make_layout(tmp_path, None)
    argv = ["agy-wrapper", "--agent-id", "a1", "--model", "m",
            "--session", str(session), "--task", str(task),
            "--result", str(result), "--resume"]
    _, code, err, calls = run_main(monkeypatch, capsys, argv, b"")
    assert code == 3
    assert "no conversation_id, use spawn not resume" in err
    assert calls["popen"] == 0
    data = json.loads(result.read_text())
    assert data["exit_code"] == code
    assert data["final_state_hint"] == "failed"
    assert data["session_continued"] is False


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
    assert "resume_rejected" in err
    assert session.read_text().strip() == "conv-old"  # pointer untouched
    data = json.loads(result.read_text())
    assert data["conversation_id"] == "conv-old"
    assert data["exit_code"] == code
    assert data["final_state_hint"] == "failed"
    assert data["session_continued"] is False
    assert data["result"] is None


def test_resume_pass_retains_same_conversation(tmp_path, monkeypatch, capsys):
    _, _, task, session, result = make_layout(tmp_path, "conv-old\n")
    raw = (json.dumps({"conversation_id": "conv-old", "status": "SUCCESS",
                       "response": "done"}) + "\n").encode()
    argv = ["agy-wrapper", "--agent-id", "a1", "--model", "m",
            "--session", str(session), "--task", str(task),
            "--result", str(result), "--resume"]
    _, code, err, calls = run_main(monkeypatch, capsys, argv, raw)
    assert code == 0
    assert session.read_text() == "conv-old\n"
    data = json.loads(result.read_text())
    assert data["conversation_id"] == "conv-old"
    assert data["session_continued"] is True
    assert data["result"] == "done"
    assert calls["argv"][calls["argv"].index("--conversation") + 1] == "conv-old"


@pytest.mark.parametrize("raw", [
    b'{"conversation_id":',
    b'{"id":"conv-old","status":"SUCCESS","response":"wrong ID field"}',
    b'{"conversation_id":"conv-new","status":"SUCCESS","response":"fresh"}',
    b'{"conversation_id":"conv-old","status":"ERROR","response":""}',
    b'{"conversation_id":"conv-old","status":"SUCCESS","response":{}}',
    b'{"event":"init","conversation_id":"conv-old"}',
    b'{"status":"SUCCESS","response":"text","tool":{"conversation_id":"conv-old"}}',
])
def test_bad_resume_envelopes_fail_consistently(tmp_path, monkeypatch, capsys, raw):
    _, _, task, session, result = make_layout(tmp_path, "conv-old\n")
    argv = ["agy-wrapper", "--agent-id", "a1", "--model", "m",
            "--session", str(session), "--task", str(task),
            "--result", str(result), "--resume"]
    _, code, _, calls = run_main(monkeypatch, capsys, argv, raw)
    data = json.loads(result.read_text())
    assert code == data["exit_code"] == 3
    assert data["final_state_hint"] == "failed"
    assert data["session_continued"] is False
    assert data["result"] is None
    assert session.read_text() == "conv-old\n"
    assert "--conversation" in calls["argv"]


def test_failed_child_does_not_replace_pointer(tmp_path, monkeypatch, capsys):
    _, _, task, session, result = make_layout(tmp_path, "conv-old\n")
    argv = ["agy-wrapper", "--agent-id", "a1", "--model", "m",
            "--session", str(session), "--task", str(task),
            "--result", str(result), "--resume"]
    raw = b'{"conversation_id":"conv-old","status":"SUCCESS","response":"partial"}'
    _, code, _, _ = run_main(monkeypatch, capsys, argv, raw, returncode=1)
    data = json.loads(result.read_text())
    assert code == data["exit_code"] == 1
    assert data["final_state_hint"] == "failed"
    assert data["session_continued"] is False
    assert session.read_text() == "conv-old\n"


def test_nonzero_child_exit_matches_metadata(tmp_path, monkeypatch, capsys):
    _, _, task, session, result = make_layout(tmp_path, "conv-old\n")
    argv = ["agy-wrapper", "--agent-id", "a1", "--model", "m",
            "--session", str(session), "--task", str(task),
            "--result", str(result), "--resume"]
    raw = b'{"conversation_id":"conv-old","status":"SUCCESS","response":"partial"}'
    _, code, _, _ = run_main(monkeypatch, capsys, argv, raw, returncode=17)
    data = json.loads(result.read_text())
    assert code == data["exit_code"] == 17
    assert data["result"] is None and data["session_continued"] is False
    assert session.read_text() == "conv-old\n"


def test_spawn_miss_warns_not_exit_3(tmp_path, monkeypatch, capsys):
    _, _, task, session, result = make_layout(tmp_path, None)
    raw = b"no envelope here\n"
    argv = ["agy-wrapper", "--agent-id", "a1", "--model", "m",
            "--session", str(session), "--task", str(task),
            "--result", str(result)]
    _, code, err, calls = run_main(monkeypatch, capsys, argv, raw)
    assert code == 0  # spawn tolerates miss with warning
    assert "fresh conversation" in err


def test_first_run_429_persists_pointer_for_resume(tmp_path, monkeypatch, capsys):
    """Acceptance (item 2): first-run-429 -> pointer exists -> resume proceeds.

    The old SUCCESS gate dropped ERROR-envelope ids, making every
    first-attempt failure unresumable (the IISA access-gap-n2 death).
    """
    _, _, task, session, result = make_layout(tmp_path, None)
    err429 = json.dumps({
        "conversation_id": "conv-first-429",
        "status": "ERROR",
        "response": "",
        "error": "Individual quota reached. Resets in 18m6s.",
    }).encode()
    argv = ["agy-wrapper", "--agent-id", "a1", "--model", "m",
            "--session", str(session), "--task", str(task),
            "--result", str(result)]
    _, code, _, _ = run_main(monkeypatch, capsys, argv, err429, returncode=1)
    # Run 1 failed (as expected) — but the pointer must now exist.
    assert code == 1
    assert session.read_text().strip() == "conv-first-429"
    data = json.loads(result.read_text())
    assert data["conversation_id"] == "conv-first-429"

    # Run 2: resume with the SAME id, clean SUCCESS envelope -> proceeds.
    ok_env = json.dumps({
        "conversation_id": "conv-first-429",
        "status": "SUCCESS",
        "response": "recovered after quota reset",
    }).encode()
    argv2 = ["agy-wrapper", "--agent-id", "a2", "--model", "m",
             "--session", str(session), "--task", str(task),
             "--result", str(tmp_path / "agent" / "run-002" / "r2.json"),
             "--resume"]
    (tmp_path / "agent" / "run-002").mkdir(exist_ok=True)
    _, code2, err2, calls = run_main(monkeypatch, capsys, argv2, ok_env)
    assert code2 == 0
    assert calls["popen"] == 1  # actually launched, not rejected
    assert "resume_rejected" not in err2
    data2 = json.loads((tmp_path / "agent" / "run-002" / "r2.json").read_text())
    assert data2["session_continued"] is True


def test_resumed_then_failed_midflight_429_keeps_pointer(tmp_path, monkeypatch, capsys):
    """Acceptance (item 2 message split): resumed fine, died mid-flight.

    Opposite parent action from resume_rejected: keep the pointer,
    back off, resume again (do NOT spawn fresh).
    """
    _, _, task, session, result = make_layout(tmp_path, "conv-live\n")
    err429 = json.dumps({
        "conversation_id": "conv-live",
        "status": "ERROR",
        "response": "partial turn output before quota hit",
        "error": "Individual quota reached. Resets in 21m23s.",
    }).encode()
    argv = ["agy-wrapper", "--agent-id", "a1", "--model", "m",
            "--session", str(session), "--task", str(task),
            "--result", str(result), "--resume"]
    _, code, err, _ = run_main(monkeypatch, capsys, argv, err429, returncode=1)
    assert code == 3
    assert "resumed_then_failed" in err
    assert "resume_rejected" not in err
    assert "spawn fresh" not in err.split("resumed_then_failed")[1][:200] or \
           "do not spawn fresh" in err
    assert session.read_text().strip() == "conv-live"  # pointer kept
    data = json.loads(result.read_text())
    assert data["conversation_id"] == "conv-live"
    assert data["error_kind"] == "resumed_then_failed"
    assert data["session_continued"] is False  # not clean — errored
