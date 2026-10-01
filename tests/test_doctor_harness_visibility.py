"""Doctor reports harness wrappers/binaries and per-launch harness."""

import argparse
import time

import pytest

from sam import config as sam_config
from sam import proc as sam_proc
from sam.commands import doctor as doctor_cmd


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "sam-home"
    monkeypatch.setenv("SAM_HOME", str(h))
    sam_config.init_sam_home()
    return h


def test_collect_reports_harnesses(home):
    report = doctor_cmd.collect(window_hours=1.0)
    assert set(report["harnesses"]) == {"pi", "agy", "opencode"}
    assert report["harnesses"]["opencode"]["wrapper_installed"] is False


def test_launch_rows_carry_harness_and_text_shows_it(home, capsys):
    sam_proc.record_launch("h1", 1, "spawn", model="opencode/fake-m", name="oc-row",
                           harness="opencode", ts=time.time() - 30)
    report = doctor_cmd.collect(window_hours=1.0)
    assert report["spawn_log"][-1]["harness"] == "opencode"
    assert doctor_cmd.run(argparse.Namespace(window=1.0, json=False)) == 0
    out = capsys.readouterr().out
    assert "<opencode>" in out
    assert "harnesses:" in out
