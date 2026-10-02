"""S2: sam.plat primitives (no other SAM module involved)."""
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from sam import plat
from tests.xplat import HELPER, REPO

WIN = plat.IS_WINDOWS


def test_package_is_not_named_platform():
    # sam/ is put on sys.path by the command modules: a sam/platform package
    # would shadow the standard library.
    assert not os.path.exists(os.path.join(REPO, "sam", "platform"))
    import platform
    assert hasattr(platform, "system")


def test_hostname_and_reserved_names():
    assert plat.hostname()
    assert plat.is_reserved_name("NUL") is WIN
    assert plat.is_reserved_name("worker-1") is False


def test_replace_while_readers_have_the_file_open(tmp_path):
    target = tmp_path / "state.json"
    target.write_text(json.dumps({"i": -1}), encoding="utf-8")
    stop = threading.Event()
    errors = []

    def reader():
        while not stop.is_set():
            try:
                json.loads(plat.read_text(target))
            except Exception as e:  # noqa
                errors.append(repr(e))

    threads = [threading.Thread(target=reader) for _ in range(2)]
    for t in threads:
        t.start()
    try:
        for i in range(300):
            tmp = tmp_path / ("state.%d.tmp" % i)
            tmp.write_text(json.dumps({"i": i, "pad": "x" * 500}), encoding="utf-8")
            plat.replace(str(tmp), str(target))
    finally:
        stop.set()
        for t in threads:
            t.join()
    assert errors == []
    assert json.loads(plat.read_text(target))["i"] == 299


def test_fix_child_env_sets_pwd(tmp_path):
    env = plat.fix_child_env({"PWD": "/somewhere/else", "OLDPWD": "/x"}, tmp_path)
    assert env["PWD"] == str(tmp_path)
    assert "OLDPWD" not in env


def test_popen_detached_child_outlives_its_parent(tmp_path):
    pidfile = tmp_path / "pid.txt"
    code = ("import sys,os;sys.path.insert(0,%r);from sam import plat;"
            "p=plat.popen_detached([sys.executable,%r,'sleep','30'],%r,dict(os.environ));"
            "open(%r,'w').write(str(p.pid))" % (REPO, HELPER, str(tmp_path), str(pidfile)))
    subprocess.run([sys.executable, "-c", code], check=True, timeout=60)
    pid = int(pidfile.read_text())
    time.sleep(1.0)
    from tests.xplat import pid_alive
    assert pid_alive(pid)
    if WIN:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    else:
        os.kill(pid, 9)


@pytest.mark.skipif(not WIN, reason="Windows job objects")
def test_job_kills_orphans(tmp_path):
    from sam.plat import windows as win
    pidfile = tmp_path / "pids.txt"
    root = subprocess.Popen([sys.executable, HELPER, "tree", str(pidfile)])
    time.sleep(4)
    pids = [int(x) for x in pidfile.read_text().split()]
    assert len(pids) == 3
    from tests.xplat import pid_alive
    assert pid_alive(pids[0]) and not pid_alive(pids[1]) and pid_alive(pids[2])
    assert sorted(win.group_pids(root.pid)) == sorted([pids[0], pids[2]])
    win.killpg(root.pid)
    time.sleep(1)
    assert not pid_alive(pids[0]) and not pid_alive(pids[2])


@pytest.mark.skipif(not WIN, reason="Windows npm shim")
def test_resolve_executable_unwraps_npm_shim(tmp_path, monkeypatch):
    from sam.plat import windows as win
    (tmp_path / "node_modules" / "x" / "bin").mkdir(parents=True)
    exe = tmp_path / "node_modules" / "x" / "bin" / "fakecli.exe"
    exe.write_bytes(b"MZ")
    (tmp_path / "fakecli.cmd").write_text(
        '@ECHO off\r\n"%dp0%\\node_modules\\x\\bin\\fakecli.exe"   %*\r\n')
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    assert win.resolve_executable("fakecli") == [str(exe)]
    assert win.resolve_executable("no-such-cli-xyz") is None
