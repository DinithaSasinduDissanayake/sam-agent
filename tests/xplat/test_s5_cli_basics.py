"""S5: the CLI starts on this OS; registry/config writes are atomic and UTF-8."""
import json
import os
import subprocess
import sys

from sam import config as sam_config
from sam import locks as sam_locks
from sam import registry as sam_registry
from tests.xplat import HELPER, sam, sam_json


def _env():
    env = dict(os.environ)
    return env


def test_cli_init_and_empty_status():
    env = _env()
    rc, data = sam_json(["init"], env)
    assert rc == 0 and data["status"] == "ok"
    assert os.path.isfile(os.path.join(env["SAM_HOME"], "config.json"))
    cp = sam(["status", "--json"], env)
    assert cp.returncode == 0 and json.loads(cp.stdout) == []


def test_registry_roundtrip_keeps_unicode():
    sam_config.init_sam_home()
    with sam_locks.registry_lock(exclusive=True, timeout=5):
        reg = sam_registry.load_registry()
        reg["agents"].append({"id": "sam-x", "name": "x", "state": "completed",
                              "note": "× → ✓"})
        sam_registry.save_registry(reg)
    again = sam_registry.load_registry()
    assert again["agents"][0]["note"] == "× → ✓"


def test_concurrent_registry_writers_lose_nothing():
    sam_config.init_sam_home()
    env = _env()
    procs = [subprocess.Popen([sys.executable, HELPER, "registry_writer", "15"], env=env)
             for _ in range(4)]
    for p in procs:
        assert p.wait(120) == 0
    reg = sam_registry.load_registry()
    assert len(reg["agents"]) == 60
    assert len({a["id"] for a in reg["agents"]}) == 60


def test_status_while_writers_are_busy_never_fails():
    sam_config.init_sam_home()
    env = _env()
    writer = subprocess.Popen([sys.executable, HELPER, "registry_writer", "40"], env=env)
    try:
        for _ in range(15):
            cp = sam(["status", "--json", "--all"], env)
            assert cp.returncode == 0, cp.stderr
            json.loads(cp.stdout)
    finally:
        assert writer.wait(120) == 0
