#!/usr/bin/env python3
"""Unit tests for Batch 1 core modules (config, registry, locks, proc, state).

Uses temp SAM_HOME, real flock, real filesystem. No real pi binary needed.
"""

import json
import os
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path

# Add parent dirs for imports
_TEST_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _TEST_DIR.parent
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))

# We'll test via subprocess to avoid import conflicts with the test files
# Import the actual implementation modules
_TEST_IMPL = _PROJECT_DIR
if str(_TEST_IMPL) not in sys.path:
    sys.path.insert(0, str(_TEST_IMPL))

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def sam_home(tmp_path, monkeypatch):
    """Set up a temp SAM_HOME and monkeypatch the env var."""
    home = tmp_path / "sam-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("SAM_HOME", str(home))
    return home


@pytest.fixture
def inited_home(sam_home, monkeypatch):
    """Fully initialized SAM home (config, dirs, but no registry)."""
    # Import here so fixtures are loaded first
    from sam.config import init_sam_home
    init_sam_home(sam_home=sam_home, force=True)
    return sam_home


# ── config.py tests ───────────────────────────────────────────────────────────

class TestConfig:
    """Tests for sam/config.py"""

    def test_get_sam_home_default(self):
        """Default SAM_HOME should be ~/.sam"""
        from sam.config import get_sam_home
        # Temporarily clear SAM_HOME
        old = os.environ.pop("SAM_HOME", None)
        try:
            home = get_sam_home()
            assert str(home).endswith(".sam"), f"Expected ~/.sam, got {home}"
        finally:
            if old is not None:
                os.environ["SAM_HOME"] = old

    def test_get_sam_home_env(self, sam_home):
        """SAM_HOME env var should override default"""
        from sam.config import get_sam_home
        home = get_sam_home()
        assert home == sam_home

    def test_load_config_missing_returns_defaults(self, sam_home):
        """Missing config file should return defaults, not error"""
        from sam.config import load_config
        cfg = load_config(sam_home=sam_home)
        assert cfg["defaults"]["model"] == "opencode/deepseek-v4-flash-free"
        assert cfg["defaults"]["max_restarts"] == 1
        assert cfg["security"]["inherit_env"] is True

    def test_load_config_corrupt_raises(self, sam_home):
        """Corrupt config file should raise ConfigCorrupt"""
        from sam.config import load_config, ConfigCorrupt
        cfg_path = sam_home / "config.json"
        cfg_path.write_text("not json")
        with pytest.raises(ConfigCorrupt):
            load_config(sam_home=sam_home)

    def test_save_config_round_trip(self, sam_home):
        """save_config then load_config should return identical data"""
        from sam.config import save_config, load_config
        custom = {
            "defaults": {"model": "test-model", "max_restarts": 5, "max_depth": 2},
            "security": {"inherit_env": False},
        }
        save_config(custom, sam_home=sam_home)
        loaded = load_config(sam_home=sam_home)
        assert loaded["defaults"]["model"] == "test-model"
        assert loaded["defaults"]["max_restarts"] == 5
        assert loaded["security"]["inherit_env"] is False

    def test_save_config_file_mode(self, sam_home):
        """Config file should be created with 0o600"""
        from sam.config import save_config
        save_config({"defaults": {}, "security": {}}, sam_home=sam_home)
        cfg_path = sam_home / "config.json"
        st = cfg_path.stat()
        mode = stat.S_IMODE(st.st_mode)
        assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"

    def test_init_sam_home_creates_dirs(self, sam_home):
        """init_sam_home should create all required directories"""
        from sam.config import init_sam_home
        init_sam_home(sam_home=sam_home)
        for subdir in ["", "bin", "agents", "tasks", "locks"]:
            d = sam_home / subdir
            assert d.is_dir(), f"Directory missing: {d}"
            st = d.stat()
            mode = stat.S_IMODE(st.st_mode)
            assert mode == 0o700, f"Dir {d} mode {oct(mode)}, expected 0o700"

    def test_init_sam_home_idempotent(self, sam_home):
        """Re-running init should not error"""
        from sam.config import init_sam_home
        init_sam_home(sam_home=sam_home)
        init_sam_home(sam_home=sam_home)  # Should not raise


# ── registry.py tests ─────────────────────────────────────────────────────────

