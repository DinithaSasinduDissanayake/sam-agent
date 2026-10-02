"""S10: runner reliability. One small runner per agent, no daemon:
the agent must survive everything that happens to whoever spawned it."""
import json
import os
import subprocess
import sys
import time

import pytest

from tests.xplat import REPO, fake_env, pid_alive, sam, sam_json, wait_state

WIN = os.name == "nt"


def _task(tmp_path):
    p = tmp_path / "task.md"
    p.write_text("Line one.\nTOKEN: ZEBRA7", encoding="utf-8")
    return str(p)


def _kill_tree(pid):
    if WIN:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        os.killpg(os.getpgid(pid), 9)


def _spawner(tmp_path, env, name, prelude=""):
    """A parent process that runs `sam spawn` and then keeps living (like a shell
    or an orchestrator session). Returns its Popen."""
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    code = (prelude +
            "import subprocess,sys,time\n"
            "subprocess.run([sys.executable,'-m','sam.cli','spawn','--name',%r,'--task',%r,"
            "'--harness','opencode','--cwd',%r,'--no-space'],cwd=%r)\n"
            "time.sleep(300)\n" % (name, _task(tmp_path), str(ws), REPO))
    kw = {"creationflags": 0x00000010} if WIN else {"start_new_session": True}   # own console / session
    return subprocess.Popen([sys.executable, "-c", code], env=env, cwd=REPO, **kw)


def test_agent_survives_tree_kill_of_the_spawning_shell_and_console(tmp_path):
    env = fake_env("opencode", "slow", FAKE_SLEEP="8")
    sam(["init"], env)
    parent = _spawner(tmp_path, env, "s1")
    st = wait_state("s1", env, ("running",), timeout=60)
    _kill_tree(parent.pid)                      # shell + its console + everything under it
    parent.wait(20)
    assert pid_alive(st["pid"]), "runner died with the spawning shell"
    done = wait_state("s1", env, ("completed", "failed", "unknown"), timeout=60)
    assert done["resolved_state"] == "completed"


@pytest.mark.skipif(not WIN, reason="Windows Job Objects")
def test_agent_survives_a_caller_job_that_forbids_breakaway(tmp_path):
    """Caller lives in a kill-on-close Job without BREAKAWAY_OK (some sandboxes /
    CI runners): the runner is then started through WMI, outside that job."""
    env = fake_env("opencode", "slow", FAKE_SLEEP="8")
    sam(["init"], env)
    prelude = ("import sys\nsys.path.insert(0,%r)\nfrom sam.plat import winjob\n"
               "h=winjob.create(None,kill_on_close=True,breakaway_ok=False)\nwinjob.assign_self(h)\n" % REPO)
    parent = _spawner(tmp_path, env, "j1", prelude)
    st = wait_state("j1", env, ("running", "completed"), timeout=90)
    parent.kill()                               # last handle closes -> the caller job kills its members
    parent.wait(20)
    time.sleep(2)
    done = wait_state("j1", env, ("completed", "failed", "unknown"), timeout=60)
    assert done["resolved_state"] == "completed"


def test_twelve_agents_at_once(tmp_path):
    env = fake_env("opencode", "slow", FAKE_SLEEP="4", SAM_MAX_RUNNING="20")
    sam(["init"], env)
    ws = tmp_path / "ws"
    ws.mkdir()
    procs = [subprocess.Popen([sys.executable, "-m", "sam.cli", "spawn", "--name", "m%02d" % i,
                               "--task", _task(tmp_path), "--harness", "opencode",
                               "--cwd", str(ws), "--no-space", "--json"],
                              cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
             for i in range(12)]                # all spawn commands race for the registry lock
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, err.decode("utf-8", "replace")
    for i in range(12):
        assert wait_state("m%02d" % i, env, ("completed", "failed", "unknown"),
                          timeout=120)["resolved_state"] == "completed"
    cp = sam(["status", "--json", "--all"], env)
    rows = json.loads(cp.stdout)
    assert len(rows) == 12 and len({r["pid"] for r in rows}) == 12     # one runner per agent


def test_running_cap_defers_instead_of_failing(tmp_path):
    env = fake_env("opencode", "slow", FAKE_SLEEP="6", SAM_MAX_RUNNING="1")
    sam(["init"], env)
    ws = tmp_path / "ws"
    ws.mkdir()
    base = ["--task", _task(tmp_path), "--harness", "opencode", "--cwd", str(ws)]
    assert sam(["spawn", "--name", "c1", "--no-space"] + base, env).returncode == 0
    wait_state("c1", env, ("running",), timeout=30)
    cp = sam(["spawn", "--name", "c2"] + base, env)
    assert cp.returncode == 6 and "NOT an error" in cp.stderr      # deferral, not a failure
    wait_state("c1", env, ("completed",), timeout=60)


def test_dead_runner_is_reported_with_its_breadcrumb_and_leaves_no_orphans(tmp_path):
    pidfile = tmp_path / "pids.txt"
    env = fake_env("opencode", "spawn_child", FAKE_PIDFILE=str(pidfile))
    sam(["init"], env)
    ws = tmp_path / "ws"
    ws.mkdir()
    rc, d = sam_json(["spawn", "--name", "d1", "--task", _task(tmp_path), "--harness", "opencode",
                      "--cwd", str(ws), "--no-space"], env)
    for _ in range(150):
        if pidfile.exists() and len(pidfile.read_text().split()) == 2:
            break
        time.sleep(0.1)
    pids = [int(x) for x in pidfile.read_text().split()]
    if WIN:
        subprocess.run(["taskkill", "/PID", str(d["pid"]), "/F"], capture_output=True)   # runner only
    else:
        os.kill(d["pid"], 9)
    time.sleep(3)
    assert not pid_alive(pids[0]), "harness survived its runner"
    if WIN:
        assert not pid_alive(pids[1]), "grandchild survived its runner"
    rc, st = sam_json(["status", "d1"], env)
    assert st["resolved_state"] == "unknown"
    assert st["runner_status"]["phase"] == "running" and st["runner_status"]["child_pid"] == pids[0]
    human = sam(["status", "d1"], env)
    assert "Runner died" in human.stdout and "sam resume d1" in human.stdout
    if not WIN:
        for p in pids:
            try:
                os.kill(p, 9)
            except OSError:
                pass
