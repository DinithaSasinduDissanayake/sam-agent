"""F20: sam result shows result_partial for partial runs."""

import argparse
import json

import pytest

from sam import config as sam_config
from sam import registry as sam_registry
from sam.commands import result as result_cmd


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "sam-home"
    monkeypatch.setenv("SAM_HOME", str(h))
    sam_config.init_sam_home()
    run = h / "agents" / "p1" / "run-001"
    run.mkdir(parents=True)
    rp = run / "result.json"
    rp.write_text(json.dumps({"agent_id": "p1", "final_state_hint": "partial", "exit_code": 1,
                              "result": None, "result_partial": "DELIVERABLE X",
                              "partial_path": "/x/PARTIAL.md"}))
    sam_registry.save_registry({"version": 1, "agents": [{
        "id": "p1", "name": "part", "state": "partial", "harness": "opencode",
        "run_id": 1, "result_path": str(rp), "log_path": str(run / "output.log")}]})
    return h


def test_result_prints_partial_text(home, capsys):
    rc = result_cmd.run(argparse.Namespace(id_or_name="p1", name=None, json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("PARTIAL")
    assert "DELIVERABLE X" in out and "/x/PARTIAL.md" in out


def test_result_json_partial(home, capsys):
    rc = result_cmd.run(argparse.Namespace(id_or_name="p1", name=None, json=True))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "partial"
    assert data["result_partial"] == "DELIVERABLE X"
    assert data["partial_path"] == "/x/PARTIAL.md"