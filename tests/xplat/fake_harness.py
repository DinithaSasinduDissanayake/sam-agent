#!/usr/bin/env python3
"""fake_harness.py - stands in for opencode / agy / claude / codex / pi in tests.

Selected with $SAM_<HARNESS>_BIN=<this file>. Behaviour comes from the environment:
  FAKE_FLAVOR  opencode | agy | claude | codex | pi      (output dialect)
  FAKE_MODE    ok | error | silent_hang | linger | exit0_error | spawn_child | slow
  FAKE_SLEEP   seconds for mode slow (default 3)
  FAKE_PIDFILE where mode spawn_child appends the pids it created
The reply text proves what arrived: REPLY:<last prompt line>|LINES=<n>|PWD=<$PWD>|CWD=<cwd>
"""
import json
import os
import subprocess
import sys
import time

flavor = os.environ.get("FAKE_FLAVOR", "opencode")
mode = os.environ.get("FAKE_MODE", "ok")
argv = sys.argv[1:]


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def arg_after(flag):
    return argv[argv.index(flag) + 1] if flag in argv else None


if flavor == "agy":
    prompt = arg_after("-p") or ""
    requested = arg_after("--conversation")
elif flavor == "pi":
    files = [a for a in argv if a.startswith("@")]
    prompt = open(files[0][1:], encoding="utf-8").read() if files else ""
    requested = None
else:
    prompt = sys.stdin.read()
    if flavor == "opencode":
        requested = arg_after("--session")
    elif flavor == "claude":
        requested = arg_after("--resume")
    else:  # codex: exec resume <flags...> <id> -
        requested = argv[-2] if "resume" in argv else None

if requested and requested != "bad-id":
    sid = requested
else:
    sid = "fake-%s-%d" % (flavor, os.getpid())   # "bad-id": silently start a new session

lines = [ln for ln in prompt.splitlines() if ln.strip()]
text = "REPLY:%s|LINES=%d|PWD=%s|CWD=%s" % (lines[-1] if lines else "", len(prompt.splitlines()),
                                            os.environ.get("PWD", ""), os.getcwd())

if mode == "silent_hang":
    time.sleep(600)
    sys.exit(0)

if mode == "spawn_child":
    kid = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    with open(os.environ["FAKE_PIDFILE"], "a") as f:
        f.write("%d\n%d\n" % (os.getpid(), kid.pid))
if mode == "slow":
    time.sleep(float(os.environ.get("FAKE_SLEEP", "3")))

usage = {"input_tokens": 100, "output_tokens": 7}
if flavor == "opencode":
    out({"type": "step_start", "sessionID": sid, "part": {"type": "step-start"}})
    if mode == "spawn_child":
        time.sleep(600)
    if mode == "error":
        out({"type": "error", "sessionID": sid, "error": {"name": "APIError", "data": {
            "message": "The backend is temporarily overloaded. Please retry.", "statusCode": 503}}})
        sys.exit(1)
    out({"type": "text", "sessionID": sid, "part": {"id": "p1", "type": "text", "text": text}})
    out({"type": "step_finish", "sessionID": sid, "part": {"reason": "stop", "tokens": {
        "input": 100, "output": 7, "reasoning": 1, "cache": {"read": 5, "write": 0}}, "cost": 0}})
elif flavor == "agy":
    out({"event": "init", "conversation_id": sid, "init": {"model": "fake"}})
    if mode == "spawn_child":
        time.sleep(600)
    if mode == "error":
        out({"event": "result", "result": {"conversation_id": sid, "status": "ERROR",
                                           "error": "RESOURCE_EXHAUSTED: Individual quota reached"}})
        sys.exit(1)
    out({"event": "result", "result": {"conversation_id": sid, "status": "SUCCESS",
                                       "response": text + "\n", "usage": usage}})
elif flavor == "claude":
    out({"type": "system", "subtype": "init", "session_id": sid})
    if mode == "spawn_child":
        time.sleep(600)
    if mode == "error":
        out({"type": "result", "subtype": "error_during_execution", "is_error": True,
             "result": "API Error: Connection error.", "session_id": sid})
        sys.exit(1)
    out({"type": "result", "subtype": "success", "is_error": False, "result": text,
         "session_id": sid, "stop_reason": "tool_use" if mode == "exit0_error" else "end_turn",
         "usage": usage, "total_cost_usd": 0.001, "num_turns": 1})
elif flavor == "codex":
    out({"type": "thread.started", "thread_id": sid})
    out({"type": "turn.started"})
    if mode == "spawn_child":
        time.sleep(600)
    if mode == "error":
        out({"type": "error", "message": "Reconnecting... 5/5 (unauthorized (401))"})
        out({"type": "turn.failed", "error": {"message": "unauthorized (401)"}})
        sys.exit(1)
    out({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": text}})
    out({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 5, "output_tokens": 7}})
else:  # pi
    out({"type": "session", "version": 3, "id": "fake-pi"})
    with open(arg_after("--session"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "session", "version": 3, "id": "fake-pi"}) + "\n")
    if mode == "spawn_child":
        time.sleep(600)
    failed = mode in ("error", "exit0_error")
    msg = {"role": "assistant", "content": [] if failed else [{"type": "text", "text": text}],
           "usage": {"input": 100, "output": 7, "cacheRead": 5, "cacheWrite": 0, "cost": {"total": 0}},
           "stopReason": "error" if failed else "stop"}
    if failed:
        msg["errorMessage"] = "403: {\"type\":\"FreeTierError\"}"
    out({"type": "message_end", "message": msg})
    out({"type": "agent_settled"})
    if mode == "error":
        sys.exit(1)

if mode == "linger":
    time.sleep(600)
sys.exit(0)
