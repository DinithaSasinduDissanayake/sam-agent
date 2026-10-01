#!/usr/bin/env python3
"""SAM result — Print an agent's final result text.

Final-only: reads run result.json, prints the `result` field raw.
With --json prints {"status","agent_id","exit_code","result"}.

Fallback for old completed runs whose result.json lacks a persisted
`result`: read-only harness-aware structured extraction (agy response
envelope from the log tail, pi attributable session records only). Never
dumps the full log and never attributes a previous run's answer: when no
final text can be safely attributed to the current run, reports
unavailable (distinct from still-running).
"""

import json
import os
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_SAM_PKG = _THIS_DIR.parent
if str(_SAM_PKG) not in sys.path:
    sys.path.insert(0, str(_SAM_PKG))

from sam import harness as sam_harness
from sam import registry as sam_registry
from sam import state as sam_state


def _agy_envelope_response(log_path, max_bytes=256 * 1024):
    """Last structured agy `response` text from the log tail, else None.

    Whole-output JSON first, then JSON-lines in reverse (last wins);
    Documented event=result payloads yield a successful response.
    Sentinel lines are skipped. Read-only.
    """
    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return None
    text = data.decode("utf-8", errors="replace")
    lines = [ln for ln in text.splitlines()
             if not ln.startswith("##AGY_BEGIN_")
             and not ln.startswith("##AGY_END_")]

    def from_obj(obj):
        if not isinstance(obj, dict):
            return None
        if obj.get("event") == "result":
            obj = obj.get("result")
            if not isinstance(obj, dict):
                return None
        if "event" in obj or "type" in obj or obj.get("status") != "SUCCESS":
            return None
        v = obj.get("response")
        return v if isinstance(v, str) and v else None

    try:
        found = from_obj(json.loads("\n".join(lines)))
        if found is not None:
            return found
    except (ValueError, TypeError):
        pass
    for line in reversed(lines):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
            found = from_obj(obj)
        except (ValueError, TypeError):
            continue
        if found is not None:
            return found
        if isinstance(obj, dict) and ("status" in obj or obj.get("event") == "result"
                                      or obj.get("type") == "result"):
            return None
    return None


def _emit_error(code, message, as_json):
    if as_json:
        print(json.dumps({"status": "error", "code": code, "message": message}), file=sys.stderr)
    else:
        print(f"sam: {message}", file=sys.stderr)
    return code


def run(args):
    as_json = getattr(args, "json", False)
    try:
        registry = sam_registry.load_registry()
        agents = registry.get("agents", [])

        ref = getattr(args, "id_or_name", None) or getattr(args, "name", None)
        if ref is None:
            return _emit_error(2, "agent identifier required", as_json)

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

        result_path = agent.get("result_path")
        if not result_path or not os.path.exists(result_path):
            resolved = sam_state.resolve_agent_state(
                agent, agent.get("run_id", 1))
            if resolved in ("running", "spawning"):
                return _emit_error(
                    1, "no result yet (agent still running)", as_json)
            if resolved in sam_state.TERMINAL_STATES:
                data = {"final_state_hint": resolved,
                        "exit_code": agent.get("exit_code")}
            else:
                return _emit_error(1, "result unavailable (run state unknown)", as_json)
        else:
            try:
                with open(result_path, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError) as e:
                return _emit_error(1, f"result unreadable: {e}", as_json)
        if not isinstance(data, dict):
            return _emit_error(1, "result unreadable: not an object", as_json)

        result = data.get("result")
        status = data.get("final_state_hint") or agent.get("state") or "unknown"
        exit_code = data.get("exit_code")

        if status in ("failed", "killed"):
            return _emit_error(1, "result unavailable (run did not complete successfully)", as_json)

        if result is None and status == "completed":
            # Old completed run without a persisted result: read-only
            # harness-aware fallback. Agy: structured envelope response
            # from the log tail. Pi: require an authoritative run window
            # and a final on the active branch; never guess from history.
            harness = sam_harness.resolve_harness(agent)
            if harness == "agy":
                log_path = agent.get("log_path")
                if log_path:
                    fallback = _agy_envelope_response(log_path)
                    if fallback is not None:
                        result = fallback
            elif harness == "pi" and "result" not in data:
                # A recorded null means the wrapper's boundary found no final.
                # Only legacy schemas lacking this field need time recovery.
                result = sam_harness.extract_pi_run_result(
                    agent.get("session_path"),
                    data.get("started_at") or agent.get("run_started_at"),
                    data.get("ended_at") or agent.get("ended_at") or agent.get("completed_at"))
            if result is None:
                return _emit_error(
                    1, "result unavailable (completed run has no "
                    "attributable final text; not showing prior output)",
                    as_json)
        elif result is None and status in sam_state.TERMINAL_STATES:
            return _emit_error(1, "result unavailable (run has no final text)", as_json)
        elif result is None:
            # Not terminal: distinguish still-running from missing output.
            resolved = sam_state.resolve_agent_state(
                agent, agent.get("run_id", 1))
            if resolved in ("running", "spawning"):
                return _emit_error(
                    1, "no result yet (agent still running)", as_json)
            return _emit_error(1, "no result yet", as_json)

        if as_json:
            print(json.dumps({
                "status": status,
                "agent_id": agent.get("id"),
                "exit_code": exit_code,
                "result": result,
            }))
            return 0

        if result is None:
            return _emit_error(1, "no result yet", as_json)
        if isinstance(result, str):
            sys.stdout.write(result)
            if result and not result.endswith("\n"):
                sys.stdout.write("\n")
        else:
            print(json.dumps(result))
        return 0

    except Exception as e:
        return _emit_error(1, str(e), as_json)