class TestRegistry:
    """Tests for sam/registry.py"""

    def test_load_missing_returns_empty(self, sam_home):
        """Missing registry should return empty, not error"""
        from sam.registry import load_registry
        data = load_registry()
        assert data == {"version": 1, "agents": []}

    def test_save_and_load_round_trip(self, inited_home):
        """Save an agent, load it back"""
        from sam.registry import load_registry, save_registry
        from sam.config import get_sam_home
        # Ensure config module sees our SAM_HOME
        import test_implement_config as cfg_mod
        # Override get_sam_home to return our temp home
        data = {"version": 1, "agents": [
            {"id": "test-1", "name": "tester", "state": "running",
             "pid": 1234, "pgid": 1234, "pid_start_time": 100,
             "log_path": "/tmp/log", "result_path": "/tmp/res",
             "session_path": "/tmp/ses", "task_path": "/tmp/task",
             "cwd": "/tmp", "created_at": "2026-01-01T00:00:00Z",
             "updated_at": "2026-01-01T00:00:00Z",
             "exit_code": None, "exit_signal": None,
             "duration_ms": None, "restart_count": 0,
             "killed_reason": None, "run_id": 1,
             }
        ]}  # fmt: skip
        save_registry(data)
        loaded = load_registry()
        assert loaded["version"] == 1
        assert len(loaded["agents"]) == 1
        assert loaded["agents"][0]["id"] == "test-1"

    def test_find_by_id(self):
        """find_by_id should return correct agent by ID"""
        from sam.registry import find_by_id
        agents = [
            {"id": "a1", "name": "one"},
            {"id": "a2", "name": "two"},
        ]
        assert find_by_id(agents, "a1")["name"] == "one"
        assert find_by_id(agents, "a3") is None

    def test_find_active_by_name(self):
        """find_active_by_name should return non-terminal agents only"""
        from sam.registry import find_active_by_name
        agents = [
            {"id": "a1", "name": "foo", "state": "running"},
            {"id": "a2", "name": "foo", "state": "completed"},
            {"id": "a3", "name": "bar", "state": "running"},
        ]
        term = frozenset({"completed", "failed", "killed"})
        matches = find_active_by_name(agents, "foo", term)
        assert len(matches) == 1
        assert matches[0]["id"] == "a1"

    def test_create_entry_defaults(self):
        """create_entry should fill default fields"""
        from sam.registry import create_entry
        entry = create_entry({"id": "new1", "name": "test", "model": "m"})
        assert entry["state"] == "spawning"
        assert entry["run_id"] == 1
        assert entry["restart_count"] == 0


# ── locks.py tests ────────────────────────────────────────────────────────────

class TestLocks:
    """Tests for sam/locks.py"""

    def test_name_lock_invalid_raises(self):
        """Invalid name should raise ValueError"""
        from sam.locks import name_lock
        for bad in ["../x", "", "a" * 65, "has space"]:
            with pytest.raises(ValueError):
                with name_lock(bad, timeout=0.2):
                    pass

    def test_name_lock_sequential(self, inited_home):
        """Sequential name lock acquisition should work"""
        from sam.locks import name_lock
        with name_lock("test-agent", timeout=2):
            pass  # Acquire and release
        with name_lock("test-agent", timeout=2):
            pass  # Should work again

    def test_name_lock_blocks_concurrent(self, inited_home):
        """Second acquire of same name should block"""
        from sam.locks import name_lock, LockTimeout
        acquired = threading.Event()

        def hold_lock():
            with name_lock("block-test", timeout=5):
                acquired.set()
                time.sleep(0.5)

        t = threading.Thread(target=hold_lock, daemon=True)
        t.start()
        acquired.wait(timeout=1)

        with pytest.raises(LockTimeout):
            with name_lock("block-test", timeout=0.2):
                pass
        t.join(timeout=2)

    def test_registry_lock_sequential(self, inited_home):
        """Sequential registry lock should work"""
        from sam.locks import registry_lock
        with registry_lock(exclusive=True, timeout=2):
            pass
        with registry_lock(exclusive=True, timeout=2):
            pass


# ── proc.py tests ─────────────────────────────────────────────────────────────

