"""Cross-platform tests: run on Windows AND Linux. No /proc, no shebang executables,
no real harness CLIs (tests/xplat/fake_harness.py stands in for all of them)."""
import json
import os
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_harness.py")
HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "helper_proc.py")
HARNESSES = ("pi", "agy", "opencode", "claude", "codex")


def fake_env(flavor="opencode", mode="ok", **extra):
    """Environment for a child `sam` / runner process that uses the fake harness."""
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO + os.pathsep + env.get("PYTHONPATH", "")
    for h in HARNESSES:
        env["SAM_%s_BIN" % h.upper()] = FAKE
    env["SAM_RUNNER"] = "generic"          # use sam/runner.py for pi/agy on POSIX too
    env["FAKE_FLAVOR"] = flavor
    env["FAKE_MODE"] = mode
    env["SAM_SLOT_WAIT_S"] = "0"
    env.update({k: str(v) for k, v in extra.items()})
    return env


def sam(args, env, timeout=120):
    """Run the sam CLI as a subprocess. Returns CompletedProcess (text)."""
    return subprocess.run([sys.executable, "-m", "sam.cli"] + list(args), cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace")


def sam_json(args, env, timeout=120):
    cp = sam(list(args) + ["--json"], env, timeout)
    try:
        return cp.returncode, json.loads(cp.stdout or cp.stderr)
    except ValueError:
        raise AssertionError("no JSON from sam %s: rc=%s out=%r err=%r"
                             % (args, cp.returncode, cp.stdout[-400:], cp.stderr[-400:]))


def wait_state(name, env, wanted, timeout=60):
    """Poll `sam status NAME --json` until resolved_state is in ``wanted``."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        rc, data = sam_json(["status", name], env)
        last = data.get("resolved_state")
        if last in wanted:
            return data
        time.sleep(0.5)
    raise AssertionError("agent %s never reached %s (last state %s)" % (name, wanted, last))


def pid_alive(pid):
    """Alive and not a zombie, on both OSes, without importing sam.proc."""
    pid = int(pid)
    if os.name == "nt":
        from sam.plat import windows as win
        return win.proc_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open("/proc/%d/stat" % pid) as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True
