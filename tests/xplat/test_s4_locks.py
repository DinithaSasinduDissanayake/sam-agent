"""S4: registry/name locks exclude other processes on every OS."""
import subprocess
import sys
import time

import pytest

from sam import config as sam_config
from sam import locks as sam_locks
from tests.xplat import HELPER


def _init_home():
    sam_config.init_sam_home()


def test_locks_are_reentrant_in_sequence():
    _init_home()
    for _ in range(3):
        with sam_locks.registry_lock(exclusive=True, timeout=5):
            pass
        with sam_locks.name_lock("worker-1", timeout=5):
            pass
        with sam_locks.retry_queue_lock(exclusive=True, timeout=5):
            pass


def test_invalid_name_rejected():
    _init_home()
    with pytest.raises(ValueError):
        with sam_locks.name_lock("bad name!", timeout=1):
            pass


def test_registry_lock_blocks_another_process_and_is_freed_when_it_dies(tmp_path):
    _init_home()
    flag = tmp_path / "locked.flag"
    holder = subprocess.Popen([sys.executable, HELPER, "hold_lock", str(flag), "60"])
    try:
        for _ in range(100):
            if flag.exists():
                break
            time.sleep(0.1)
        assert flag.exists(), "holder never took the lock"
        with pytest.raises(sam_locks.LockTimeout):
            with sam_locks.registry_lock(exclusive=True, timeout=1):
                pass
    finally:
        holder.kill()
        holder.wait()
    with sam_locks.registry_lock(exclusive=True, timeout=5):   # freed by the OS
        pass


def test_shared_locks_coexist():
    _init_home()
    with sam_locks.registry_lock(exclusive=False, timeout=5):
        pass
