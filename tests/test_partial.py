#!/usr/bin/env python3
"""Item 3 acceptance: result_partial + PARTIAL.md + state `partial`.

Fixture tests (deterministic) + one live wrapper-process smoke
(envelope captured, agy child SIGKILLed mid-response).
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from sam import state as sam_state

_WRAPPER = Path(__file__).resolve().parent.parent / "wrapper" / "agy_wrapper.py"


def load_wrapper():
    spec = importlib.util.spec_from_file_location("agy_wrapper", str(_WRAPPER))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_layout(tmp_path):
    agent_root = tmp_path / "agent"
    run_dir = agent_root / "run-001"
    run_dir.mkdir(parents=True)
    task = tmp_path / "task.md"
    task.write_text("do thing\n")
    session = agent_root / "conv.txt"
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


def _argv(task, session, result, resume=False):
    argv = ["agy-wrapper", "--agent-id", "a1", "--model", "m",
            "--session", str(session), "--task", str(task),
            "--result", str(result)]
    if resume:
        argv.append("--resume")
    return argv


def test_error_envelope_with_response_marks_partial(tmp_path, monkeypatch, capsys):
    """d2c87d-class: failed run, full deliverable text in ERROR envelope."""
    _, run_dir, task, session, result = make_layout(tmp_path)
    env = json.dumps({
        "conversation_id": "conv-x", "status": "ERROR",
        "response": "Deliverables complete: SUMMARY.md written to disk.",
        "error": "use of closed network connection",
    }).encode()
    _, code, _, _ = run_main(monkeypatch, capsys, _argv(task, session, result),
                             env, returncode=1)
    assert code == 1  # underlying failure code preserved
    data = json.loads(result.read_text())
    assert data["final_state_hint"] == "partial"
    assert data["result_partial"].startswith("Deliverables complete")
    assert data["partial_path"] == str(run_dir / "PARTIAL.md")
    assert data["result"] is None  # final-only: not a clean completion
    part = run_dir / "PARTIAL.md"
    assert part.is_file()
    text = part.read_text()
    assert "verify before respawn" in text.lower()
    assert "Deliverables complete" in text
    assert "operator error" in text


def test_signal_death_with_response_marks_partial(tmp_path, monkeypatch, capsys):
    """Kill mid-response class: child killed by signal, envelope captured."""
    _, run_dir, task, session, result = make_layout(tmp_path)
    env = json.dumps({
        "conversation_id": "conv-y", "status": "ERROR",
        "response": "partial turn output before the kill",
        "error": "killed",
    }).encode()
    _, code, _, _ = run_main(monkeypatch, capsys, _argv(task, session, result),
                             env, returncode=-9)
    assert code == 137  # 128 + SIGKILL
    data = json.loads(result.read_text())
    assert data["final_state_hint"] == "partial"
    assert data["exit_signal"] == 9
    assert data["result_partial"] == "partial turn output before the kill"
    assert (run_dir / "PARTIAL.md").is_file()


def test_error_envelope_without_response_stays_failed(tmp_path, monkeypatch, capsys):
    """Else-stays-failed: no captured response -> state failed, no file."""
    _, run_dir, task, session, result = make_layout(tmp_path)
    env = json.dumps({
        "conversation_id": "conv-z", "status": "ERROR",
        "response": "", "error": "Individual quota reached.",
    }).encode()
    _, code, _, _ = run_main(monkeypatch, capsys, _argv(task, session, result),
                             env, returncode=1)
    assert code == 1
    data = json.loads(result.read_text())
    assert data["final_state_hint"] == "failed"
    assert "result_partial" not in data
    assert not (run_dir / "PARTIAL.md").exists()


def test_resolve_maps_partial_hint_and_is_terminal():
    assert "partial" in sam_state.TERMINAL_STATES
    entry = {"state": "running", "pid": 4194303, "pid_start_time": 1,
             "launch_deadline_at": None,
             "result_path": "/nonexistent/result.json"}
    # pid dead + result.json hint partial -> resolves partial
    res = {"final_state_hint": "partial"}
    state = sam_state.resolve_agent_state(
        entry, 1, proc_alive_fn=lambda p: False,
        read_start_time_fn=lambda p: None,
        read_result_fn=lambda p: res)
    assert state == "partial"
    # registry write-back of terminal "partial" short-circuits
    entry2 = {"state": "partial", "pid": 12345}
    assert sam_state.resolve_agent_state(entry2, 1) == "partial"


def test_live_wrapper_smoke_partial(tmp_path, monkeypatch):
    """Live process: fake agy prints deliverable envelope, SIGKILLs itself."""
    _, run_dir, task, session, result = make_layout(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_agy = bin_dir / "agy"
    fake_agy.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "print('##AGY_BEGIN_s')\n"
        "print(json.dumps({\"conversation_id\": \"smoke-partial\","
        " \"status\": \"ERROR\", \"response\": \"LIVE PARTIAL DELIVERABLE\","
        " \"error\": \"boom\"}))\n"
        "print('##AGY_END_s')\n"
        "sys.stdout.flush()\n"
        "os.kill(os.getpid(), 9)\n")
    fake_agy.chmod(0o700)
    env = os.environ.copy()
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    proc = subprocess.run(
        [sys.executable, str(_WRAPPER), "--agent-id", "a1", "--model", "m",
         "--session", str(session), "--task", str(task),
         "--result", str(result)],
        capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 137, proc.stderr
    data = json.loads(result.read_text())
    assert data["final_state_hint"] == "partial"
    assert data["result_partial"] == "LIVE PARTIAL DELIVERABLE"
    part = run_dir / "PARTIAL.md"
    assert part.is_file()
    assert "LIVE PARTIAL DELIVERABLE" in part.read_text()
