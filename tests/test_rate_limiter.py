"""Item 1 acceptance tests: global spawn rate limiter.

Hermetic via conftest SAM_HOME isolation. Covers the agreed test-1 rows:
two concurrent spawns -> second defers/blocks; spacing visible in state;
--no-space pair -> zero spacing + bypass recorded.
"""

import argparse
import json
import os
import time

from sam import proc as sam_proc


def _seed_last_spawn(age_s):
    state = {"last_spawn": time.time() - age_s, "recent": []}
    path = sam_proc._spawn_state_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(state))


def test_spacing_denies_fresh_launch():
    _seed_last_spawn(1.0)
    slot = sam_proc.acquire_spawn_slot("a", "/tmp/t.md", "m",
                                       wait_s=0, running=0)
    assert slot["granted"] is False
    assert slot["retry_after_s"] > 10  # ~14s remaining of 15s spacing
    assert "spacing" in slot["reason"] or "ago" in slot["reason"]


def test_spacing_grants_after_window():
    _seed_last_spawn(60.0)
    slot = sam_proc.acquire_spawn_slot("b", "/tmp/t.md", "m",
                                       wait_s=0, running=0)
    assert slot["granted"] is True
    assert slot["bypassed"] is False


def test_cap_denies_at_max_running():
    _seed_last_spawn(600.0)
    slot = sam_proc.acquire_spawn_slot("c", "/tmp/t.md", "m",
                                       wait_s=0, running=4)
    assert slot["granted"] is False
    assert "cap" in slot["reason"]


def test_no_space_bypass_grants_and_records():
    _seed_last_spawn(0.5)  # would otherwise deny
    first = sam_proc.acquire_spawn_slot("canary-1", "/tmp/t.md", "m",
                                        no_space=True, running=0)
    second = sam_proc.acquire_spawn_slot("canary-2", "/tmp/t.md", "m",
                                         no_space=True, running=0)
    assert first["granted"] and first["bypassed"]
    assert second["granted"] and second["bypassed"]
    # Both timestamped: herd visible to doctor --window.
    state = json.loads(sam_proc._spawn_state_path().read_text())
    assert isinstance(state.get("last_spawn"), float)


def test_duplicate_request_suppressed():
    _seed_last_spawn(0.0)
    kwargs = dict(task="/tmp/t.md", model="m", wait_s=0, running=99)
    first = sam_proc.acquire_spawn_slot("spammy", **kwargs)
    assert first["granted"] is False
    assert first["duplicate_suppressed"] is False
    second = sam_proc.acquire_spawn_slot("spammy", **kwargs)
    assert second["granted"] is False
    assert second["duplicate_suppressed"] is True


def test_count_running_agents_liveness():
    me = os.getpid()
    start = sam_proc.read_pid_start_time(me)
    assert start is not None
    agents = [
        {"state": "running", "pid": me, "pid_start_time": start},
        {"state": "running", "pid": me, "pid_start_time": start + 999999},
        {"state": "running", "pid": 2 ** 22, "pid_start_time": None},
        {"state": "completed", "pid": me, "pid_start_time": start},
        {"state": "running", "pid": None, "pid_start_time": None},
    ]
    # Only the genuinely-live entry counts (startup-time match required).
    assert sam_proc.count_running_agents(agents) == 1


def _spawn_ns(tmp_path, **over):
    task = tmp_path / "task.md"
    task.write_text("# do canary\n")
    ns = argparse.Namespace(name="n1", task=str(task), model=None,
                            harness="agy", thinking=None, effort=None,
                            cwd=None, json=True, no_space=False)
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


def test_spawn_cli_defers_when_slot_busy(tmp_path, capsys, monkeypatch):
    from sam.commands import spawn as spawn_cmd
    monkeypatch.setenv("SAM_SLOT_WAIT_S", "0")
    _seed_last_spawn(0.0)
    rc = spawn_cmd.run(_spawn_ns(tmp_path))
    assert rc == 6
    out = capsys.readouterr().err
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["status"] == "deferred"
    assert payload["retry_after_s"] >= 1
    assert "NOT an error" in payload["message"]


def test_spawn_cli_no_space_passes_limiter(tmp_path):
    from sam.commands import spawn as spawn_cmd
    _seed_last_spawn(0.0)
    # Passes the limiter (no deferral) and fails later at the missing
    # wrapper — proving --no-space bypassed spacing, not the whole flow.
    rc = spawn_cmd.run(_spawn_ns(tmp_path, no_space=True))
    assert rc == 1
