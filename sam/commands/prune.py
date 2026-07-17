#!/usr/bin/env python3
"""SAM prune — Remove terminal agents (completed/failed/killed) from registry.

v0.1.1: Deletes agent directories and registry entries for terminal agents.
"""

import json
import shutil
import sys
from pathlib import Path

from sam import config as sam_config
from sam import locks as sam_locks
from sam import registry as sam_registry


def run(args):
    """Prune terminal agents from registry and delete their directories.

    Removes all agents with state in (completed, failed, killed).
    Active agents (spawning, running) are preserved.
    """
    as_json = getattr(args, "json", False)

    try:
        config = sam_config.load_config()
        reg = sam_registry.load_registry()
        agents = reg.get("agents", [])

        terminal_states = {"completed", "failed", "killed"}
        active = []
        pruned = []

        for agent in agents:
            if agent.get("state") in terminal_states:
                pruned.append(agent)
            else:
                active.append(agent)

        if not pruned:
            if as_json:
                print(json.dumps({"status": "ok", "pruned": 0}))
            else:
                print("No terminal agents to prune")
            return 0

        # Delete agent directories (best-effort)
        deleted_dirs = 0
        for agent in pruned:
            log_path = agent.get("log_path", "")
            if log_path:
                # Agent dir is: agents/<id>/run-NNN/
                agent_run_dir = Path(log_path).parent
                agent_dir = agent_run_dir.parent
                if agent_dir.exists():
                    try:
                        shutil.rmtree(agent_dir)
                        deleted_dirs += 1
                    except Exception as e:
                        print(f"Warning: could not delete {agent_dir}: {e}",
                              file=sys.stderr)

        # Acquire lock and update registry
        with sam_locks.registry_lock(exclusive=True, timeout=10):
            reg = sam_registry.load_registry()
            # Re-filter (registry may have changed while we were deleting dirs)
            agents = reg.get("agents", [])
            active = [a for a in agents if a.get("state") not in terminal_states]
            reg["agents"] = active
            sam_registry.save_registry(reg)

        result = {
            "status": "ok",
            "pruned": len(pruned),
            "directories_deleted": deleted_dirs,
        }
        if as_json:
            print(json.dumps(result))
        else:
            print(f"Pruned {len(pruned)} terminal agents")
            if deleted_dirs:
                print(f"Deleted {deleted_dirs} agent directories")

        return 0

    except sam_locks.LockTimeout as e:
        msg = f"lock timeout: {e}"
        if as_json:
            print(json.dumps({"status": "error", "code": 1, "message": msg}),
                  file=sys.stderr)
        else:
            print(f"sam: {msg}", file=sys.stderr)
        return 1
    except Exception as e:
        msg = str(e)
        if as_json:
            print(json.dumps({"status": "error", "code": 1, "message": msg}),
                  file=sys.stderr)
        else:
            print(f"sam: {msg}", file=sys.stderr)
        return 1
