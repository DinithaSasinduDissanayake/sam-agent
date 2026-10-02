"""S6: sam/runner.py with every adapter, against tests/xplat/fake_harness.py."""
import json
import os
import subprocess
import sys
import time

import pytest

from tests.xplat import HARNESSES, REPO, fake_env, pid_alive

RUNNER = os.path.join(REPO, "sam", "runner.py")
TASK = 'First line.\nSecond line with "quotes" %PATH% & | < > done.\nTOKEN: ZEBRA7'


def run_runner(tmp_path, harness, mode="ok", resume=False, pointer=None, task=TASK,
               extra_env=None, n=1, timeout=120):
    agent = tmp_path / "agents" / "sam-test"
    run = agent / ("run-%03d" % n)
    run.mkdir(parents=True)
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    session = agent / "session.jsonl"
    if pointer is not None:
        session.write_text(pointer + "\n", encoding="utf-8")
    (run / "task.md").write_text(task, encoding="utf-8")
    cmd = [sys.executable, RUNNER, "--harness", harness, "--agent-id", "sam-test",
           "--model", "fake-model", "--session", str(session),
           "--task", str(run / "task.md"), "--result", str(run / "result.json")]
    if resume:
        cmd.append("--resume")
    env = fake_env(harness, mode, PWD="/definitely/wrong", **(extra_env or {}))
    cp = subprocess.run(cmd, cwd=str(ws), env=env, capture_output=True, text=True, timeout=timeout)
    result = json.loads((run / "result.json").read_text(encoding="utf-8"))
    return cp.returncode, result, run, session, ws


@pytest.mark.parametrize("harness", HARNESSES)
def test_ok_run_delivers_full_prompt_and_result(tmp_path, harness):
    rc, r, run, session, ws = run_runner(tmp_path, harness)
    assert rc == 0, r
    assert r["final_state_hint"] == "completed" and r["exit_code"] == 0
    assert r["harness"] == harness and r["host"] and r["runner"] == "generic"
    assert r["result"].startswith("REPLY:TOKEN: ZEBRA7|LINES=3|"), r["result"]   # all 3 lines arrived
    assert ("|PWD=%s|" % ws) in r["result"]          # inherited wrong $PWD was replaced
    assert r["usage"] and r["infra_hint"] is None
    log = (run / "output.log").read_bytes()
    assert log.startswith(b"##") and b"_BEGIN_" in log and b"_END_" in log
    status = json.loads((run / "runner.json").read_text(encoding="utf-8"))
    assert status["phase"] == "finished"
    if harness == "pi":
        assert r["conversation_id"] is None
    else:
        assert session.read_text(encoding="utf-8").strip() == r["conversation_id"]


@pytest.mark.parametrize("harness", ["agy", "opencode", "claude", "codex"])
def test_resume_continues_the_same_session(tmp_path, harness):
    rc, r1, *_ = run_runner(tmp_path, harness)
    sid = r1["conversation_id"]
    rc, r2, *_ = run_runner(tmp_path, harness, resume=True, n=2)
    assert rc == 0 and r2["conversation_id"] == sid and r2["session_continued"] is True


@pytest.mark.parametrize("harness", ["agy", "opencode", "claude", "codex"])
def test_resume_without_pointer_is_refused_before_launch(tmp_path, harness):
    rc, r, run, *_ = run_runner(tmp_path, harness, resume=True)
    assert rc == 3 and r["error_kind"] == "resume_rejected"
    assert not (run / "output.log").exists()


def test_agy_silent_fresh_conversation_is_detected(tmp_path):
    # agy answers a bad --conversation id with a NEW conversation and exit 0
    rc, r, run, session, _ = run_runner(tmp_path, "agy", resume=True, pointer="bad-id")
    assert rc == 3 and r["final_state_hint"] == "failed"
    assert r["error_kind"] == "resume_rejected" and r["result"] is None
    assert session.read_text(encoding="utf-8").strip() == "bad-id"     # pointer untouched


def test_provider_overload_is_infra_quota(tmp_path):
    rc, r, *_ = run_runner(tmp_path, "opencode", mode="error")
    assert rc != 0 and r["final_state_hint"] == "failed"
    assert "overloaded" in r["error"] and r["infra_hint"] == "quota"
    assert r["conversation_id"]        # pointer persisted even though the run failed


