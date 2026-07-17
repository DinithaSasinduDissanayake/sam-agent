#!/usr/bin/env python3
"""SAM status — Read-only view of agent states.

Spec: reviews-phase-f-batch2.md — GLM-5.2 §4 + Grok-4.5 shared helpers
"""

import json
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_SAM_PKG = _THIS_DIR.parent
if str(_SAM_PKG) not in sys.path:
    sys.path.insert(0, str(_SAM_PKG))

from sam import config as sam_config
from sam import registry as sam_registry
from sam import state as sam_state


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
    except Exception as e:
        return _emit_error(1, f"cannot load registry: {e}", as_json)

    # v0.1.1: --all flag shows terminal agents too; default hides them
    show_all = getattr(args, "all", False)

    ref = getattr(args, "id_or_name", None) or getattr(args, "name", None)

    if ref:
        # Single agent mode
        agent = None
        # Try exact ID first
        for a in agents:
            if a.get("id") == ref:
                agent = a
                break
        # Try exact name
        if agent is None:
            matches = [a for a in agents if a.get("name") == ref]
            # Among matches, prefer non-terminal
            non_term = [a for a in matches if a.get("state") not in sam_state.TERMINAL_STATES]
            if len(non_term) > 1:
                return _emit_error(1, f"ambiguous name '{ref}'", as_json)
            if non_term:
                agent = non_term[0]
            elif matches:
                agent = matches[0]

        if agent is None:
            return _emit_error(1, f"agent not found: {ref}", as_json)

        # Resolve state
        try:
            resolved = sam_state.resolve_agent_state(
                agent, agent.get("run_id", 1))
            agent = dict(agent)
            agent["resolved_state"] = resolved
        except Exception:
            agent = dict(agent)
            agent["resolved_state"] = "unknown"

        if as_json:
            print(json.dumps(agent, default=str))
        else:
            s = agent.get("resolved_state", "?")
            pid = agent.get("pid", "?")
            print(f"{agent.get('id','?'):20s} {agent.get('name','?'):20s} "
                  f"{s:10s} pid={pid}")
        return 0

    # List mode
    # v0.1.1: filter to active (non-terminal) by default unless --all
    from sam import state as sam_state
    if not show_all:
        agents = [a for a in agents
                  if a.get("state") not in sam_state.TERMINAL_STATES]

    resolved_list = []
    for a in agents:
        try:
            resolved = sam_state.resolve_agent_state(a, a.get("run_id", 1))
            entry = dict(a)
            entry["resolved_state"] = resolved
        except Exception:
            entry = dict(a)
            entry["resolved_state"] = "failed"
        resolved_list.append(entry)

    resolved_list.sort(key=lambda x: x.get("created_at", ""), reverse=True)

    if as_json:
        print(json.dumps(resolved_list, default=str))
    else:
        print(f"{'ID':20s} {'NAME':20s} {'STATE':10s} {'PID':8s}")
        print("-" * 60)
        for a in resolved_list:
            s = a.get("resolved_state", "?")
            pid = str(a.get("pid", "?"))
            print(f"{a.get('id','?'):20s} {a.get('name','?'):20s} "
                  f"{s:10s} pid={pid}")
    return 0