class TestProc:
    """Tests for sam/proc.py"""

    def test_proc_alive_self(self):
        """Our own PID should be alive"""
        from sam.proc import proc_alive
        assert proc_alive(os.getpid()) is True

    def test_proc_alive_dead(self):
        """A non-existent PID should return False"""
        from sam.proc import proc_alive
        assert proc_alive(0) is False or proc_alive(999999999) is False

    def test_proc_start_time_self(self):
        """read_pid_start_time should work for our own PID"""
        from sam.proc import read_pid_start_time
        t = read_pid_start_time(os.getpid())
        assert t is not None
        assert isinstance(t, int)
        assert t > 0

    def test_start_time_match_self(self):
        """Our own PID start time should match itself"""
        from sam.proc import (read_pid_start_time,
                                          proc_start_time_match)
        t = read_pid_start_time(os.getpid())
        assert proc_start_time_match(os.getpid(), t) is True

    def test_start_time_mismatch_dead(self):
        """Dead PID should return False for match"""
        from sam.proc import proc_start_time_match
        assert proc_start_time_match(999999999, 12345) is False

    def test_killpg_zombie_reaping(self):
        """kill_process_group should terminate a subprocess"""
        from sam.proc import kill_process_group
        import subprocess
        proc = subprocess.Popen(
            ["sleep", "30"],
            start_new_session=True,
        )
        pgid = proc.pid  # session leader
        result = kill_process_group(pgid, sigterm_timeout=2)
        assert result is True, "kill_process_group should succeed"
        assert proc.poll() is not None, "process should be dead"


# ── state.py tests ────────────────────────────────────────────────────────────

class TestState:
    """Tests for sam/state.py"""

    def test_terminal_states(self):
        """is_terminal should recognize terminal states"""
        from sam.state import is_terminal, TERMINAL_STATES
        for s in TERMINAL_STATES:
            assert is_terminal(s) is True
        assert is_terminal("running") is False
        assert is_terminal("spawning") is False

    def test_resolve_terminal_short_circuit(self):
        """Priority 1: terminal registry state returns immediately"""
        from sam.state import resolve_agent_state
        entry = {"state": "completed", "pid": None}
        result = resolve_agent_state(entry, 1)
        assert result == "completed"

    def test_resolve_running_alive(self):
        """Priority 3: alive process with matching start_time → running"""
        from sam.state import resolve_agent_state
        pid = os.getpid()
        from sam.proc import read_pid_start_time
        start = read_pid_start_time(pid)
        entry = {"state": "running", "pid": pid,
                 "pid_start_time": start}
        result = resolve_agent_state(entry, 1)
        assert result == "running"

    def test_resolve_dead_with_result(self):
        """Priority 5: dead PID with result.json → completed/failed"""
        from sam.state import resolve_agent_state
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                          delete=False) as f:
            json.dump({"final_state_hint": "completed", "run_id": 1}, f)
            rp = f.name
        entry = {"state": "running", "pid": 999999999,
                 "pid_start_time": None, "result_path": rp,
                 "run_id": 1}
        result = resolve_agent_state(entry, 1)
        os.unlink(rp)
        assert result == "completed"

    def test_resolve_spawning_no_pid(self):
        """Priority 2: spawning with no PID → failed"""
        from sam.state import resolve_agent_state
        entry = {"state": "spawning", "pid": None}
        result = resolve_agent_state(entry, 1)
        assert result == "failed"


class TestConfigValidation:
    """Config validation tests (fix #4)."""

    def test_config_missing_required_key_raises(self, sam_home):
        """Config missing required key should raise ConfigCorrupt."""
        import json
        from sam.config import load_config, ConfigCorrupt
        cfg_path = sam_home / "config.json"
        cfg_path.write_text(json.dumps({"security": {"inherit_env": True}}))
        with pytest.raises(ConfigCorrupt):
            load_config(sam_home=sam_home)

    def test_config_invalid_inherit_env_type(self, sam_home):
        """Config with wrong type for inherit_env should raise ConfigCorrupt."""
        import json
        from sam.config import load_config, ConfigCorrupt
        cfg_path = sam_home / "config.json"
        cfg_path.write_text(json.dumps({
            "defaults": {"model": "m", "max_restarts": 1, "max_depth": 4},
            "security": {"inherit_env": "yes"},
        }))
        with pytest.raises(ConfigCorrupt):
            load_config(sam_home=sam_home)
