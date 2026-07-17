#!/usr/bin/env python3
"""Unit tests for Batch 2 command modules (init, spawn, status, kill, wait, logs, restart).

Uses temp SAM_HOME, fake_pi on PATH. No real pi binary needed.
"""

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

_TEST_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _TEST_DIR.parent
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

def install_fake_pi(bin_dir):
    """Install a fake pi script that mimics the real pi."""
    fake_pi_path = bin_dir / "pi"
    fake_pi_path.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys, time\n"
        "exit_code = int(os.environ.get('FAKE_PI_EXIT_CODE', '0'))\n"
        "sleep = float(os.environ.get('FAKE_PI_SLEEP', '0'))\n"
        "output = os.environ.get('FAKE_PI_OUTPUT', 'fake pi output\\n')\n"
        "time.sleep(sleep)\n"
        "sys.stdout.write(output)\n"
        "sys.stdout.flush()\n"
        "sys.exit(exit_code)\n"
    )
    fake_pi_path.chmod(0o755)
    return fake_pi_path


def install_fake_wrapper(bin_dir):
    """Install a minimal pi-wrapper that uses fake pi."""
    wrapper_path = bin_dir / "pi-wrapper"
    wrapper_path.write_text(
        "#!/usr/bin/env python3\n"
        "import argparse, json, os, subprocess, sys, tempfile, time, uuid\n"
        "def main():\n"
        "    parser = argparse.ArgumentParser()\n"
        "    for a in ['--agent-id','--model','--session','--task','--result']:\n"
        "        parser.add_argument(a, required=True)\n"
        "    args = parser.parse_args()\n"
        "    sentinel = uuid.uuid4().hex[:8]\n"
        "    log_path = os.path.join(os.path.dirname(args.result), 'output.log')\n"
        "    os.makedirs(os.path.dirname(log_path), exist_ok=True)\n"
        "    with open(log_path, 'wb', buffering=0) as log:\n"
        "        log.write(f'##PI_BEGIN_{sentinel}\\n'.encode())\n"
        "        pi = subprocess.Popen(\n"
        "            ['pi', '--print', '--model', args.model,\n"
        "             '--session', args.session, f'@{args.task}'],\n"
        "            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)\n"
        "        while chunk := pi.stdout.read(65536):\n"
        "            log.write(chunk)\n"
        "        pi.wait()\n"
        "        log.write(f'##PI_END_{sentinel}\\n'.encode())\n"
        "    result = {\n"
        "        'agent_id': args.agent_id,\n"
        "        'exit_code': pi.returncode,\n"
        "        'final_state_hint': 'completed' if pi.returncode == 0 else 'failed',\n"
        "        'duration_ms': 100,\n"
        "        'output_path': log_path,\n"
        "    }\n"
        "    tmp = tempfile.NamedTemporaryFile(\n"
        "        dir=os.path.dirname(args.result), prefix='.tmp-',\n"
        "        suffix='.json', delete=False, mode='w')\n"
        "    json.dump(result, tmp)\n"
        "    tmp.flush()\n"
        "    os.replace(tmp.name, args.result)\n"
        "if __name__ == '__main__':\n"
        "    main()\n"
    )
    wrapper_path.chmod(0o755)
    return wrapper_path


@pytest.fixture
def sam_home(tmp_path, monkeypatch):
    home = tmp_path / "sam-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("SAM_HOME", str(home))
    # Add a bin dir with fake pi and wrapper to PATH
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    install_fake_pi(bin_dir)
    install_fake_wrapper(bin_dir)
    return home


@pytest.fixture
def inited_home(sam_home):
    from sam.config import init_sam_home, save_config, DEFAULT_CONFIG
    init_sam_home(sam_home=sam_home, force=True)
    # Ensure wrapper is installed
    wrapper = sam_home / "bin" / "pi-wrapper"
    if not wrapper.exists():
        install_fake_wrapper(sam_home / "bin")
    return sam_home


# ── init_cmd tests ────────────────────────────────────────────────────────────

