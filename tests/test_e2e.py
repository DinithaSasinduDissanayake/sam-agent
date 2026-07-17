#!/usr/bin/env python3
"""Golden path E2E test: init → spawn → status → wait → logs.

Uses subprocess to test the full SAM pipeline with fake pi.
No real pi binary or API keys needed.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

_TEST_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _TEST_DIR.parent
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))

import pytest


def install_fake_pi(bin_dir):
    """Install a deterministic fake pi CLI."""
    fake_pi = bin_dir / "pi"
    fake_pi.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys, time\n"
        "exit_code = int(os.environ.get('FAKE_PI_EXIT_CODE', '0'))\n"
        "sleep = float(os.environ.get('FAKE_PI_SLEEP', '0'))\n"
        "output = os.environ.get('FAKE_PI_OUTPUT', 'Hello from fake pi\\n')\n"
        "time.sleep(sleep)\n"
        "sys.stdout.write(output)\n"
        "sys.stdout.flush()\n"
        "sys.exit(exit_code)\n"
    )
    fake_pi.chmod(0o755)


def install_fake_wrapper(bin_dir):
    """Install a minimal pi-wrapper for testing."""
    wrapper = bin_dir / "pi-wrapper"
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import argparse, json, os, subprocess, sys, tempfile, uuid\n"
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
    wrapper.chmod(0o755)


def run_sam_command(cmd, cwd=None, env=None):
    """Run a sam subcommand via python -m and return (returncode, stdout, stderr)."""
    base_env = os.environ.copy()
    if env:
        base_env.update(env)
    result = subprocess.run(
        [sys.executable, "-c",
         f"import sys; sys.path.insert(0, '{_PROJECT_DIR}'); "
         f"from test_implement_{cmd[0]} import run; "
         f"import argparse; "
         f"args = argparse.Namespace(**{json.dumps(cmd[1]) if isinstance(cmd[1], dict) else cmd[1]}); "
         f"sys.exit(run(args))"],
        capture_output=True, text=True, cwd=cwd, env=base_env, timeout=30,
    )
    return result.returncode, result.stdout, result.stderr


@pytest.fixture
def sam_env(tmp_path):
    """Set up a full SAM environment with fake pi and wrapper."""
    sam_home = tmp_path / "sam-home"
    sam_home.mkdir(mode=0o700)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_fake_pi(bin_dir)
    install_fake_wrapper(bin_dir)

    # Create a task file
    task_file = tmp_path / "task.md"
    task_file.write_text("Write a hello world program in Python.")

    env = os.environ.copy()
    env["SAM_HOME"] = str(sam_home)
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env["FAKE_PI_EXIT_CODE"] = "0"
    env["FAKE_PI_OUTPUT"] = "Hello from fake pi\n```python\nprint('hello')\n```\n"
    env["FAKE_PI_SLEEP"] = "0.1"

    return {
        "sam_home": sam_home,
        "bin_dir": bin_dir,
        "task_file": str(task_file),
        "env": env,
    }


class TestGoldenPath:
    """The complete SAM golden path: init → spawn → status → wait → logs."""

    def test_full_golden_path(self, sam_env):
        """Complete golden path: init, spawn, status, wait, logs."""
        env = sam_env["env"]
        sh = sam_env["sam_home"]
        task = sam_env["task_file"]

        # Step 1: init
        code, out, err = run_sam_command(
            ["init", {"json": True, "force": False}],
            env=env)
        assert code == 0, f"init failed: {err}"
        assert (sh / "config.json").exists()
        assert (sh / "bin" / "pi-wrapper").exists()

        # Step 2: spawn
        code, out, err = run_sam_command(
            ["spawn", {"name": "golden-test", "task": task,
                       "model": "test-model", "cwd": None, "json": True}],
            env=env)
        assert code == 0, f"spawn failed: {err}"
        spawn_data = json.loads(out)
        agent_id = spawn_data.get("agent_id")
        assert agent_id is not None, f"No agent_id in spawn output: {out}"

        # Step 3: status should show the agent
        code, out, err = run_sam_command(
            ["status", {"json": True, "agent": agent_id, "name": None}],
            env=env)
        assert code == 0, f"status failed: {err}"

        # Step 4: wait for completion
        code, out, err = run_sam_command(
            ["wait", {"json": True, "agent": agent_id, "name": None,
                       "timeout": 10}],
            env=env)
        assert code == 0, f"wait failed: {err}"
        wait_data = json.loads(out)
        assert wait_data.get("status") == "completed"

        # Step 5: logs should show the agent's output
        code, out, err = run_sam_command(
            ["logs", {"json": False, "agent": agent_id, "name": None,
                       "follow": False, "raw": False, "n": 0}],
            env=env)
        assert code == 0, f"logs failed: {err}"
        assert "Hello from fake pi" in out or "Hello" in out, \
            f"Expected output in logs, got: {out}"

    def test_spawn_with_failure(self, sam_env):
        """Agent that fails should show in status as failed."""
        env = dict(sam_env["env"])
        env["FAKE_PI_EXIT_CODE"] = "1"
        env["FAKE_PI_OUTPUT"] = "Error: something broke\n"

        code, out, err = run_sam_command(
            ["init", {"json": False, "force": False}],
            env=env)
        assert code == 0

        task = sam_env["task_file"]
        code, out, err = run_sam_command(
            ["spawn", {"name": "fail-test", "task": task,
                       "model": "test-model", "cwd": None, "json": True}],
            env=env)
        assert code == 0, f"spawn failed: {err}"

        code, out, err = run_sam_command(
            ["wait", {"json": True, "agent": "fail-test", "name": None,
                       "timeout": 10}],
            env=env)
        assert code == 1, f"wait should return 1 for failure, got {code}"
        wait_data = json.loads(out)
        assert wait_data.get("status") in ("failed", "killed")

    def test_list_agents(self, sam_env):
        """Status with no arguments should list all agents."""
        env = sam_env["env"]
        code, out, err = run_sam_command(
            ["init", {"json": False, "force": False}],
            env=env)
        assert code == 0

        # Spawn one agent
        task = sam_env["task_file"]
        code, out, err = run_sam_command(
            ["spawn", {"name": "list-test", "task": task,
                       "model": "test-model", "cwd": None, "json": True}],
            env=env)
        assert code == 0

        # List all agents
        code, out, err = run_sam_command(
            ["status", {"json": True, "agent": None, "name": None}],
            env=env)
        assert code == 0
        data = json.loads(out)
        names = [a.get("name") for a in (data if isinstance(data, list)
                                          else data.get("agents", []))]
        assert "list-test" in names, f"Expected list-test in {names}"
