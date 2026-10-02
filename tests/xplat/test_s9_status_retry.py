"""S9: infra_hint is trusted by the retry classifier; remote status merge."""
import json
import os
import subprocess

from sam import retry as sam_retry
from sam.commands import status as status_cmd


def test_infra_hint_is_trusted_when_present():
    base = {"final_state_hint": "failed", "exit_code": 1, "duration_ms": 5000,
            "error": "something about dial tcp and RESOURCE_EXHAUSTED"}
    assert sam_retry.detect_infra_failure(dict(base, infra_hint="quota"))[0] == "quota"
    assert sam_retry.detect_infra_failure(dict(base, infra_hint="startup-network"))[0] == "startup-network"
    # the runner said "not infra": the text grep must NOT override it
    assert sam_retry.detect_infra_failure(dict(base, infra_hint=None)) == (None, "")
    # legacy results (no key) are classified exactly as before
    assert sam_retry.detect_infra_failure(base)[0] == "quota"


def test_fetch_remote_merges_and_survives_failures(monkeypatch, capsys):
    calls = []

    class CP:
        def __init__(self, out):
            self.stdout, self.returncode = out, 0

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if "good-host" in cmd:
            return CP(json.dumps([{"name": "far", "resolved_state": "running", "host": "xlr8"},
                                  {"name": "old", "resolved_state": "completed"}]))
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    rows = status_cmd._fetch_remote(["good-host", "dead-host"], show_all=True)
    assert [r["name"] for r in rows] == ["far", "old"]
    assert rows[0]["host"] == "xlr8" and rows[0]["remote"] == "good-host"
    assert rows[1]["host"] == "good-host"                 # filled when the remote sent none
    assert calls[0][-1] == "sam status --json --all" and calls[0][0] == "ssh"
    assert "dead-host unavailable" in capsys.readouterr().err
