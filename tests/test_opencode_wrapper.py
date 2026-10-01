"""opencode wrapper: pure parsing/classification + real subprocess runs with a fake opencode."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "wrapper" / "opencode_wrapper.py"
FAKE = ROOT / "tests" / "fixtures" / "fake_opencode.py"
FIX = ROOT / "tests" / "fixtures"


def load_wrapper():
    spec = importlib.util.spec_from_file_location("opencode_wrapper", str(WRAPPER))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _lines(*objs):
    return ("\n".join(json.dumps(o) for o in objs) + "\n").encode()


def _analysis(errors=(), stderr=(), codes=(), text=""):
    return {"errors": list(errors), "stderr_lines": list(stderr),
            "status_codes": list(codes), "all_text": text}


# ── pure helpers ──────────────────────────────────────────────────────────────

def test_analyze_final_text_is_last_segment():
    a = load_wrapper().analyze_output(_lines(
        {"type": "step_start", "sessionID": "ses_a", "part": {"id": "p1"}},
        {"type": "text", "sessionID": "ses_a", "part": {"id": "p2", "text": "thinking aloud"}},
        {"type": "tool_use", "sessionID": "ses_a", "part": {"id": "p3", "tool": "bash"}},
        {"type": "step_start", "sessionID": "ses_a", "part": {"id": "p4"}},
        {"type": "text", "sessionID": "ses_a", "part": {"id": "p5", "text": "FINAL"}},
        {"type": "step_finish", "sessionID": "ses_a", "part": {"id": "p6"}}))
    assert a["final_text"] == "FINAL"
    assert a["all_text"] == "thinking aloud\nFINAL"
    assert a["event_types"]["step_start"] == 2


def test_analyze_dedupes_text_by_part_id():
    a = load_wrapper().analyze_output(_lines(
        {"type": "text", "part": {"id": "p1", "text": "Hel"}},
        {"type": "text", "part": {"id": "p1", "text": "Hello"}}))
    assert a["final_text"] == "Hello"
    assert a["all_text"] == "Hello"


def test_analyze_usage_sums_step_finish():
    mod = load_wrapper()
    a = mod.analyze_output(_lines(
        {"type": "step_finish", "part": {"tokens": {"input": 10, "output": 4, "reasoning": 1,
                                                    "cache": {"read": 2, "write": 3}},
                                         "cost": 0.5}},
        {"type": "step_finish", "part": {"tokens": {"input": 5, "output": 1}}}))
    assert a["usage"] == {"input_tokens": 15, "output_tokens": 5, "reasoning_tokens": 1,
                          "cache_read_tokens": 2, "cache_write_tokens": 3, "cost": 0.5}
    assert mod.analyze_output(_lines({"type": "text", "part": {"text": "x"}}))["usage"] is None


def test_analyze_non_json_lines_are_stderr_and_sentinels_skipped():
    raw = (b"##OPENCODE_BEGIN_deadbeef\nError: something broke\n"
           + _lines({"type": "text", "part": {"id": "p", "text": "ok"}})
           + b"##OPENCODE_END_deadbeef\n{not json\n")
    a = load_wrapper().analyze_output(raw)
    assert a["stderr_lines"] == ["Error: something broke", "{not json"]
    assert a["event_count"] == 1


def test_analyze_session_ids_in_order():
    a = load_wrapper().analyze_output(_lines(
        {"type": "step_start", "sessionID": "ses_main"},
        {"type": "text", "part": {"sessionID": "ses_child", "text": "x"}},
        {"type": "text", "sessionID": "ses_main", "part": {"text": "y"}}))
    assert a["session_ids"] == ["ses_main", "ses_child"]


def test_classify_quota_by_status_code():
    mod = load_wrapper()
    assert mod.classify_infra(_analysis(errors=['{"name": "APIError"}'], codes=[429]), 5) == "quota"
    assert mod.classify_infra(_analysis(errors=["429 Too Many Requests"]), 5) == "quota"


def test_classify_quota_by_free_tier_text():
    mod = load_wrapper()
    assert mod.classify_infra(
        _analysis(errors=["FreeTierError: free tier exhausted"], codes=[403]), 5) == "quota"


def test_classify_generic_403_is_not_infra():
    mod = load_wrapper()
    assert mod.classify_infra(_analysis(errors=["Forbidden: invalid API key"], codes=[403]), 5) is None


def test_classify_network_startup_vs_midflight():
    mod = load_wrapper()
    a = _analysis(stderr=["Error: Unable to connect. ECONNRESET"])
    assert mod.classify_infra(a, 10) == "startup-network"
    assert mod.classify_infra(a, 500) is None


def test_classify_ignores_model_text_and_stack_line_numbers():
    mod = load_wrapper()
    assert mod.classify_infra(_analysis(text="RESOURCE_EXHAUSTED 429 Too Many Requests"), 5) is None
    assert mod.classify_infra(_analysis(stderr=["    at handler (/app/server.js:429:17)"]), 5) is None


def test_real_fixture_basic():
    a = load_wrapper().analyze_output((FIX / "opencode_real_basic.jsonl").read_bytes())
    assert a["event_count"] > 0 and a["session_ids"]
    assert a["final_text"] is not None and "DISC_OK" in a["final_text"]
    assert a["errors"] == []


def test_real_fixture_tool():
    a = load_wrapper().analyze_output((FIX / "opencode_real_tool.jsonl").read_bytes())
    assert a["event_count"] > 0
    assert a["final_text"] is not None and "TOOL_DONE" in a["final_text"]


# ── real wrapper subprocess with the fake opencode ────────────────────────────

@pytest.fixture
def oc(tmp_path):
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    exe = fakebin / "opencode"
    exe.write_text(FAKE.read_text())
    exe.chmod(0o755)
    agent = tmp_path / "agent"
    run = agent / "run-001"
    run.mkdir(parents=True)
    task = run / "task.md"
    task.write_text("Reply with exactly: HELLO\n")
    empty = tmp_path / "emptybin"
    empty.mkdir()
    return SimpleNamespace(tmp=tmp_path, fakebin=fakebin, agent=agent, run=run, task=task,
                           session=agent / "session.jsonl", result=run / "result.json",
                           dump=tmp_path / "dump.json", empty=empty)


def run_wrapper(oc, *extra, mode="ok", env_extra=None, path=None):
    env = dict(os.environ)
    env["PATH"] = path if path is not None else str(oc.fakebin) + os.pathsep + env.get("PATH", "")
    env["FAKE_OC_MODE"] = mode
    env["FAKE_OC_DUMP"] = str(oc.dump)
    env.update(env_extra or {})
    argv = [sys.executable, str(WRAPPER), "--agent-id", "a1", "--model", "opencode/fake-m",
            "--session", str(oc.session), "--task", str(oc.task),
            "--result", str(oc.result), *extra]
    proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)
    data = json.loads(oc.result.read_text()) if oc.result.exists() else None
    dump = json.loads(oc.dump.read_text()) if oc.dump.exists() else None
    return proc, data, dump


def test_ok_run_writes_result_pointer_and_log(oc):
    mod = load_wrapper()
    proc, data, dump = run_wrapper(oc)
    assert proc.returncode == 0, proc.stderr
    assert data["final_state_hint"] == "completed"
    assert data["result"] == "fake answer"
    assert data["harness"] == "opencode"
    assert data["session_id"] == "ses_fake0001"
    assert data["conversation_id"] == "ses_fake0001"
    assert data["session_continued"] is False
    assert data["infra_hint"] is None
    assert data["usage"]["input_tokens"] == 100
    assert oc.session.read_text().strip() == "ses_fake0001"
    log = (oc.run / "output.log").read_text().splitlines()
    assert log[0].startswith("##OPENCODE_BEGIN_")
    assert log[-1].startswith("##OPENCODE_END_")
    argv = dump["argv"]
    assert argv[:6] == ["run", "--format", "json", "--auto", "-m", "opencode/fake-m"]
    task_text = oc.task.read_text()
    if mod.PROMPT_VIA_STDIN:
        assert dump["stdin"] == task_text
        assert task_text not in argv
    else:
        assert argv[-2:] == ["--", task_text]


def test_effort_maps_to_variant(oc):
    proc, data, dump = run_wrapper(oc, "--effort", "low")
    assert proc.returncode == 0, proc.stderr
    assert dump["argv"][dump["argv"].index("--variant") + 1] == "low"
    assert data["variant"] == "low"


def test_fresh_run_ignores_old_pointer(oc):
    oc.session.write_text("ses_old\n")
    proc, data, dump = run_wrapper(oc)
    assert proc.returncode == 0, proc.stderr
    assert "--session" not in dump["argv"]
    assert oc.session.read_text().strip() == "ses_fake0001"
    assert data["session_continued"] is False


def test_resume_passes_session_and_marks_continued(oc):
    oc.session.write_text("ses_prev\n")
    proc, data, dump = run_wrapper(oc, "--resume")
    assert proc.returncode == 0, proc.stderr
    assert dump["argv"][dump["argv"].index("--session") + 1] == "ses_prev"
    assert data["session_continued"] is True
    assert data["session_id"] == "ses_prev"
    assert oc.session.read_text().strip() == "ses_prev"


def test_resume_without_pointer_exits_3_without_launch(oc):
    proc, data, dump = run_wrapper(oc, "--resume")
    assert proc.returncode == 3
    assert dump is None
    assert data["exit_code"] == 3
    assert data["error_kind"] == "resume_no_pointer"
    assert data["final_state_hint"] == "failed"


def test_resume_rejected_on_different_session(oc):
    oc.session.write_text("ses_prev\n")
    proc, data, _ = run_wrapper(oc, "--resume", env_extra={"FAKE_OC_FORCE_SESSION": "ses_other"})
    assert proc.returncode == 3
    assert data["error_kind"] == "resume_rejected"
    assert data["final_state_hint"] == "failed"
    assert oc.session.read_text().strip() == "ses_prev"


def test_error429_is_failed_with_quota_hint(oc):
    proc, data, _ = run_wrapper(oc, mode="error429")
    assert proc.returncode == 1
    assert data["final_state_hint"] == "failed"
    assert data["infra_hint"] == "quota"
    assert "429" in data["error"]
    assert data["result"] is None


def test_error_event_with_exit_0_is_failed(oc):
    proc, data, _ = run_wrapper(oc, mode="error429", env_extra={"FAKE_OC_EXIT": "0"})
    assert proc.returncode == 1
    assert data["exit_code"] == 1
    assert data["final_state_hint"] == "failed"


@pytest.mark.parametrize("mode,expected", [("error403free", "quota"), ("error403", None)])
def test_403_classification(oc, mode, expected):
    proc, data, _ = run_wrapper(oc, mode=mode)
    assert proc.returncode == 1
    assert data["final_state_hint"] == "failed"
    assert data["infra_hint"] == expected


def test_network_stderr_is_startup_network(oc):
    proc, data, _ = run_wrapper(oc, mode="network")
    assert proc.returncode == 1
    assert data["final_state_hint"] == "failed"
    assert data["infra_hint"] == "startup-network"
    assert "ECONNRESET" in data["error"]


def test_text_then_error_is_partial(oc):
    proc, data, _ = run_wrapper(oc, mode="text_then_error")
    assert proc.returncode == 1
    assert data["final_state_hint"] == "partial"
    assert "PARTIAL DELIVERABLE" in data["result_partial"]
    assert (oc.run / "PARTIAL.md").exists()
    assert data["infra_hint"] is None


def test_no_events_is_failed(oc):
    proc, data, _ = run_wrapper(oc, mode="silent_exit0")
    assert proc.returncode == 1
    assert data["final_state_hint"] == "failed"
    assert data["error_kind"] == "no_events"


def test_strips_ssh_env_keeps_sam_env(oc):
    proc, _, dump = run_wrapper(oc, env_extra={
        "SSH_CLIENT": "1.2.3.4 5 22", "SSH_CONNECTION": "1.2.3.4 5 6.7.8.9 22",
        "SSH_TTY": "/dev/pts/9", "SSH_AUTH_SOCK": "/tmp/agent.sock",
        "SAM_AGENT_ID": "a-parent"})
    assert proc.returncode == 0, proc.stderr
    env = dump["env"]
    for key in ("SSH_CLIENT", "SSH_CONNECTION", "SSH_TTY"):
        assert key not in env
    assert env["SSH_AUTH_SOCK"] == "/tmp/agent.sock"
    assert env["SAM_AGENT_ID"] == "a-parent"


def test_missing_binary_writes_failed_result(oc):
    proc, data, dump = run_wrapper(oc, path=str(oc.empty))
    assert proc.returncode == 1
    assert dump is None
    assert data["error"] == "opencode binary not found"


@pytest.mark.skipif(load_wrapper().PROMPT_VIA_STDIN, reason="prompt goes via stdin (D4)")
def test_argv_mode_rejects_huge_task(oc):
    oc.task.write_text("x" * 130000)
    proc, data, dump = run_wrapper(oc)
    assert proc.returncode == 1
    assert dump is None
    assert data["error_kind"] == "task_too_large"


def test_tool_mode_final_answer_after_tool(oc):
    proc, data, _ = run_wrapper(oc, mode="tool")
    assert proc.returncode == 0, proc.stderr
    assert data["result"] == "fake answer"
    assert data["usage"]["input_tokens"] == 200