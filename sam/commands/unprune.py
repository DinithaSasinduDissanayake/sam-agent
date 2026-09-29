#!/usr/bin/env python3
"""SAM unprune — Restore an archived agent (sets archived=false)."""

import json
import sys
from datetime import datetime, timezone

from sam import locks as sam_locks
from sam import registry as sam_registry


def run(args):
    as_json = getattr(args, "json", False)
    ref = getattr(args, "id", None) or getattr(args, "id_or_name", None)
    if not ref:
        msg = "agent identifier required"
        if as_json:
            print(json.dumps({"status": "error", "code": 2, "message": msg}), file=sys.stderr)
        else:
            print(f"sam: {msg}", file=sys.stderr)
        return 2
    try:
        with sam_locks.registry_lock(exclusive=True, timeout=10):
            reg = sam_registry.load_registry()
            agents = reg.get("agents", [])
            agent = sam_registry.find_by_id(agents, ref)
            if agent is None:
                matches = [a for a in agents if a.get("name") == ref]
                agent = matches[0] if len(matches) == 1 else None
                if agent is None and len(matches) > 1:
                    raise RuntimeError(f"ambiguous name '{ref}'")
            if agent is None:
                raise RuntimeError(f"agent not found: {ref}")
            if not agent.get("archived"):
                if as_json:
                    print(json.dumps({"status": "ok", "restored": 0, "agent_id": agent["id"],
                                      "message": "not archived"}))
                else:
                    print(f"Agent {agent['id']} is not archived")
                return 0
            agent["archived"] = False
            agent["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            sam_registry.save_registry(reg)
            if as_json:
                print(json.dumps({"status": "ok", "restored": 1, "agent_id": agent["id"]}))
            else:
                print(f"Restored agent {agent['id']}")
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
        code = 3 if "not found" in msg else 2
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