def test_agy_quota_is_infra_quota(tmp_path):
    rc, r, *_ = run_runner(tmp_path, "agy", mode="error")
    assert r["final_state_hint"] == "failed" and r["infra_hint"] == "quota"


def test_pi_exit_zero_with_provider_error_is_a_failure(tmp_path):
    rc, r, *_ = run_runner(tmp_path, "pi", mode="exit0_error")
    assert rc != 0 and r["final_state_hint"] == "failed"
    assert "FreeTierError" in r["error"] and r["infra_hint"] is None


def test_claude_exit_in_the_middle_of_a_tool_call_is_not_completed(tmp_path):
    rc, r, *_ = run_runner(tmp_path, "claude", mode="exit0_error")
    assert r["final_state_hint"] == "partial" and "tool call" in r["error"]


def test_watchdog_kills_a_silent_harness(tmp_path):
    t0 = time.time()
    rc, r, *_ = run_runner(tmp_path, "claude", mode="silent_hang",
                           extra_env={"SAM_FIRST_OUTPUT_TIMEOUT_S": "3"})
    assert time.time() - t0 < 60
    assert r["final_state_hint"] == "failed" and r["error_kind"] == "watchdog_first_output"
    assert r["infra_hint"] == "startup-network"


def test_harness_that_lingers_after_its_final_event_still_completes(tmp_path):
    t0 = time.time()
    rc, r, *_ = run_runner(tmp_path, "pi", mode="linger", extra_env={"SAM_EXIT_GRACE_S": "3"})
    assert time.time() - t0 < 60
    assert rc == 0 and r["final_state_hint"] == "completed"
    assert r.get("lingered_after_final_event") is True


def test_agy_task_longer_than_the_command_line_limit_fails_cleanly(tmp_path):
    rc, r, run, *_ = run_runner(tmp_path, "agy", task="x" * 31000)
    assert r["final_state_hint"] == "failed" and r["error_kind"] == "task_too_large"


def test_stdin_harness_takes_a_task_longer_than_the_command_line_limit(tmp_path):
    big = "\n".join("filler %d" % i for i in range(6000)) + "\nTOKEN: GIRAFFE9"
    assert len(big) > 40000
    rc, r, *_ = run_runner(tmp_path, "opencode", task=big)
    assert rc == 0 and r["result"].startswith("REPLY:TOKEN: GIRAFFE9|LINES=6001|")


def test_missing_binary_writes_a_failed_result(tmp_path):
    rc, r, *_ = run_runner(tmp_path, "opencode", extra_env={"SAM_OPENCODE_BIN": "", "PATH": ""})
    assert r["final_state_hint"] == "failed" and r["error_kind"] == "binary_not_found"


def test_killing_the_runner_kills_the_whole_tree(tmp_path):
    """Dead runner => dead agent (no orphans burning quota)."""
    pidfile = tmp_path / "pids.txt"
    agent = tmp_path / "agents" / "sam-test"
    run = agent / "run-001"
    run.mkdir(parents=True)
    (run / "task.md").write_text("go", encoding="utf-8")
    env = fake_env("opencode", "spawn_child", FAKE_PIDFILE=str(pidfile))
    kw = {} if os.name == "nt" else {"start_new_session": True}
    runner = subprocess.Popen(
        [sys.executable, RUNNER, "--harness", "opencode", "--agent-id", "sam-test",
         "--model", "m", "--session", str(agent / "session.jsonl"),
         "--task", str(run / "task.md"), "--result", str(run / "result.json")],
        cwd=str(tmp_path), env=env, **kw)
    for _ in range(100):
        if pidfile.exists() and len(pidfile.read_text().split()) == 2:
            break
        time.sleep(0.1)
    pids = [int(x) for x in pidfile.read_text().split()]
    assert all(pid_alive(p) for p in pids)
    from sam import proc as sam_proc
    if os.name == "nt":
        runner.kill()                     # only the runner: the job must take the rest
    else:
        sam_proc.killpg(runner.pid, sam_proc.SIGKILL)
    runner.wait(10)
    time.sleep(2)
    assert not any(pid_alive(p) for p in pids)
    status = json.loads((run / "runner.json").read_text(encoding="utf-8"))
    assert status["phase"] == "running" and status["child_pid"] == pids[0]   # breadcrumb survives