class TestInit:
    def test_init_creates_tree(self, sam_home):
        from sam.commands.init_cmd import run
        import argparse
        args = argparse.Namespace(json=False, force=False)
        code = run(args)
        assert code == 0
        for subdir in ["", "bin", "agents", "tasks", "locks"]:
            assert (sam_home / subdir).is_dir()

    def test_init_json_output(self, sam_home):
        from sam.commands.init_cmd import run
        import argparse
        args = argparse.Namespace(json=True, force=False)
        code = run(args)
        assert code == 0

    def test_init_idempotent(self, sam_home):
        from sam.commands.init_cmd import run
        import argparse
        args = argparse.Namespace(json=False, force=False)
        assert run(args) == 0
        assert run(args) == 0  # Second call should not error


# ── spawn tests ───────────────────────────────────────────────────────────────

class TestSpawn:
    def test_spawn_happy_path(self, inited_home):
        """Basic spawn with fake pi should succeed"""
        from sam.commands.spawn import run
        import argparse
        task = inited_home / "task.md"
        task.write_text("test task")
        args = argparse.Namespace(
            name="test-agent", task=str(task),
            model="test-model", cwd=None, json=False)
        code = run(args)
        assert code == 0

    def test_spawn_name_exists(self, inited_home):
        """Spawning twice with same name should fail with code 2"""
        from sam.commands.spawn import run
        import argparse
        task = inited_home / "task.md"
        task.write_text("test task")
        args = argparse.Namespace(
            name="dup", task=str(task),
            model="test-model", cwd=None, json=False)
        assert run(args) == 0  # First should work
        code = run(args)  # Second should fail
        assert code == 2

    def test_spawn_missing_task(self, inited_home):
        """Spawning with non-existent task should fail with code 1"""
        from sam.commands.spawn import run
        import argparse
        args = argparse.Namespace(
            name="no-task", task="/nonexistent/task.md",
            model="test-model", cwd=None, json=False)
        code = run(args)
        assert code == 1


# ── status tests ──────────────────────────────────────────────────────────────

class TestStatus:
    def test_status_empty(self, inited_home):
        """Status with no agents should show empty list"""
        from sam.commands.status import run
        import argparse
        args = argparse.Namespace(json=True, agent=None, name=None)
        code = run(args)
        assert code == 0

    def test_status_not_found(self, inited_home):
        """Status for non-existent agent should fail with code 1"""
        from sam.commands.status import run
        import argparse
        args = argparse.Namespace(json=True, agent="nonexistent", name=None)
        code = run(args)
        assert code == 1


# ── kill tests ────────────────────────────────────────────────────────────────

class TestKill:
    def test_kill_not_found(self, inited_home):
        """Killing non-existent agent should fail with code 3"""
        from sam.commands.kill import run
        import argparse
        args = argparse.Namespace(json=False, agent="nonexistent", name=None)
        code = run(args)
        assert code == 3


# ── wait tests ────────────────────────────────────────────────────────────────

class TestWait:
    def test_wait_not_found(self, inited_home):
        """Waiting for non-existent agent should fail with code 5"""
        from sam.commands.wait import run
        import argparse
        args = argparse.Namespace(json=False, agent="nonexistent",
                                   name=None, timeout=1)
        code = run(args)
        assert code == 5


# ── logs tests ────────────────────────────────────────────────────────────────

class TestLogs:
    def test_logs_not_found(self, inited_home):
        """Logs for non-existent agent should fail with code 3"""
        from sam.commands.logs import run
        import argparse
        args = argparse.Namespace(json=False, agent="nonexistent",
                                   name=None, follow=False, raw=False, n=0)
        code = run(args)
        assert code == 3


# ── restart tests ─────────────────────────────────────────────────────────────

class TestRestart:
    def test_restart_not_found(self, inited_home):
        """Restarting non-existent agent should fail with code 3"""
        from sam.commands.restart import run
        import argparse
        args = argparse.Namespace(json=False, agent="nonexistent", name=None)
        code = run(args)
        assert code == 3

    def test_restart_not_terminal(self, inited_home):
        """Restarting a running agent should fail with code 6"""
        from sam.commands.restart import run
        import argparse
        # First create a running agent
        from sam.commands.spawn import run as spawn_run
        task = inited_home / "task.md"
        task.write_text("test task")
        spawn_args = argparse.Namespace(
            name="restart-test", task=str(task),
            model="test-model", cwd=None, json=False)
        spawn_run(spawn_args)

        # Now try to restart it (should fail since it's running)
        args = argparse.Namespace(json=False, agent="restart-test", name=None)
        code = run(args)
        assert code == 6
