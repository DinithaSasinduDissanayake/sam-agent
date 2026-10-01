"""Item 6 acceptance tests: registry read-through backfill from result.json.

Status writes terminal states but historically never copied exit_code /
duration_ms back into the registry, so registry-only readers (the other
session's audits, dashboards) saw `exit_code: null` (e.g. design-n1
`None` vs result.json `0`).
"""

import argparse
import json
import shutil
from pathlib import Path

import pytest

from sam import config as sam_config
from sam import registry as sam_registry

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "sam-home"
    monkeypatch.setenv("SAM_HOME", str(home))
    for key in ("SAM_HARNESS", "SAM_MODEL", "SAM_AGENT_ID", "SAM_DEPTH",
                "SAM_ROOT_ID"):
        monkeypatch.delenv(key, raising=False)
    sam_config.init_sam_home()
    for name in ("pi", "agy"):
        wrapper = home / "bin" / f"{name}-wrapper"
        shutil.copyfile(ROOT / "wrapper" / f"{name}_wrapper.py", wrapper)
        wrapper.chmod(0o700)
    return home


def _seed(home, aid, name, state, result=None, exit_code=None,
          duration_ms=None):
    run = home / "agents" / aid / "run-001"
    run.mkdir(parents=True)
    entry = {
        "id": aid, "name": name, "harness": "agy", "state": state,
        "model": "m", "run_id": 1, "run_count": 1,
        "created_at": "2026-10-01T00:00:00Z",
        "run_started_at": "2026-10-01T00:00:00Z",
        "exit_code": exit_code, "duration_ms": duration_ms,
        "log_path": str(run / "output.log"),
        "result_path": str(run / "result.json"),
        "pid": None, "pgid": None, "pid_start_time": None,
    }
    (run / "output.log").write_text("log\n")
    if result is not None:
        result.setdefault("agent_id", aid)
        result.setdefault("run_id", 1)
        (run / "result.json").write_text(json.dumps(result))
    sam_registry.save_registry(
        {"version": 1, "agents": [entry]})
    return entry


def _status_ns(**over):
    base = dict(id_or_name=None, name=None, all=False, archived=False,
                limit=None, fields=None, detail=False, watch=None,
                stall_seconds=300, json=True)
    base.update(over)
    return argparse.Namespace(**base)


def test_list_status_backfills_exit_code_and_duration(home, capsys):
    from sam.commands import status as status_cmd
    _seed(home, "a1", "done-one", "completed",
          result={"final_state_hint": "completed", "exit_code": 0,
                  "duration_ms": 4321, "ended_at": 1790841934.0,
                  "result": "ok"})
    assert status_cmd.run(_status_ns()) == 0
    capsys.readouterr()
    entry = sam_registry.load_registry()["agents"][0]
    assert entry["exit_code"] == 0
    assert entry["duration_ms"] == 4321


def test_single_agent_status_backfills_and_reports(home, capsys):
    from sam.commands import status as status_cmd
    _seed(home, "a2", "failed-one", "failed",
          result={"final_state_hint": "failed", "exit_code": 3,
                  "duration_ms": 900, "error": "boom"})
    assert status_cmd.run(_status_ns(id_or_name="failed-one")) == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["exit_code"] == 3           # patched into the output too
    entry = sam_registry.load_registry()["agents"][0]
    assert entry["exit_code"] == 3
    assert entry["duration_ms"] == 900


def test_backfill_never_overwrites_existing_exit_code(home):
    from sam.commands import status as status_cmd
    _seed(home, "a3", "kept", "completed", exit_code=7,
          result={"final_state_hint": "completed", "exit_code": 0})
    status_cmd.run(_status_ns())
    entry = sam_registry.load_registry()["agents"][0]
    assert entry["exit_code"] == 7  # registry value wins once present


def test_backfill_awaiting_retry_exit_code(home):
    from sam.commands import status as status_cmd
    _seed(home, "a4", "queued", "awaiting_retry",
          result={"final_state_hint": "failed", "exit_code": 3,
                  "duration_ms": 1200,
                  "error": "Individual quota reached Resets in 18m6s"})
    status_cmd.run(_status_ns())
    entry = sam_registry.load_registry()["agents"][0]
    assert entry["state"] == "awaiting_retry"  # not re-derived
    assert entry["exit_code"] == 3


def test_backfill_missing_result_leaves_none(home):
    from sam.commands import status as status_cmd
    _seed(home, "a5", "sig-killed", "killed", result=None)
    assert status_cmd.run(_status_ns()) == 0
    entry = sam_registry.load_registry()["agents"][0]
    assert entry["exit_code"] is None
    assert entry["state"] == "killed"
