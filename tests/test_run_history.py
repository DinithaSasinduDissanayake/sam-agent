"""Stage-one regressions. Real CLI/wrappers with deterministic independent workers.

Fixtures model agy's documented headless terminal envelope and pi's v3
id/parentId session tree and stopReason semantics. No live model calls.
"""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

from sam import config, harness, registry, util
from sam.commands import restart, resume, spawn, status
from sam.commands import result as result_cmd
from sam import run_times

ROOT = Path(__file__).resolve().parents[1]


def load_pi():
    spec = importlib.util.spec_from_file_location("pi_wrapper", ROOT / "wrapper/pi_wrapper.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def message(eid, parent, text, role="assistant", stop="stop", blocks=None):
    msg = {"role": role, "content": blocks or [{"type": "text", "text": text}],
           "timestamp": eid}
    if role == "assistant":
        msg["stopReason"] = stop
    return {"type": "message", "id": eid, "parentId": parent, "message": msg}


def append_entries(path, entries, mode="a"):
    with path.open(mode) as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "sam-home"
    monkeypatch.setenv("SAM_HOME", str(home))
    for key in ("SAM_HARNESS", "SAM_MODEL", "SAM_AGENT_ID", "SAM_DEPTH", "SAM_ROOT_ID"):
        monkeypatch.delenv(key, raising=False)
    config.init_sam_home()
    for name in ("pi", "agy"):
        wrapper = home / "bin" / f"{name}-wrapper"
        shutil.copyfile(ROOT / "wrapper" / f"{name}_wrapper.py", wrapper)
        wrapper.chmod(0o700)
    return home


def seed(home, kind, pointer="conv-old\n", archived=True, legacy=True):
    root = home / "agents/a1"
    run = root / "run-001"
    run.mkdir(parents=True)
    task = home / "tasks/a1.md" if legacy else run / "task.md"
    task.write_text("original task\n")
    session = root / "session.jsonl"
    if kind == "pi":
        append_entries(session, [message("old", None, "old answer")])
    elif pointer is not None:
        session.write_text(pointer)
    (run / "output.log").write_text("old log\n")
    (run / "result.json").write_text(json.dumps({"final_state_hint": "completed", "result": "old answer", "task_path": str(task)}))
    entry = {"id": "a1", "name": "worker", "harness": kind, "state": "completed",
             "session_path": str(session), "task_path": str(task), "cwd": str(home),
             "model": "fake-model", "run_id": 1, "run_count": 1, "restart_count": 0,
             "log_path": str(run / "output.log"), "result_path": str(run / "result.json"),
             "archived": archived, "archived_at": "yesterday", "prune_reason": "stale",
             "pruned": archived}
    registry.save_registry({"version": 1, "agents": [entry]})
    return entry


def args(home, command="resume"):
    task = home / "followup.md"
    task.write_text("follow-up task\n")
    return argparse.Namespace(id_or_name="a1", task=str(task), json=True)


@pytest.mark.parametrize("command", [resume, restart])
@pytest.mark.parametrize("pointer", [None, "", " \n", "conv-old extra\n", '{"id":"other"}\n'])
def test_invalid_agy_pointer_is_nonmutating(home, monkeypatch, command, pointer):
    seed(home, "agy", pointer=pointer)
    before = config.registry_path().read_bytes()
    files = {str(p): p.read_bytes() for p in home.rglob("*") if p.is_file()}
    task_args = args(home)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("must not launch"))
    assert command.run(task_args) == 1
    assert config.registry_path().read_bytes() == before
    for path, data in files.items():
        assert Path(path).read_bytes() == data
    assert not (home / "agents/a1/run-002").exists()


@pytest.mark.parametrize("command", [resume, restart])
@pytest.mark.parametrize("kind", ["pi", "agy"])
def test_launch_argv_wakeup_and_legacy_retention(home, monkeypatch, capsys, command, kind):
    old = seed(home, kind)
    captured = {}

    def launch(argv, **kw):
        captured.update(argv=argv, kwargs=kw)
        return argparse.Namespace(pid=os.getpid())

    monkeypatch.setattr(subprocess, "Popen", launch)
    assert command.run(args(home)) == 0
    output = json.loads(capsys.readouterr().out)
    assert output.get("session_continued") is not True
    current = registry.load_registry()["agents"][0]
    assert current["state"] == "running"
    assert current["archived"] is False
    assert "prune_reason" not in current and "archived_at" not in current
    assert "pruned" not in current
    assert ("--resume" in captured["argv"]) == (kind == "agy")
    assert captured["kwargs"]["start_new_session"] is True
    assert captured["kwargs"]["stdin"] == subprocess.DEVNULL
    assert captured["kwargs"]["cwd"] == old["cwd"]
    new_session = old["session_path"] if kind == "agy" or command is resume else str(home / "agents/a1/run-002/session.jsonl")
    assert current["session_path"] == new_session
    assert current["task_path"] != old["task_path"]
    assert Path(current["task_path"]).read_text() == ("follow-up task\n" if command is resume else "original task\n")
    assert Path(old["task_path"]).read_text() == "original task\n"
    assert (home / "agents/a1/run-001/task.md").read_text() == "original task\n"
    assert Path(old["log_path"]).read_text() == "old log\n"
    assert json.loads(Path(old["result_path"]).read_text())["result"] == "old answer"
    # Default listing really includes the awakened running entry.
    assert status.run(argparse.Namespace(json=True, id_or_name=None)) == 0
    listed = json.loads(capsys.readouterr().out)
    entries = listed if isinstance(listed, list) else listed["agents"]
    assert any(e["id"] == "a1" for e in entries)


