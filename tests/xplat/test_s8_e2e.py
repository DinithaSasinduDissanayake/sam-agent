"""S8: the real CLI end to end (spawn -> wait -> result -> resume -> restart -> kill)
with the fake harness, on this OS."""
import json
import os
import tempfile
import time

import pytest

from tests.xplat import HARNESSES, fake_env, pid_alive, sam, sam_json, wait_state


def _task(tmp_path, text="Line one.\nTOKEN: ZEBRA7", name="task.md"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def _spawn(env, name, task, harness, *extra):
    rc, data = sam_json(["spawn", "--name", name, "--task", task, "--harness", harness,
                         "--no-space"] + list(extra), env)
    assert rc == 0, data
    return data


@pytest.mark.parametrize("harness", HARNESSES)
def test_spawn_wait_result(tmp_path, harness):
    env = fake_env(harness)
    assert sam(["init"], env).returncode == 0
    ws = tmp_path / "ws"
    ws.mkdir()
    d = _spawn(env, "a1", _task(tmp_path), harness, "--cwd", str(ws))
    assert d["status"] == "ok" and d["pid"] > 0
    rc, w = sam_json(["wait", "a1"], env)
    assert rc == 0 and w["status"] == "completed", w
    cp = sam(["result", "a1"], env)
    assert cp.returncode == 0 and cp.stdout.startswith("REPLY:TOKEN: ZEBRA7|LINES=2|")
    assert ("|CWD=%s" % ws) in cp.stdout.strip()
    rc, st = sam_json(["status", "a1"], env)
    assert st["resolved_state"] == "completed" and st["harness"] == harness and st["host"]
    logs = sam(["logs", "a1"], env)
    assert logs.returncode == 0 and "_BEGIN_" not in logs.stdout     # sentinels are stripped


def test_resume_and_restart_pointer_harness(tmp_path):
    env = fake_env("opencode")
    sam(["init"], env)
    ws = tmp_path / "ws"
    ws.mkdir()
    _spawn(env, "r1", _task(tmp_path), "opencode", "--cwd", str(ws))
    rc, w = sam_json(["wait", "r1"], env)
    sid = w["result"]["conversation_id"]
    rc, d = sam_json(["resume", "r1", "--task", _task(tmp_path, "again\nTOKEN: TWO", "t2.md"), "--no-space"], env)
    assert rc == 0 and d["session_continuation_requested"] is True, d
    rc, w2 = sam_json(["wait", "r1"], env)
    assert w2["status"] == "completed" and w2["result"]["conversation_id"] == sid
    assert w2["result"]["session_continued"] is True
    rc, d = sam_json(["restart", "r1", "--no-space"], env)
    assert rc == 0, d
    rc, w3 = sam_json(["wait", "r1"], env)
    assert w3["status"] == "completed" and w3["result"]["conversation_id"] != sid   # restart = fresh session


def test_resume_refused_without_session(tmp_path):
    env = fake_env("claude", "error")
    sam(["init"], env)
    ws = tmp_path / "ws"
    ws.mkdir()
    _spawn(env, "x1", _task(tmp_path), "claude", "--cwd", str(ws))
    sam_json(["wait", "x1"], env)
    agent_dir = os.path.join(env["SAM_HOME"], "agents")
    for root, _dirs, files in os.walk(agent_dir):
        if "session.jsonl" in files:
            os.remove(os.path.join(root, "session.jsonl"))
    rc, d = sam_json(["resume", "x1", "--task", _task(tmp_path), "--no-space"], env)
    assert rc == 1 and "use spawn not resume" in d["message"]


def test_kill_takes_down_the_whole_tree(tmp_path):
    pidfile = tmp_path / "pids.txt"
    env = fake_env("opencode", "spawn_child", FAKE_PIDFILE=str(pidfile))
    sam(["init"], env)
    ws = tmp_path / "ws"
    ws.mkdir()
    d = _spawn(env, "k1", _task(tmp_path), "opencode", "--cwd", str(ws))
    for _ in range(150):
        if pidfile.exists() and len(pidfile.read_text().split()) == 2:
            break
        time.sleep(0.1)
    pids = [int(x) for x in pidfile.read_text().split()]
    assert pid_alive(d["pid"]) and all(pid_alive(p) for p in pids)
    rc, st = sam_json(["status", "k1"], env)
    assert st["resolved_state"] == "running"
    rc, k = sam_json(["kill", "k1"], env)
    assert rc == 0 and k["outcome"] == "killed"
    time.sleep(1)
    assert not pid_alive(d["pid"]) and not any(pid_alive(p) for p in pids)
    rc, st = sam_json(["status", "k1"], env)
    assert st["resolved_state"] == "killed"


def test_failed_run_reports_failed_and_reasoning_flags_are_checked(tmp_path):
    env = fake_env("codex", "error")
    sam(["init"], env)
    ws = tmp_path / "ws"
    ws.mkdir()
    _spawn(env, "f1", _task(tmp_path), "codex", "--cwd", str(ws), "--effort", "low")
    rc, w = sam_json(["wait", "f1"], env)
    assert rc == 1 and w["status"] == "failed" and "401" in w["result"]["error"]
    cp = sam(["spawn", "--name", "f2", "--task", _task(tmp_path), "--harness", "codex",
              "--thinking", "high", "--no-space"], env)
    assert cp.returncode == 2 and "use --effort" in cp.stderr
    cp = sam(["spawn", "--name", "f3", "--task", _task(tmp_path), "--harness", "pi",
              "--effort", "low", "--no-space"], env)
    assert cp.returncode == 2 and "--effort requires" in cp.stderr


def test_default_workspace_is_persistent_when_the_task_is_in_a_temp_dir(tmp_path):
    env = fake_env("opencode")
    env.pop("SAM_WORKSPACE_MODE", None)       # default mode: auto
    sam(["init"], env)
    assert os.path.commonpath([str(tmp_path), tempfile.gettempdir()]) == os.path.normpath(tempfile.gettempdir()), \
        "pytest tmp_path is expected to live in the temp dir"
    _spawn(env, "w1", _task(tmp_path), "opencode")
    rc, w = sam_json(["wait", "w1"], env)
    assert w["status"] == "completed"
    expect = os.path.join(env["SAM_HOME"], "workspaces", "w1")
    rc, st = sam_json(["status", "w1"], env)
    assert os.path.normcase(st["cwd"]) == os.path.normcase(expect) and os.path.isdir(expect)
    env["SAM_WORKSPACE_MODE"] = "task-dir"
    _spawn(env, "w2", _task(tmp_path), "opencode")
    sam_json(["wait", "w2"], env)
    rc, st = sam_json(["status", "w2"], env)
    assert os.path.normcase(st["cwd"]) == os.path.normcase(str(tmp_path))


@pytest.mark.skipif(os.name != "nt", reason="Windows reserved device names")
def test_reserved_name_is_rejected(tmp_path):
    env = fake_env("opencode")
    sam(["init"], env)
    cp = sam(["spawn", "--name", "nul", "--task", _task(tmp_path), "--harness", "opencode", "--no-space"], env)
    assert cp.returncode != 0 and "reserved" in cp.stderr
