#!/usr/bin/env python3
"""SAM locks module: name lock + registry lock as context managers.

Spec: reviews-line-by-line.md — GLM-5.2, File: sam/locks.py
"""

import contextlib
import fcntl
import os
import re
import time

from sam import config as sam_config


class LockTimeout(Exception):
    """Raised when a lock cannot be acquired within the timeout period."""
    pass


_NAME_REGEX = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _acquire_lock(path, timeout, lock_flag):
    """Shared lock acquisition logic. Returns fd.

    Lines 3-9 (shared between name_lock and registry_lock):
    Line 3/3: deadline = time.monotonic() + timeout.
    Line 4/5: Loop while time.monotonic() < deadline:
    Line 5/6:   Try fcntl.flock(fd, lock_flag | LOCK_NB). If successful, break.
    Line 6/7:   If BlockingIOError, time.sleep(0.1) and continue.
    Line 7/8:   If InterruptedError, retry immediately.
    Line 8/9: If loop exits without acquiring, os.close(fd) and raise LockTimeout.
    """
    fd = None
    try:
        fd = os.open(
            path,
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
    except OSError:
        raise LockTimeout(f"Cannot open lock file: {path}")

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            fcntl.flock(fd, lock_flag | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            time.sleep(0.1)
            continue
        except InterruptedError:
            continue

    # Timeout
    os.close(fd)
    raise LockTimeout(f"Could not acquire lock within {timeout}s: {path}")


@contextlib.contextmanager
def name_lock(name, timeout=10):
    """
    Line 1: Validate name against regex. If invalid, raise ValueError.
    Line 2: path = sam.config.locks_dir() / f"{name}.lock".
    Line 3: fd = os.open(path, O_CREAT | O_RDWR | O_NOFOLLOW | O_CLOEXEC, 0o600).
    Line 4-9: Lock acquisition loop via _acquire_lock.
    Line 10: Yield the fd.
    Line 11: On exit: LOCK_UN, close fd.
    """
    if not _NAME_REGEX.match(name):
        raise ValueError(f"Invalid name format: {name!r}")

    path = sam_config.locks_dir() / f"{name}.lock"
    fd = _acquire_lock(path, timeout, fcntl.LOCK_EX)

    try:
        yield fd
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextlib.contextmanager
def registry_lock(exclusive=True, timeout=10):
    """
    Line 1: path = sam.config.get_sam_home() / "registry.lock".
    Line 2: fd = os.open(path, O_CREAT | O_RDWR | O_NOFOLLOW | O_CLOEXEC, 0o600).
    Line 3: deadline = time.monotonic() + timeout.
    Line 4: lock_flag = LOCK_EX if exclusive else LOCK_SH.
    Line 5-8: Lock acquisition loop.
    Line 9: If timeout, close fd, raise LockTimeout.
    Line 10: Yield fd.
    Line 11: On exit: LOCK_UN, close fd.
    """
    path = sam_config.get_sam_home() / "registry.lock"
    lock_flag = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    fd = _acquire_lock(path, timeout, lock_flag)

    try:
        yield fd
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
