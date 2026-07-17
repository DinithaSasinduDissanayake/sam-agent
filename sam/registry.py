#!/usr/bin/env python3
"""SAM registry module: atomic read/write of registry.json, lookup functions.

Spec: reviews-line-by-line.md — GLM-5.2, File: sam/registry.py
Caller must hold registry lock. Registry.py does NOT acquire locks.
"""

import json
import os
import tempfile
from pathlib import Path

from sam import config as sam_config


class RegistryCorrupt(Exception):
    """Raised when registry.json is unparseable or has invalid schema."""
    pass


def load_registry():
    """
    Line 1: path = sam.config.registry_path()
    Line 2: If not path.exists(), return {"version": 1, "agents": []}.
    Line 3: Try to read path as UTF-8 text and parse with json.loads().
    Line 4: If json.JSONDecodeError or OSError occurs, raise RegistryCorrupt.
    Line 5: If parsed dict missing version key or agents list, raise RegistryCorrupt.
    Line 6: If data["version"] != 1, raise RegistryCorrupt.
    Line 7: Return the parsed dict.
    """
    path = sam_config.registry_path()

    if not path.exists():
        return {"version": 1, "agents": []}

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise RegistryCorrupt(f"Cannot read registry file: {e}") from e

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise RegistryCorrupt(f"Registry file contains invalid JSON: {e}") from e

    if not isinstance(data, dict) or "version" not in data or "agents" not in data:
        raise RegistryCorrupt("Registry file missing required keys (version, agents)")

    if data["version"] != 1:
        raise RegistryCorrupt(f"Registry version {data['version']} not supported (expected 1)")

    if not isinstance(data["agents"], list):
        raise RegistryCorrupt("Registry agents must be a list")

    return data


def save_registry(data):
    """
    Line 1: Validate data["version"] == 1 and isinstance(data["agents"], list).
    Line 2: Validate each agent entry using sam.schema.validate_registry_entry().
    Line 3: path = sam.config.registry_path(); dir_path = path.parent
    Line 4: fd, tmp_path = tempfile.mkstemp(dir=dir_path, prefix=".tmp-")
    Line 5: Close fd. Reopen tmp_path via os.open(..., O_NOFOLLOW, 0o600).
    Line 6: Write json.dumps(data, indent=2) to fd. Flush, fsync, close.
    Line 7: os.replace(tmp_path, path).
    Line 8: dir_fd = os.open(dir_path, O_RDONLY); fsync; close.
    Line 9: If any exception, attempt to os.unlink(tmp_path) and re-raise.
    """
    if not isinstance(data, dict):
        raise RegistryCorrupt("save_registry expects a dict")
    if data.get("version") != 1:
        raise RegistryCorrupt("data.version must be 1")
    if not isinstance(data.get("agents"), list):
        raise RegistryCorrupt("data.agents must be a list")

    # Line 2: Validate each agent entry
    for agent in data["agents"]:
        if not isinstance(agent, dict):
            raise RegistryCorrupt("Each agent entry must be a dict")
        # Minimal required field check
        for field in ("id", "name", "state"):
            if field not in agent:
                raise RegistryCorrupt(f"Agent entry missing required field '{field}'")

    path = sam_config.registry_path()
    dir_path = path.parent
    tmp_path = None
    fd = None

    try:
        fd, tmp_path = tempfile.mkstemp(dir=str(dir_path), prefix=".tmp-")
        os.close(fd)
        fd = None

        fd = os.open(tmp_path, os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.write(fd, json.dumps(data, indent=2).encode("utf-8"))
        os.fsync(fd)
        os.close(fd)
        fd = None

        os.replace(tmp_path, str(path))

        dir_fd = os.open(str(dir_path), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    except BaseException:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


def find_by_id(agents, agent_id):
    """
    Line 1: Iterate agents list.
    Line 2: Return the first agent dict where agent["id"] == id.
    Line 3: If loop completes without match, return None.
    """
    for agent in agents:
        if agent.get("id") == agent_id:
            return agent
    return None


def find_active_by_name(agents, name, terminal_states):
    """
    Line 1: Return a list of agent dicts where agent["name"] == name
            AND agent["state"] not in terminal_states.
    """
    return [a for a in agents if a.get("name") == name and a.get("state") not in terminal_states]


def find_terminal_by_name(agents, name, terminal_states):
    """
    Line 1: Return a list of agent dicts where agent["name"] == name
            AND state in terminal_states.
    """
    return [a for a in agents if a.get("name") == name and a.get("state") in terminal_states]


def list_agents(agents):
    """Line 1: Return the agents list unmodified."""
    return agents


def create_entry(fields):
    """
    Line 1: Accept fields dict.
    Line 2: Create a new dict merging default fields with fields.
    Line 3: Return the new entry dict.
    """
    defaults = {
        "run_id": 1,
        "state": "spawning",
        "restart_count": 0,
        "exit_code": None,
        "exit_signal": None,
        "duration_ms": None,
        "killed_reason": None,
    }
    entry = dict(defaults)
    entry.update(fields)
    if "created_at" not in entry:
        from datetime import datetime, timezone
        entry["created_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if "updated_at" not in entry:
        entry["updated_at"] = entry["created_at"]
    return entry


def transition_state(agent, new_state, **fields):
    """
    Line 1: Update agent dict in-place.
    Line 2: Set state=new_state, updated_at=utcnow().
    Line 3: Merge **fields into agent.
    Line 4: Return agent.
    """
    from datetime import datetime, timezone
    agent["state"] = new_state
    agent["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    agent.update(fields)
    return agent
