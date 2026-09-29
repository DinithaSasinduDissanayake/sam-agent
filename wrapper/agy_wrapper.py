#!/usr/bin/env python3
"""SAM agy-wrapper — standalone executable wrapping agy CLI.

Launches agy, streams output to log file with sentinel markers,
writes result.json atomically. Has NO sam.* imports.
Stdlib only: argparse, subprocess, os, sys, json, time, signal, shutil, secrets, tempfile.

Session model: --session is a pointer file holding a conversation_id
(read if exists for resume via --conversation, written from the
JSON envelope on stdout after the child exits).
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
HARNESS = "agy"


def _read_conversation_id(session_path):
    """Read conversation_id from pointer file; None when absent/empty."""
    try:
        if not os.path.isfile(session_path):
            return None
        with open(session_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read().strip()
            return text.split()[0] if text else None
    except OSError:
        return None


_CANONICAL_KEY = "conversation_id"
_ALIAS_KEYS = ("conversationId", "conversation",
               "session_id", "sessionId", "id")


def _find_id_in_obj(obj):
    """Prefer canonical `conversation_id`; fall back to alias keys."""
    if isinstance(obj, dict):
        v = obj.get(_CANONICAL_KEY)
        if isinstance(v, str) and v.strip():
            return (v.strip(), _CANONICAL_KEY)
        for k in _ALIAS_KEYS:
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return (v.strip(), k)
        for vv in obj.values():
            if isinstance(vv, dict):
                found, key = _find_id_in_obj(vv)
                if found:
                    return (found, key)
    return (None, None)


def _extract_conversation_id_with_key(raw):
    """Extract (value, key) preferring `conversation_id` over aliases."""
    if not raw:
        return (None, None)
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    # 1) Whole-output JSON object.
    try:
        found, key = _find_id_in_obj(json.loads(text))
        if found:
            return (found, key)
    except (ValueError, TypeError):
        pass
    # 2) JSON-lines: scan lines in reverse for last envelope with an id.
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            found, key = _find_id_in_obj(json.loads(line))
        except (ValueError, TypeError):
            continue
        if found:
            return (found, key)
    return (None, None)


def _extract_conversation_id(raw, strict=False):
    """Extract conversation_id from agy's JSON envelope output.

    Strict: only whole-output JSON or a JSON-line envelope counts; on
    miss prints a warning to stderr (callers pass strict=True after a
    successful run so a missing id is visible instead of silent).
    """
    found, _key = _extract_conversation_id_with_key(raw)
    return found


def _extract_agy_result(raw):
    """Best-effort: stdout JSON envelope `response`, else stream-json last `result`.

    The real agy envelope is a single JSON object wrapped in
    ##AGY_BEGIN_/##AGY_END_ sentinel lines, e.g.
    {"conversation_id": "...", "status": "SUCCESS", "response": "..."}.
    Sentinels are stripped before parsing so the whole-output parse
    succeeds on the real shape; falls back to JSON-lines in reverse
    (last wins). A stream-json event {"type": "result", ...} yields its
    result/response/text field. Returns str, or None when absent/unparseable.
    """
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw

    def from_obj(obj):
        if not isinstance(obj, dict):
            return None
        # stream-json result event: {"type": "result", "result": "..."}
        if obj.get("type") == "result":
            for k in ("result", "response", "text", "output"):
                v = obj.get(k)
                if isinstance(v, str) and v:
                    return v
        v = obj.get("response")
        if isinstance(v, str) and v:
            return v
        return None

    # Strip wrapper sentinel lines so the whole-output parse sees the
    # real envelope shape (sentinels otherwise break json.loads).
    lines = [ln for ln in text.splitlines()
             if not (ln.startswith("##AGY_BEGIN_")
                     or ln.startswith("##AGY_END_"))]
    stripped = "\n".join(lines)
    try:
        found = from_obj(json.loads(stripped))
        if found is not None:
            return found
    except (ValueError, TypeError):
        pass
    for line in reversed(lines):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            found = from_obj(json.loads(line))
        except (ValueError, TypeError):
            continue
        if found is not None:
            return found
    return None


def _write_conversation_id(session_path, conversation_id):
    """Atomically write conversation_id pointer file. Best-effort."""
    try:
        parent = os.path.dirname(session_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=parent or ".",
            prefix=".tmp-conv-",
            suffix=".txt",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(conversation_id + "\n")
            os.replace(tmp_path, session_path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as e:
        print(f"agy-wrapper: session pointer write failed: {e}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(prog="agy-wrapper")
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--effort", default=None,
                        choices=["low", "medium", "high", "max"])
    parser.add_argument("--resume", action="store_true", default=False,
                        help="Strict resume: require conversation_id pointer "
                             "before launch and envelope after; exit 3 "
                             "instead of starting fresh.")
    args = parser.parse_args()

    # Validate task file exists
    task_path = args.task
    if not os.path.isfile(task_path):
        print(f"agy-wrapper: task file not found: {task_path}", file=sys.stderr)
        sys.exit(1)

    # Read task contents for -p "$(cat task)"
    try:
        with open(task_path, "r", encoding="utf-8", errors="replace") as f:
            prompt_text = f.read()
    except OSError as e:
        print(f"agy-wrapper: cannot read task file: {e}", file=sys.stderr)
        sys.exit(1)
    if not prompt_text.strip():
        print("agy-wrapper: task file is empty", file=sys.stderr)
        sys.exit(1)

    # Derive log path from result path
    result_dir = os.path.dirname(args.result)
    log_path = os.path.join(result_dir, "output.log")

    # Verify paths are inside same run directory (security)
    run_dir = os.path.normpath(result_dir)
    for path_name, path_value in [("result", args.result), ("log", log_path)]:
        resolved = os.path.normpath(os.path.dirname(path_value))
        if not resolved.startswith(run_dir + "/") and resolved != run_dir:
            print(f"agy-wrapper: security violation — {path_name} path outside run dir",
                  file=sys.stderr)
            sys.exit(1)
    # Session path may be at agent root (above run dir) — allow it
    agent_root = os.path.normpath(os.path.dirname(run_dir))
    session_resolved = os.path.normpath(os.path.dirname(args.session))
    if not session_resolved.startswith(agent_root + "/") and session_resolved != agent_root:
        print("agy-wrapper: security violation — session path outside agent dir",
              file=sys.stderr)
        sys.exit(1)

    # Read existing conversation_id for resume
    conversation_id = _read_conversation_id(args.session)

    # Strict resume: no silent fresh conversation. Fail before any agy call.
    if args.resume and not conversation_id:
        msg = "agy-wrapper: no conversation_id, use spawn not resume"
        print(msg, file=sys.stderr)
        try:
            os.makedirs(result_dir, exist_ok=True)
            _write_failed_result(args.result, args.agent_id, time.time(),
                                 msg, session_path=args.session,
                                 conversation_id=None)
        except Exception:
            pass
        sys.exit(3)

    # Generate sentinel
    sentinel = secrets.token_hex(4)  # 8 hex chars

    # Open log file and write BEGIN sentinel
    os.makedirs(result_dir, exist_ok=True)
    started_at = time.time()
    captured = bytearray()

    try:
        with open(log_path, "wb", buffering=0) as log:
            log.write(f"##AGY_BEGIN_{sentinel}\n".encode())

            # Resolve agy executable
            agy_bin = shutil.which("agy")
            if agy_bin is None:
                # Write failed result
                _write_failed_result(args.result, args.agent_id, started_at,
                                     "agy binary not found")
                print("agy-wrapper: agy binary not found", file=sys.stderr)
                sys.exit(1)

            agy_basename = os.path.basename(agy_bin)
            if agy_basename != "agy":
                _write_failed_result(args.result, args.agent_id, started_at,
                                     f"allowlist: expected 'agy', got '{agy_basename}'")
                print(f"agy-wrapper: allowlist violation — {agy_basename}", file=sys.stderr)
                sys.exit(1)

            # Launch agy
            agy_argv = [
                agy_bin,
                "--model", args.model,
                "-p", prompt_text,
                "--output-format", "json",
                "--print-timeout", "15m",
            ]
            if conversation_id:
                agy_argv.extend(["--conversation", conversation_id])
            if args.effort:
                agy_argv.extend(["--effort", args.effort])

            try:
                child = subprocess.Popen(
                    agy_argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    bufsize=0,
                )
            except Exception as e:
                _write_failed_result(args.result, args.agent_id, started_at,
                                     f"Popen failed: {e}")
                print(f"agy-wrapper: Popen failed: {e}", file=sys.stderr)
                sys.exit(1)

            # Stream output in 64KB chunks
            try:
                while True:
                    chunk = child.stdout.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    log.write(chunk)
                    captured.extend(chunk)
            except Exception as e:
                # Best-effort: stream failed, but keep going for result
                print(f"agy-wrapper: stream error: {e}", file=sys.stderr)

            child.wait()

            # Write END sentinel
            ended_at = time.time()
            log.write(f"##AGY_END_{sentinel}\n".encode())
            log.flush()

    except OSError as e:
        # Disk full or log write failure
        _write_failed_result(args.result, args.agent_id, started_at,
                             f"log write failed: {e}")
        print(f"agy-wrapper: {e}", file=sys.stderr)
        sys.exit(1)

    # Persist conversation_id from JSON envelope for resume.
    # Strict resume: prefer `conversation_id`, warn on alias keys, exit 3
    # on miss without overwriting the pointer or claiming continued.
    new_conversation_id, used_key = _extract_conversation_id_with_key(
        bytes(captured))
    resume_envelope_miss = False
    if new_conversation_id:
        if used_key != _CANONICAL_KEY:
            print(f"agy-wrapper: warning: using alias key '{used_key}' "
                  f"for conversation_id; prefer '{_CANONICAL_KEY}'",
                  file=sys.stderr)
        _write_conversation_id(args.session, new_conversation_id)
        conversation_id = new_conversation_id
    elif args.resume:
        resume_envelope_miss = True
        print("agy-wrapper: no conversation_id in output; "
              "resume not continued", file=sys.stderr)
    else:
        print("agy-wrapper: warning: no conversation_id envelope in output; "
              "resume will start a fresh conversation", file=sys.stderr)

    # Determine final state
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

    # Build result object (unified schema shared with pi-wrapper:
    # agent_id, harness, exit_code, exit_signal, final_state_hint,
    # duration_ms, wrapper_version, conversation_id, session_path, result;
    # plus legacy started_at/ended_at/output_path/task_path).
    # Final-only: result = JSON envelope `response` (or last `result` event).
    duration_ms = int((ended_at - started_at) * 1000)
    result = {
        "agent_id": args.agent_id,
        "harness": HARNESS,
        "exit_code": exit_code,
        "exit_signal": exit_signal,
        "final_state_hint": final_hint,
        "duration_ms": duration_ms,
        "wrapper_version": WRAPPER_VERSION,
        "conversation_id": conversation_id,
        "session_path": args.session,
        "result": _extract_agy_result(bytes(captured)),
        "started_at": started_at,
        "ended_at": ended_at,
        "output_path": log_path,
        "task_path": args.task,
    }

    # Write result.json atomically
    _write_result_atomic(args.result, result)

    # Strict resume miss: pointer untouched above; exit 3, never claim continued.
    if resume_envelope_miss:
        sys.exit(3)

    # Exit with appropriate code
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


def _write_failed_result(result_path, agent_id, started_at, error_message,
                         session_path=None, conversation_id=None):
    """Best-effort write a failed result.json (unified schema)."""
    try:
        result = {
            "agent_id": agent_id,
            "harness": HARNESS,
            "exit_code": -1,
            "exit_signal": None,
            "final_state_hint": "failed",
            "duration_ms": 0,
            "wrapper_version": WRAPPER_VERSION,
            "conversation_id": conversation_id,
            "session_path": session_path,
            "result": None,
            "started_at": started_at,
            "ended_at": time.time(),
            "error": error_message,
        }
        _write_result_atomic(result_path, result)
    except Exception:
        pass  # Best-effort only


if __name__ == "__main__":
    main()
