#!/usr/bin/env python3
"""SAM util module: Shared helpers used by spawn, restart, and other commands.

Extracted from spawn.py/restart.py to eliminate duplication and circular imports.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def generate_agent_id():
    """Generate a unique, sortable agent ID: sam-YYYYMMDD-HHMMSS-xxxxxx"""
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%d-%H%M%S")
    suffix = uuid.uuid4().hex[:6]
    return f"sam-{ts}-{suffix}"


def validate_name(name):
    """Validate agent name against ^[a-zA-Z0-9_-]{1,64}$. Raises ValueError."""
    if not _NAME_RE.match(name):
        raise ValueError(f"name must match {_NAME_RE.pattern}, got {name!r}")


def build_child_env(agent_id, model, depth):
    """Build child process environment for sub-agent.

    Sets SAM_AGENT_ID, SAM_MODEL, SAM_DEPTH, SAM_PARENT_ID, SAM_ROOT_ID.
    Inherits full parent env (v0.1 accepted risk).
    """
    env = os.environ.copy()
    env["SAM_AGENT_ID"] = agent_id
    env["SAM_MODEL"] = model
    env["SAM_DEPTH"] = str(depth + 1)
    spawner_id = os.environ.get("SAM_AGENT_ID")
    env["SAM_PARENT_ID"] = spawner_id or ""
    root = os.environ.get("SAM_ROOT_ID") or agent_id
    env["SAM_ROOT_ID"] = root
    return env


def launch_wrapper(wrapper_path, agent_id, model, session_path, task_path,
                   result_path, cwd, env):
    """Launch pi-wrapper as a subprocess in a new session.

    Returns subprocess.Popen object.
    Validates wrapper basename against allowlist.
    """
    wrapper = Path(wrapper_path).resolve()
    if wrapper.name != "pi-wrapper":
        raise ValueError(f"allowlist validation failed: {wrapper.name}")

    argv = [
        str(wrapper),
        "--agent-id", agent_id,
        "--model", model,
        "--session", str(session_path),
        "--task", str(task_path),
        "--result", str(result_path),
    ]

    return subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def now_iso():
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def deadline_iso(offset_seconds=30):
    """Return future ISO 8601 string (now + offset_seconds)."""
    dt = datetime.now(timezone.utc).isoformat()
    return dt + "Z" if not dt.endswith("Z") else dt


def copy_task_file(src, dst):
    """Copy task file src to dst with 0600 permissions.

    Creates parent directory if needed.
    """
    dst = Path(dst)
    dst.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copy2(str(src), str(dst))
    os.chmod(str(dst), 0o600)


def emit_error(code, message, as_json):
    """Print error to stderr in JSON or text format. Returns the exit code."""
    if as_json:
        print(json.dumps({"status": "error", "code": code, "message": message}),
              file=sys.stderr)
    else:
        print(f"sam: {message}", file=sys.stderr)
    return code
