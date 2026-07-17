#!/usr/bin/env python3
"""SAM logs — Tail or follow streaming log output of an agent.

Spec: reviews-phase-f-batch2.md — GLM-5.2 §6 + Grok-4.5 follow logic
"""

import json
import os
import re
import sys
import time
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_SAM_PKG = _THIS_DIR.parent
if str(_SAM_PKG) not in sys.path:
    sys.path.insert(0, str(_SAM_PKG))

from sam import registry as sam_registry
from sam import state as sam_state

_SENTINEL_RE = re.compile(r"^##PI_(BEGIN|END)_[a-f0-9]+$")


def _emit_error(code, message, as_json):
    if as_json:
        print(json.dumps({"status": "error", "code": code, "message": message}), file=sys.stderr)
    else:
        print(f"sam: {message}", file=sys.stderr)
    return code


def run(args):
    as_json = getattr(args, "json", False)

    # Line 1: --json and --follow are mutually exclusive
    if as_json and getattr(args, "follow", False):
        return _emit_error(2, "--json and --follow are mutually exclusive", as_json)

    try:
        # Line 2: Load registry, resolve agent
        registry = sam_registry.load_registry()
        agents = registry.get("agents", [])

        ref = getattr(args, "id_or_name", None) or getattr(args, "name", None)
        if ref is None:
            return _emit_error(3, "agent identifier required", as_json)

        agent = None
        for a in agents:
            if a.get("id") == ref:
                agent = a
                break
        if agent is None:
            matches = [a for a in agents if a.get("name") == ref]
            if matches:
                agent = matches[0]

        if agent is None:
            return _emit_error(3, f"agent not found: {ref}", as_json)

        log_path = agent.get("log_path")
        if not log_path:
            return _emit_error(1, "agent has no log path", as_json)

        # Line 4: Check if agent is terminal
        is_terminal = sam_state.resolve_agent_state(
            agent, agent.get("run_id", 1)) in sam_state.TERMINAL_STATES

        # Line 5-7: Log file doesn't exist yet
        if not os.path.exists(log_path):
            if is_terminal:
                return _emit_error(1, "log file missing for terminal agent", as_json)
            if not getattr(args, "follow", False):
                # Return empty
                if as_json:
                    print(json.dumps({"status": "ok", "lines": []}))
                else:
                    pass  # No output
                return 0
            # Follow mode: wait for file to appear
            while not os.path.exists(log_path):
                registry = sam_registry.load_registry()
                for a in registry["agents"]:
                    if a["id"] == agent["id"]:
                        agent = a
                        break
                if sam_state.resolve_agent_state(
                        agent, agent.get("run_id", 1)) in sam_state.TERMINAL_STATES:
                    if not os.path.exists(log_path):
                        return _emit_error(1, "agent terminated without producing log", as_json)
                time.sleep(0.2)

        # Line 9: Open log file
        follow = getattr(args, "follow", False)
        raw = getattr(args, "raw", False)
        n = getattr(args, "n", 0)

        if not follow:
            # Non-follow: read and print
            with open(log_path, "r", errors="replace") as f:
                lines = f.readlines()

            if n > 0:
                lines = lines[-n:]

            filtered = []
            for line in lines:
                if not raw and _SENTINEL_RE.match(line.rstrip("\n\r")):
                    continue
                filtered.append(line)

            if as_json:
                print(json.dumps({"status": "ok", "lines": filtered}))
            else:
                for line in filtered:
                    print(line, end="")
            return 0

        # Follow mode
        with open(log_path, "r", errors="replace") as f:
            # Seek near end for -n
            if n > 0:
                lines = f.readlines()
                start_lines = lines[-n:] if n < len(lines) else lines
                f.seek(0)
                if n > 0:
                    # Simple approach: read all, seek to position
                    all_text = f.read()
                    lines_list = all_text.splitlines(True)
                    tail = lines_list[-n:] if n < len(lines_list) else lines_list
                    f.seek(0)
                    # Write tail first
                    for line in tail:
                        if not raw and _SENTINEL_RE.match(line.rstrip("\n\r")):
                            continue
                        print(line, end="")
                        sys.stdout.flush()
                    f.seek(sum(len(l) for l in lines_list[:len(lines_list) - len(tail)]))
            else:
                # Seek to end
                f.seek(0, 2)

            while True:
                line = f.readline()
                if line:
                    if not raw and _SENTINEL_RE.match(line.rstrip("\n\r")):
                        continue
                    print(line, end="")
                    sys.stdout.flush()
                else:
                    # Check if agent is terminal and we've reached EOF
                    registry = sam_registry.load_registry()
                    for a in registry["agents"]:
                        if a["id"] == agent["id"]:
                            agent = a
                            break
                    term = sam_state.resolve_agent_state(
                        agent, agent.get("run_id", 1)) in sam_state.TERMINAL_STATES
                    if term:
                        # Read any remaining bytes
                        remaining = f.read()
                        if remaining:
                            for line in remaining.splitlines(True):
                                if not raw and _SENTINEL_RE.match(line.rstrip("\n\r")):
                                    continue
                                print(line, end="")
                                sys.stdout.flush()
                        break
                    time.sleep(0.2)
            return 0

    except sam_locks.LockTimeout:
        return _emit_error(1, "lock timeout", as_json)
    except Exception as e:
        return _emit_error(1, str(e), as_json)
