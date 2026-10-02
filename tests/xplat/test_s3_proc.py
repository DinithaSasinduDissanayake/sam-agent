"""S3: sam.proc primitives behave the same on Windows and POSIX."""
import os
import subprocess
import sys
import time

from sam import proc as sam_proc
from tests.xplat import HELPER, pid_alive


def _sleeper():
    return subprocess.Popen([sys.executable, HELPER, "sleep", "60"])


def test_identity_is_stable_and_detects_death():
    p = _sleeper()
    try:
        time.sleep(0.5)
        assert sam_proc.proc_alive(p.pid) is True
        t1 = sam_proc.read_pid_start_time(p.pid)
        t2 = sam_proc.read_pid_start_time(p.pid)
        assert isinstance(t1, int) and t1 == t2
        assert sam_proc.proc_start_time_match(p.pid, t1) is True
        assert sam_proc.proc_start_time_match(p.pid, t1 + 12345) is False
        assert sam_proc._pid_is_zombie(p.pid) is False
        live = sam_proc.proc_liveness(p.pid, stored_start_time=t1)
        assert live["ok"] is True, live
    finally:
        p.kill()
        p.wait()
    assert sam_proc.proc_alive(p.pid) is False
    assert sam_proc.read_pid_start_time(p.pid) is None
    assert sam_proc.proc_liveness(p.pid, stored_start_time=t1)["ok"] is False


def test_proc_alive_never_raises_for_bad_pids():
    for bad in (0, -1, 99999999, None):
        assert sam_proc.proc_alive(bad) in (True, False) if bad else sam_proc.proc_alive(bad) is False


def test_sigkill_alias_exists():
    assert sam_proc.SIGKILL is not None and sam_proc.SIGTERM is not None


def test_kill_process_group_kills_orphaned_grandchild(tmp_path):
    pidfile = tmp_path / "pids.txt"
    kw = {} if os.name == "nt" else {"start_new_session": True}
    root = subprocess.Popen([sys.executable, HELPER, "tree", str(pidfile)], **kw)
    time.sleep(4)
    pids = [int(x) for x in pidfile.read_text().split()]
    assert len(pids) == 3 and pid_alive(pids[2])
    assert sam_proc.pgid_of(root.pid) == root.pid
    sample = sam_proc.sample_process_group(root.pid, root.pid)
    assert sample and str(pids[2]) in sample and "cpu" in sample[str(pids[2])]
    assert sam_proc.kill_process_group(root.pid, sigterm_timeout=5) is True
    root.wait(10)
    time.sleep(0.5)
    assert not pid_alive(pids[0]) and not pid_alive(pids[2])


def test_count_running_agents_uses_identity():
    p = _sleeper()
    try:
        time.sleep(0.5)
        st = sam_proc.read_pid_start_time(p.pid)
        agents = [{"state": "running", "pid": p.pid, "pid_start_time": st},
                  {"state": "running", "pid": p.pid, "pid_start_time": st + 999},  # reused pid
                  {"state": "completed", "pid": p.pid, "pid_start_time": st}]
        assert sam_proc.count_running_agents(agents) == 1
    finally:
        p.kill()
        p.wait()


def test_spawn_slot_spacing_and_cap(monkeypatch):
    monkeypatch.setenv("SAM_MAX_RUNNING", "12")
    assert sam_proc.max_running() == 12
    monkeypatch.setenv("SAM_MAX_RUNNING", "junk")
    assert sam_proc.max_running() == sam_proc.MAX_RUNNING
    monkeypatch.delenv("SAM_MAX_RUNNING")
    first = sam_proc.acquire_spawn_slot("a", "t1", "m", wait_s=0, running=0)
    assert first["granted"] is True and first["fail_open"] is False
    second = sam_proc.acquire_spawn_slot("b", "t2", "m", wait_s=0, running=0)
    assert second["granted"] is False and "spacing" in second["reason"]
    capped = sam_proc.acquire_spawn_slot("c", "t3", "m", wait_s=0, running=99)
    assert capped["granted"] is False and "cap" in capped["reason"]