@pytest.mark.parametrize("command", [resume, restart])
def test_failed_popen_keeps_archive(home, monkeypatch, command):
    seed(home, "agy")

    def fail(*a, **k):
        raise OSError("fixture launch failure")

    monkeypatch.setattr(subprocess, "Popen", fail)
    assert command.run(args(home)) == 1
    entry = registry.load_registry()["agents"][0]
    assert entry["state"] == "failed"
    assert entry["archived"] is True
    assert entry["prune_reason"] == "stale"
    assert Path(entry["session_path"]).read_text() == "conv-old\n"


def test_snapshot_is_write_once(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "task.md"
    src.write_text("first")
    util.snapshot_task_file(src, dst)
    src.write_text("second")
    with pytest.raises(FileExistsError):
        util.snapshot_task_file(src, dst)
    assert dst.read_text() == "first"
    assert dst.stat().st_mode & 0o777 == 0o400


@pytest.mark.parametrize("tail", [[], [message("new", "old", "progress", stop="toolUse")],
    [message("new", "old", "partial", stop="error")],
    [message("new", "old", "partial", stop="aborted")],
    [message("new", "old", "partial", stop="length")],
    [message("new", "old", "question", role="user")],
    [message("new", "old", "progress", blocks=[{"type": "text", "text": "working"}, {"type": "toolCall", "id": "call"}])]])
def test_pi_never_reuses_old_answer(tmp_path, tail):
    mod = load_pi()
    session = tmp_path / "session.jsonl"
    append_entries(session, [message("old", None, "old answer")])
    boundary = mod._session_boundary(session)
    append_entries(session, tail)
    assert mod._extract_pi_result(session, boundary) is None


def test_pi_current_final_active_branch_and_migration(tmp_path):
    mod = load_pi()
    session = tmp_path / "session.jsonl"
    old = message("old", None, "old answer")
    append_entries(session, [old])
    boundary = mod._session_boundary(session)
    abandoned = message("abandoned", "old", "abandoned answer")
    user = message("user", "old", "follow-up", role="user")
    append_entries(session, [abandoned, user])
    assert mod._extract_pi_result(session, boundary) is None
    append_entries(session, [message("final", "user", "current answer"),
        {"type": "session_info", "id": "info", "parentId": "final", "name": "worker"}])
    assert mod._extract_pi_result(session, boundary) == "current answer"
    # A rewrite/migration assigning new IDs to history still cannot revive it.
    old["id"] = "migrated"
    append_entries(session, [old], mode="w")
    assert mod._extract_pi_result(session, boundary) is None


@pytest.mark.parametrize("raw,expected", [
    ('{"conversation_id":"c","status":"SUCCESS","response":"ok"}', "c"),
    ('{"event":"result","result":{"conversation_id":"c","status":"SUCCESS","response":"ok"}}', "c"),
    ('{"id":"c","status":"SUCCESS"}', None),
    ('{"conversationId":"c","status":"SUCCESS"}', None),
    ('{"status":"SUCCESS","tool":{"conversation_id":"c"}}', None),
    ('{"event":"init","conversation_id":"c"}', None),
    ('{"conversation_id":"c","status":"SUCCESS"}\n{"status":"ERROR"}', None),
])
def test_agy_harness_wrapper_schema_parity(raw, expected):
    spec = importlib.util.spec_from_file_location("agy_wrapper", ROOT / "wrapper/agy_wrapper.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert harness.extract_conversation_id(raw) == expected
    assert mod._extract_conversation_id(raw) == expected


def test_tracked_wrappers_match_sources():
    for name in ("pi", "agy"):
        assert (ROOT / "bin" / f"{name}-wrapper").read_bytes() == (ROOT / "wrapper" / f"{name}_wrapper.py").read_bytes()


@pytest.mark.parametrize("kind,target", [("pi", "agy"), ("agy", "pi")])
def test_cross_harness_resume_does_not_mutate(home, monkeypatch, kind, target):
    seed(home, kind)
    before = config.registry_path().read_bytes()
    request = args(home)
    request.harness = target
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("must not launch"))
    assert resume.run(request) == 1
    assert config.registry_path().read_bytes() == before
    assert not (home / "agents/a1/run-002").exists()


def test_finish_times_never_use_updated_at(tmp_path):
    path = tmp_path / "result.json"
    entry = {"created_at": "2000-01-01T00:00:00Z", "run_id": 2,
             "run_started_at": "2026-01-01T00:00:00Z",
             "updated_at": "2099-01-01T00:00:00Z", "result_path": str(path)}
    assert run_times.run_started_at(entry).year == 2026
    assert run_times.run_ended_at(entry) is None
    entry["completed_at"] = "2026-01-02T00:00:00Z"
    assert run_times.run_ended_at(entry).day == 2
    path.write_text(json.dumps({"started_at": "2026-01-03T00:00:00Z",
                                "ended_at": "2026-01-04T00:00:00Z"}))
    assert run_times.run_started_at(entry).day == 3
    assert run_times.run_ended_at(entry).day == 4
    entry["updated_at"] = "2100-01-01T00:00:00Z"
    assert run_times.run_ended_at(entry).day == 4


def test_failed_legacy_result_is_not_a_final(home, capsys):
    agent = seed(home, "pi")
    Path(agent["result_path"]).write_text(json.dumps({
        "final_state_hint": "failed", "exit_code": 1, "result": "partial answer"}))
    assert result_cmd.run(argparse.Namespace(id_or_name="a1", json=True)) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert "unavailable" in captured.err and "partial answer" not in captured.err


def test_old_pi_fallback_requires_current_run_window(home, capsys):
    agent = seed(home, "pi")
    record = message("final", None, "attributable final")
    record["message"]["timestamp"] = 1767225605000
    append_entries(Path(agent["session_path"]), [record], mode="w")
    path = Path(agent["result_path"])
    path.write_text(json.dumps({"final_state_hint": "completed",
                               "started_at": 1767225600, "ended_at": 1767225610}))
    assert result_cmd.run(argparse.Namespace(id_or_name="a1", json=False)) == 0
    assert capsys.readouterr().out.strip() == "attributable final"
    path.write_text(json.dumps({"final_state_hint": "completed",
                               "started_at": 1767225611, "ended_at": 1767225620}))
    assert result_cmd.run(argparse.Namespace(id_or_name="a1", json=False)) == 1
    assert not capsys.readouterr().out


def test_explicit_pi_null_is_not_second_guessed(home, capsys):
    agent = seed(home, "pi")
    record = message("old", None, "must not recover")
    record["message"]["timestamp"] = 1767225605000
    append_entries(Path(agent["session_path"]), [record], mode="w")
    Path(agent["result_path"]).write_text(json.dumps({
        "final_state_hint": "completed", "started_at": 1767225600,
        "ended_at": 1767225610, "result": None}))
    assert result_cmd.run(argparse.Namespace(id_or_name="a1", json=False)) == 1
    assert not capsys.readouterr().out


def test_agy_fallback_requires_successful_documented_envelope(tmp_path):
    path = tmp_path / "output.log"
    path.write_text('{"type":"result","status":"ERROR","result":"partial"}\n')
    assert result_cmd._agy_envelope_response(path) is None
    path.write_text('{"event":"result","result":{"status":"SUCCESS","response":"final"}}\n')
    assert result_cmd._agy_envelope_response(path) == "final"


FAKE_WORKER = '''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
argv = sys.argv[1:]
kind = Path(sys.argv[0]).name
Path(os.environ["ARGV_FILE"]).write_text(json.dumps(argv))
gate = os.environ.get("WORKER_GATE")
while gate and not Path(gate).exists():
    time.sleep(0.01)
if kind == "agy":
    cid = argv[argv.index("--conversation") + 1] if "--conversation" in argv else "fixture-conversation"
    print(os.environ.get("AGY_RAW", json.dumps({"conversation_id": cid, "status": "SUCCESS", "response": "current answer"})))
else:
    session = Path(argv[argv.index("--session") + 1])
    if os.environ.get("NO_RESPONSE") != "1":
        records = [json.loads(x) for x in session.read_text().splitlines()] if session.exists() else []
        parent = records[-1]["id"] if records else None
        entry = {"type":"message", "id":str(time.time_ns()), "parentId":parent,
                 "message":{"role":"assistant", "stopReason":"stop", "timestamp":time.time_ns(),
                            "content":[{"type":"text", "text":"current answer"}]}}
        with session.open("a") as f:
            f.write(json.dumps(entry) + "\\n")
sys.exit(int(os.environ.get("WORKER_EXIT", "0")))
'''


def cli(home, *argv):
    extra = ["--no-space"] if argv and argv[0] in ("spawn", "resume", "restart") else []
    return subprocess.run([sys.executable, "-m", "sam.cli", *argv, *extra, "--json"],
                          cwd=ROOT, capture_output=True, text=True, timeout=10)


def await_result(path):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if path.is_file():
            return json.loads(path.read_text())
        time.sleep(0.01)
    pytest.fail(f"wrapper did not finish: {path}")


@pytest.mark.parametrize("kind", ["pi", "agy"])
def test_real_cli_spawn_resume_restart_history(home, monkeypatch, tmp_path, kind):
    # Isolated HOME plus PATH workers prevent any real CLI/model invocation.
    monkeypatch.setenv("HOME", str(tmp_path))
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    for name in ("pi", "agy"):
        worker = fakebin / name
        worker.write_text(FAKE_WORKER)
        worker.chmod(0o700)
    monkeypatch.setenv("PATH", str(fakebin) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("ARGV_FILE", str(tmp_path / "argv.json"))
    task = home / "input.md"
    task.write_text("initial task")
    launched = cli(home, "spawn", "--name", "independent", "--task", str(task), "--model", "fixture", "--harness", kind)
    assert launched.returncode == 0, launched.stderr
    agent = registry.load_registry()["agents"][0]
    first_session = agent["session_path"]
    old_task = Path(agent["task_path"])
    first = await_result(Path(agent["result_path"]))
    assert first["result"] == "current answer"
    preserved = {p: p.read_bytes() for p in Path(agent["result_path"]).parent.iterdir() if p.is_file()}
    task.write_text("follow-up task")
    gate = tmp_path / "release"
    monkeypatch.setenv("WORKER_GATE", str(gate))
    assert cli(home, "prune", agent["id"]).returncode == 0
    launched = cli(home, "resume", agent["id"], "--task", str(task))
    assert launched.returncode == 0, launched.stderr
    assert "session_continued" not in json.loads(launched.stdout)
    agent = registry.load_registry()["agents"][0]
    assert agent["session_path"] == first_session
    assert agent["archived"] is False
    assert not Path(agent["result_path"]).exists()  # fire-and-forget, no wait
    listing = cli(home, "status")
    assert "independent" in listing.stdout
    gate.touch()
    second = await_result(Path(agent["result_path"]))
    assert second["result"] == "current answer"
    if kind == "agy":
        assert second["session_continued"] is True
        worker_argv = json.loads((tmp_path / "argv.json").read_text())
        assert worker_argv[worker_argv.index("--conversation") + 1] == "fixture-conversation"
    followup_task = Path(agent["task_path"])
    assert cli(home, "prune", agent["id"]).returncode == 0
    launched = cli(home, "restart", agent["id"])
    assert launched.returncode == 0, launched.stderr
    agent = registry.load_registry()["agents"][0]
    third = await_result(Path(agent["result_path"]))
    assert third["result"] == "current answer"
    assert agent["archived"] is False
    assert len({old_task, followup_task, Path(agent["task_path"])}) == 3
    assert old_task.read_text() == "initial task"
    assert followup_task.read_text() == Path(agent["task_path"]).read_text() == "follow-up task"
    for path, data in preserved.items():
        assert path.read_bytes() == data
    if kind == "pi":
        assert agent["session_path"] != first_session  # documented fresh restart
        # A subsequent resumed execution emits no message; old final must not leak.
        monkeypatch.setenv("NO_RESPONSE", "1")
        launched = cli(home, "resume", agent["id"], "--task", str(task))
        assert launched.returncode == 0, launched.stderr
        agent = registry.load_registry()["agents"][0]
        assert await_result(Path(agent["result_path"]))["result"] is None
    else:
        assert agent["session_path"] == first_session
        # Launch accepted, terminal envelope absent: state/result consistently fail.
        monkeypatch.setenv("AGY_RAW", "{malformed")
        launched = cli(home, "resume", agent["id"], "--task", str(task))
        assert launched.returncode == 0, launched.stderr
        agent = registry.load_registry()["agents"][0]
        failure = await_result(Path(agent["result_path"]))
        assert failure["exit_code"] == 3 and failure["final_state_hint"] == "failed"
        assert failure["session_continued"] is False
        assert Path(first_session).read_text() == "fixture-conversation\n"
        assert '"failed"' in cli(home, "status", agent["id"]).stdout
