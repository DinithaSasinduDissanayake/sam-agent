#!/usr/bin/env python3
"""SAM result — Print an agent's final result text.

Final-only: reads run result.json, prints the `result` field raw.
With --json prints {"status","agent_id","exit_code","result"}.
"""

import json
import os
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_SAM_PKG = _THIS_DIR.parent
if str(_SAM_PKG) not in sys.path:
    sys.path.insert(0, str(_SAM_PKG))

from sam import registry as sam_registry


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
            return _emit_error(1, "no result yet", as_json)
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
