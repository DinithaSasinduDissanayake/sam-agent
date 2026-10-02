"""Hermetic suite guard and lifecycle ownership for detached fixture workers."""

import os
from pathlib import Path
import signal
import subprocess
import time
import sys
import uuid

import pytest

IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS:
    # The legacy suite needs POSIX (shebang fake executables, /proc, signals).
    # On Windows only tests/xplat/ is collected.
    collect_ignore_glob = ["test_*.py"]


@pytest.fixture(autouse=True)
def isolated_process_lifecycle(tmp_path, monkeypatch):
    home = tmp_path / "isolated-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Legacy tests expect the agent to work in the task file's directory.
    monkeypatch.setenv("SAM_WORKSPACE_MODE", "task-dir")
    if IS_WINDOWS:
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv("SAM_HOME", str(home / ".sam"))
        for key in ("SAM_MODEL", "SAM_HARNESS", "SAM_AGENT_ID", "SAM_DEPTH",
                    "SAM_PARENT_ID", "SAM_ROOT_ID", "SAM_MODEL_HARNESS",
                    "SAM_MAX_RUNNING", "SAM_RUNNER"):
            monkeypatch.delenv(key, raising=False)
        yield
        return
    monkeypatch.setenv("SAM_HOME", str(home / ".sam"))
    for key in ("SAM_MODEL", "SAM_HARNESS", "SAM_AGENT_ID", "SAM_DEPTH",
                "SAM_PARENT_ID", "SAM_ROOT_ID", "SAM_TUI_SHOW_ARCHIVED",
                "SAM_MODEL_HARNESS", "SAM_MAX_RUNNING", "SAM_RUNNER"):
        monkeypatch.delenv(key, raising=False)
    # Accidental unmocked workers fail locally instead of consuming live quota.
    guard = tmp_path / "guard-bin"
    guard.mkdir()
    for name in ("pi", "agy"):
        exe = guard / name
        exe.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(99)\n")
        exe.chmod(0o700)
    monkeypatch.setenv("PATH", str(guard) + os.pathsep + os.environ["PATH"])
    marker = uuid.uuid4().hex
    monkeypatch.setenv("SAM_TEST_PROCESS_OWNER", marker)
    real_popen = subprocess.Popen
    children = []

    def tracked_popen(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", tracked_popen)

    def owned_pids():
        owned = []
        for path in Path("/proc").iterdir():
            if not path.name.isdigit() or int(path.name) == os.getpid():
                continue
            try:
                env = (path / "environ").read_bytes().split(b"\0")
                if f"SAM_TEST_PROCESS_OWNER={marker}".encode() in env:
                    owned.append(int(path.name))
            except OSError:
                pass
        return owned

    try:
        yield
    finally:
        # Covers workers detached by CLI subprocesses too, including gate-blocked
        # workers after an assertion failure. Never signals user registry PIDs.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for pid in owned_pids():
                try:
                    if os.getpgid(pid) == pid:
                        os.killpg(pid, sig)
                    else:
                        os.kill(pid, sig)
                except ProcessLookupError:
                    pass
            for child in children:
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
            if not owned_pids():
                break
            time.sleep(0.01)
        assert not owned_pids(), "fixture worker leaked after teardown"
