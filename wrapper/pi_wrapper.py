#!/usr/bin/env python3
"""SAM pi-wrapper — standalone executable wrapping pi CLI.

Launches pi, streams output to log file with sentinel markers,
writes result.json atomically. Has NO sam.* imports.
Stdlib only: argparse, subprocess, os, sys, json, time, signal, shutil, secrets, tempfile.
"""

import argparse
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time


WRAPPER_VERSION = "0.1.0"
CHUNK_SIZE = 65536  # 64KB


def main():
    """
    Line 1: Parse args.
    Line 2: Require --agent-id, --model, --session, --task, --result.
    Line 3: Validate paths exist before launching child.
    Line 4: Derive log_path = result_dir / "output.log".
    Line 5-6: Verify all paths are inside same run directory.
    Line 7: Generate 8-char hex sentinel.
    Line 8-9: Open log file, write BEGIN sentinel.
    Line 10-12: Resolve pi executable, verify basename.
    Line 13-18: Launch pi with Popen, stream output.
    Line 19-21: Read 64KB chunks, write to log.
    Line 22: Write END sentinel after child exits.
    Line 23-27: Determine final_state_hint from return code.
    Line 28: Build result object.
    Line 29-30: Write result.json atomically.
    Line 31: Close log, exit with appropriate code.
    """
    # Line 1-2: Parse arguments
    parser = argparse.ArgumentParser(prog="pi-wrapper")
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args()

    # Line 3: Validate task file exists
    task_path = args.task
    if not os.path.isfile(task_path):
        print(f"pi-wrapper: task file not found: {task_path}", file=sys.stderr)
        sys.exit(1)

    # Line 4: Derive log path from result path
    result_dir = os.path.dirname(args.result)
    log_path = os.path.join(result_dir, "output.log")

    # Line 5-6: Verify paths are inside same run directory (security)
    run_dir = os.path.normpath(result_dir)
    for path_name, path_value in [("result", args.result), ("log", log_path)]:
        resolved = os.path.normpath(os.path.dirname(path_value))
        if not resolved.startswith(run_dir + "/") and resolved != run_dir:
            print(f"pi-wrapper: security violation — {path_name} path outside run dir",
                  file=sys.stderr)
            sys.exit(1)
    # Session path may be at agent root (above run dir) — allow it
    agent_root = os.path.normpath(os.path.dirname(run_dir))
    session_resolved = os.path.normpath(os.path.dirname(args.session))
    if not session_resolved.startswith(agent_root + "/") and session_resolved != agent_root:
        print("pi-wrapper: security violation — session path outside agent dir",
              file=sys.stderr)
        sys.exit(1)

    # Line 7: Generate sentinel
    sentinel = secrets.token_hex(4)  # 8 hex chars

    # Line 8-9: Open log file and write BEGIN sentinel
    os.makedirs(result_dir, exist_ok=True)
    started_at = time.time()

    try:
        with open(log_path, "wb", buffering=0) as log:
            log.write(f"##PI_BEGIN_{sentinel}\n".encode())

            # Line 10-12: Resolve pi executable
            pi_bin = shutil.which("pi")
            if pi_bin is None:
                # Write failed result
                _write_failed_result(args.result, args.agent_id, started_at,
                                     "pi binary not found")
                print("pi-wrapper: pi binary not found", file=sys.stderr)
                sys.exit(1)

            pi_basename = os.path.basename(pi_bin)
            if pi_basename != "pi":
                _write_failed_result(args.result, args.agent_id, started_at,
                                     f"allowlist: expected 'pi', got '{pi_basename}'")
                print(f"pi-wrapper: allowlist violation — {pi_basename}", file=sys.stderr)
                sys.exit(1)

            # Line 13-18: Launch pi
            pi_argv = [
                pi_bin,
                "--print",
                "--model", args.model,
                "--session", args.session,
                f"@{args.task}",
            ]

            try:
                child = subprocess.Popen(
                    pi_argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    bufsize=0,
                )
            except Exception as e:
                _write_failed_result(args.result, args.agent_id, started_at,
                                     f"Popen failed: {e}")
                print(f"pi-wrapper: Popen failed: {e}", file=sys.stderr)
                sys.exit(1)

            # Line 19-21: Stream output in 64KB chunks
            try:
                while True:
                    chunk = child.stdout.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    log.write(chunk)
            except Exception as e:
                # Best-effort: stream failed, but keep going for result
                print(f"pi-wrapper: stream error: {e}", file=sys.stderr)

            child.wait()

            # Line 22: Write END sentinel
            ended_at = time.time()
            log.write(f"##PI_END_{sentinel}\n".encode())
            log.flush()

    except OSError as e:
        # Disk full or log write failure
        _write_failed_result(args.result, args.agent_id, started_at,
                             f"log write failed: {e}")
        print(f"pi-wrapper: {e}", file=sys.stderr)
        sys.exit(1)

    # Line 23-27: Determine final state
    returncode = child.returncode
    exit_code = returncode
    exit_signal = None
    final_hint = "completed"

    if returncode < 0:
        # Exited by signal
        exit_signal = -returncode
        exit_code = None
        final_hint = "failed"
    elif returncode == 0:
        final_hint = "completed"
    else:
        final_hint = "failed"

    # Line 28: Build result object
    duration_ms = int((ended_at - started_at) * 1000)
    result = {
        "agent_id": args.agent_id,
        "exit_code": exit_code,
        "exit_signal": exit_signal,
        "final_state_hint": final_hint,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_ms": duration_ms,
        "wrapper_version": WRAPPER_VERSION,
        "output_path": log_path,
        "task_path": args.task,
        "session_path": args.session,
    }

    # Line 29-30: Write result.json atomically
    _write_result_atomic(args.result, result)

    # Line 31-34: Exit with appropriate code
    if exit_signal is not None:
        sys.exit(128 + exit_signal)
    elif returncode == 0:
        sys.exit(0)
    else:
        sys.exit(1)


def _write_result_atomic(result_path, result_data):
    """Write result.json atomically using temp file + fsync + os.replace."""
    tmp_path = None
    fd = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=os.path.dirname(result_path),
            prefix=".tmp-",
            suffix=".json",
        )
        os.close(fd)
        fd = None

        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.write(fd, json.dumps(result_data, indent=2).encode("utf-8"))
        os.fsync(fd)
        os.close(fd)
        fd = None

        os.replace(tmp_path, result_path)

        # fsync parent directory
        dir_fd = os.open(os.path.dirname(result_path), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    except BaseException:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


def _write_failed_result(result_path, agent_id, started_at, error_message):
    """Best-effort write a failed result.json."""
    try:
        result = {
            "agent_id": agent_id,
            "exit_code": -1,
            "exit_signal": None,
            "final_state_hint": "failed",
            "started_at": started_at,
            "ended_at": time.time(),
            "duration_ms": 0,
            "wrapper_version": WRAPPER_VERSION,
            "error": error_message,
        }
        _write_result_atomic(result_path, result)
    except Exception:
        pass  # Best-effort only


if __name__ == "__main__":
    main()
