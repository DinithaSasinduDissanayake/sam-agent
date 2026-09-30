#!/usr/bin/env python3
"""SAM prune — Hide terminal agents (completed/failed/killed) + unknowns via archived flag.

Step 4: prune never deletes. Sets archived=true on registry entries;
never rmtree, never removes registry entries. Directories, logs and
results remain intact so `sam status --all`, `sam logs` and
`sam result` still work on archived agents.
Usage: sam prune [id|--all]  (no args = all terminal)
"""

import json
import sys
from datetime import datetime, timezone

from sam import locks as sam_locks
from sam import registry as sam_registry
from sam import state as sam_state


def _emit(msg, as_json, is_error=False):
    if as_json:
        print(json.dumps(msg), file=sys.stderr if is_error else sys.stdout)
    else:
        print(msg.get("message", msg) if isinstance(msg, dict) else msg,
              file=sys.stderr if is_error else sys.stdout)


def run(args):
    as_json = getattr(args, "json", False)
    ref = getattr(args, "id", None)
    prune_all = getattr(args, "all", False)

    if ref and prune_all:
        msg = "cannot specify both ID and --all"
        if as_json:
            print(json.dumps({"status": "error", "code": 2, "message": msg}), file=sys.stderr)
        else:
            print(f"sam: {msg}", file=sys.stderr)
        return 2

    try:
        with sam_locks.registry_lock(exclusive=True, timeout=10):
            reg = sam_registry.load_registry()
            agents = reg.get("agents", [])
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            if ref:
                agent = sam_registry.find_by_id(agents, ref)
                if agent is None:
                    matches = [a for a in agents if a.get("name") == ref]
                    agent = matches[0] if len(matches) == 1 else None
                    if agent is None and len(matches) > 1:
                        raise RuntimeError(f"ambiguous name '{ref}'")
                if agent is None:
                    raise RuntimeError(f"agent not found: {ref}")
                resolved = sam_state.resolve_agent_state(agent, agent.get("run_id", 1))
                is_unknown = (resolved == "unknown")
                if resolved not in sam_state.TERMINAL_STATES and not is_unknown:
                    raise RuntimeError(f"agent {agent['id']} is not terminal (state={resolved})")
                if agent.get("archived"):
                    if as_json:
                        print(json.dumps({"status": "ok", "pruned": 0, "agent_id": agent["id"],
                                          "message": "already archived"}))
                    else:
                        print(f"Agent {agent['id']} already archived")
                    return 0
                agent["archived"] = True
                if is_unknown:
                    agent["prune_reason"] = "stale"
                agent["updated_at"] = now
                sam_registry.save_registry(reg)
                if as_json:
                    print(json.dumps({"status": "ok", "pruned": 1, "agent_id": agent["id"]}))
                else:
                    print(f"Archived agent {agent['id']}" + (" (stale unknown)" if is_unknown else ""))
                return 0

            # --all or no args: archive all terminal + unknowns, non-archived agents
            targets = []
            for a in agents:
                if a.get("archived"):
                    continue
                try:
                    resolved = sam_state.resolve_agent_state(a, a.get("run_id", 1))
                except Exception:
                    resolved = a.get("state")
                if resolved in sam_state.TERMINAL_STATES or resolved == "unknown":
                    targets.append(a)
            if not targets:
                if as_json:
                    print(json.dumps({"status": "ok", "pruned": 0}))
                else:
                    print("No terminal agents to prune")
                return 0
            for a in targets:
                a["archived"] = True
                try:
                    r = sam_state.resolve_agent_state(a, a.get("run_id", 1))
                except Exception:
                    r = a.get("state")
                if r == "unknown":
                    a["prune_reason"] = "stale"
                a["updated_at"] = now
            sam_registry.save_registry(reg)
            if as_json:
                print(json.dumps({"status": "ok", "pruned": len(targets),
                                  "agent_ids": [a.get("id") for a in targets]}))
            else:
                print(f"Archived {len(targets)} terminal agents")
            return 0

    except sam_locks.LockTimeout as e:
        msg = f"lock timeout: {e}"
        if as_json:
            print(json.dumps({"status": "error", "code": 1, "message": msg}), file=sys.stderr)
        else:
            print(f"sam: {msg}", file=sys.stderr)
        return 1
    except RuntimeError as e:
        msg = str(e)
        code = 3 if "not found" in msg else 2 if "ambiguous" in msg or "both" in msg else 6 if "not terminal" in msg else 1
        if as_json:
            print(json.dumps({"status": "error", "code": code, "message": msg}), file=sys.stderr)
        else:
            print(f"sam: {msg}", file=sys.stderr)
        return code
    except Exception as e:
        msg = str(e)
        if as_json:
            print(json.dumps({"status": "error", "code": 1, "message": msg}), file=sys.stderr)
        else:
            print(f"sam: {msg}", file=sys.stderr)
        return 1
