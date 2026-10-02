"""helper_proc.py <role> ... - child-process roles for the xplat tests.

  sleep SECONDS
  tree PIDFILE         become a group root like the runner (Windows: own kill-on-close
                       job; POSIX: caller starts us with start_new_session), start a
                       child that starts a grandchild, the child then exits (orphan),
                       record all pids, sleep.
  mid PIDFILE          (internal) the middle process of `tree`
  hold_lock FLAGFILE SECONDS   take the registry lock (exclusive), create FLAGFILE, hold
  registry_writer N    N times: lock registry, load, append a row, save
"""
import os
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
ME = os.path.abspath(__file__)
role = sys.argv[1]


def note(path, pid):
    with open(path, "a") as f:
        f.write("%d\n" % pid)


if role == "sleep":
    time.sleep(float(sys.argv[2]))

elif role == "tree":
    from sam import plat
    if plat.IS_WINDOWS:
        from sam.plat import windows as win
        _job = win.enter_own_job()
    note(sys.argv[2], os.getpid())
    subprocess.Popen([sys.executable, ME, "mid", sys.argv[2]])
    time.sleep(600)

elif role == "mid":
    note(sys.argv[2], os.getpid())
    kid = subprocess.Popen([sys.executable, ME, "sleep", "600"])
    note(sys.argv[2], kid.pid)
    time.sleep(1.5)
    sys.exit(0)          # the grandchild is now an orphan

elif role == "hold_lock":
    from sam import locks
    with locks.registry_lock(exclusive=True, timeout=10):
        open(sys.argv[2], "w").write("locked")
        time.sleep(float(sys.argv[3]))

elif role == "registry_writer":
    from sam import locks, registry
    me = os.getpid()
    for i in range(int(sys.argv[2])):
        with locks.registry_lock(exclusive=True, timeout=30):
            reg = registry.load_registry()
            reg["agents"].append({"id": "w-%d-%d" % (me, i), "name": "w%d-%d" % (me, i),
                                  "state": "completed"})
            registry.save_registry(reg)
