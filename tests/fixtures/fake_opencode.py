#!/usr/bin/env python3
"""Deterministic stand-in for `opencode run --format json` (tests + live fake checks).

Environment:
  FAKE_OC_MODE   ok | tool | error429 | error403free | error403 | network |
                 text_then_error | crash | silent_exit0 | sleep   (default ok)
  FAKE_OC_DUMP   if set: write {"argv", "stdin", "env"} as JSON to this path
  FAKE_OC_SESSION        session id for a fresh run (default ses_fake0001)
  FAKE_OC_FORCE_SESSION  always use this id (simulates a rejected resume)
  FAKE_OC_TEXT   final answer text (default "fake answer")
  FAKE_OC_EXIT   exit code for error429 (default 1)
  FAKE_OC_SLEEP  seconds for mode sleep (default 300)
"""
import json
import os
import sys
import time

argv = sys.argv[1:]
stdin_text = ""
try:
    if sys.stdin is not None and not sys.stdin.isatty():
        stdin_text = sys.stdin.read()
except (OSError, ValueError):
    stdin_text = ""
dump = os.environ.get("FAKE_OC_DUMP")
if dump:
    with open(dump, "w") as f:
        json.dump({"argv": argv, "stdin": stdin_text, "env": dict(os.environ)}, f)

mode = os.environ.get("FAKE_OC_MODE", "ok")
if os.environ.get("FAKE_OC_FORCE_SESSION"):
    sid = os.environ["FAKE_OC_FORCE_SESSION"]
elif "--session" in argv:
    sid = argv[argv.index("--session") + 1]
else:
    sid = os.environ.get("FAKE_OC_SESSION", "ses_fake0001")
text = os.environ.get("FAKE_OC_TEXT", "fake answer")
counter = [0]


def ev(type_, **data):
    counter[0] += 1
    obj = {"type": type_, "timestamp": int(time.time() * 1000), "sessionID": sid}
    obj.update(data)
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def part(kind, **extra):
    p = {"id": "prt_%04d" % counter[0], "sessionID": sid, "messageID": "msg_0001",
         "type": kind}
    p.update(extra)
    return p


def text_ev(t):
    ev("text", part=part("text", text=t, time={"start": 1, "end": 2}))


def step_start():
    ev("step_start", part=part("step-start"))


def step_finish():
    ev("step_finish", part=part("step-finish", reason="stop", cost=0,
                                tokens={"input": 100, "output": 20, "reasoning": 5,
                                        "cache": {"read": 0, "write": 0}}))


def error_ev(name, message, status=None):
    data = {"message": message}
    if status is not None:
        data["statusCode"] = status
    ev("error", error={"name": name, "data": data})


if mode == "ok":
    step_start(); text_ev(text); step_finish()
    sys.exit(0)
if mode == "tool":
    step_start(); text_ev("I will run a tool first.")
    ev("tool_use", part=part("tool", tool="bash",
                             state={"status": "completed", "output": "TOOL_OUT"}))
    step_finish()
    step_start(); text_ev(text); step_finish()
    sys.exit(0)
if mode == "error429":
    step_start()
    error_ev("APIError", "Rate limit exceeded: 429 Too Many Requests. Resets in 0m20s", 429)
    sys.exit(int(os.environ.get("FAKE_OC_EXIT", "1")))
if mode == "error403free":
    error_ev("APIError", "FreeTierError: free tier access denied for this client", 403)
    sys.exit(1)
if mode == "error403":
    error_ev("APIError", "Forbidden: invalid API key", 403)
    sys.exit(1)
if mode == "network":
    sys.stderr.write("Error: Unable to connect. Is the computer able to access the url? ECONNRESET\n")
    sys.exit(1)
if mode == "text_then_error":
    step_start(); text_ev("PARTIAL DELIVERABLE")
    error_ev("UnknownError", "stream aborted")
    sys.exit(1)
if mode == "crash":
    sys.exit(7)
if mode == "silent_exit0":
    sys.exit(0)
if mode == "sleep":
    step_start()
    end = time.time() + float(os.environ.get("FAKE_OC_SLEEP", "300"))
    while time.time() < end:
        ev("heartbeat_test")
        time.sleep(1)
    sys.exit(0)
sys.stderr.write("fake opencode: unknown mode %s\n" % mode)
sys.exit(64)