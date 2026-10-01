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
import re
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


_PARTIAL_CAP = 65536


def _write_partial_md(result_dir, agent_id, log_path, resp_text):
    """Write PARTIAL.md next to result.json; return path or None.

    Contents: truncated captured response, workspace path, log path, and
    the one-line parent contract (verify before respawn).
    """
    path = os.path.join(result_dir, "PARTIAL.md")
    body = resp_text
    if len(body) > _PARTIAL_CAP:
        body = body[:_PARTIAL_CAP] + "\n\n... [truncated by sam wrapper]"
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                "# PARTIAL run — verify before respawn\n\n"
                f"- agent: {agent_id}\n"
                f"- workspace: {os.getcwd()}\n"
                f"- log: {log_path}\n"
                "- state: partial (run failed, but the envelope carried a "
                "non-empty response)\n\n"
                "Parent contract: read this file AND the workspace files it "
                "names before respawning. Respawning without reading this "
                "file is an operator error.\n\n"
                "## Captured response\n\n" + body + "\n")
        return path
    except OSError:
        return None


def _read_conversation_id(session_path):
    """Read conversation_id from pointer file; None when absent/empty."""
    try:
        if not session_path or not os.path.isfile(session_path):
            return None
        with open(session_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read().strip()
            return text if re.fullmatch(r"[A-Za-z0-9_-]+", text) else None
    except OSError:
        return None


_CANONICAL_KEY = "conversation_id"


def _find_id_in_obj(obj):
    """Only the documented conversation_id field identifies a conversation."""
    if isinstance(obj, dict):
        v = obj.get(_CANONICAL_KEY)
        if isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_-]+", v):
            return (v, _CANONICAL_KEY)
    return (None, None)


def _extract_conversation_id_with_key(raw):
    """Extract the ID from the last terminal envelope, never progress/tool IDs."""
    return _find_id_in_obj(_extract_agy_envelope(raw))


def _extract_agy_envelope(raw):
    """JSON envelope or documented event=result payload (headless schema)."""
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    text = "\n".join(ln for ln in text.splitlines()
                     if not ln.startswith(("##AGY_BEGIN_", "##AGY_END_")))

    def terminal(obj):
        if not isinstance(obj, dict):
            return None
        if obj.get("event") == "result":
            return obj.get("result") if isinstance(obj.get("result"), dict) else {}
        if "event" not in obj and "status" in obj:
            return obj
        return None

    # 1) Whole-output JSON object.
    try:
        found = terminal(json.loads(text))
        if found is not None:
            return found
    except (ValueError, TypeError):
        pass
    # 2) JSON-lines: scan lines in reverse for last envelope with an id.
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            found = terminal(json.loads(line))
        except (ValueError, TypeError):
            continue
        if found is not None:
            return found
    return None


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
    """Atomically write conversation_id pointer file; report persistence failure."""
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
        return True
    except Exception as e:
        print(f"agy-wrapper: session pointer write failed: {e}", file=sys.stderr)
        return False


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
                             "instead of starting fresh. Two failure modes: "
                             "resume_rejected (not continued — spawn fresh) "
                             "vs resumed_then_failed (continued, then "
                             "errored — back off and resume again).")
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
                                  conversation_id=None, exit_code=3)
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

            # Launch agy with no SAM wall-clock timeout: agy's own default
            # for --print-timeout is 0s ("wait until the turn completes"),
            # passed explicitly below. `sam wait --timeout` is separate:
            # its observation deadline attempts worker termination on expiry.
            agy_argv = [
                agy_bin,
                "--model", args.model,
                "-p", prompt_text,
                "--output-format", "json",
                "--print-timeout", "0s",
            ]
            if conversation_id:
                agy_argv.extend(["--conversation", conversation_id])
            if args.effort:
                agy_argv.extend(["--effort", args.effort])

            child_env = os.environ.copy()
            for var in ("SSH_CLIENT", "SSH_CONNECTION", "SSH_TTY"):
                child_env.pop(var, None)
            if "DBUS_SESSION_BUS_ADDRESS" not in child_env:
                uid_bus = f"/run/user/{os.getuid()}/bus"
                if os.path.exists(uid_bus):
                    child_env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={uid_bus}"

            try:
                child = subprocess.Popen(
                    agy_argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    bufsize=0,
                    env=child_env,
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

    envelope = _extract_agy_envelope(bytes(captured))
    new_conversation_id, _ = _find_id_in_obj(envelope)
    has_id = new_conversation_id is not None
    envelope_status = (envelope.get("status") if isinstance(envelope, dict)
                       else None)
    valid_envelope = (has_id and envelope_status == "SUCCESS" and
                      isinstance(envelope.get("response"), str)
                      if isinstance(envelope, dict) else False)
    continuation = args.resume or conversation_id is not None
    strict_error = None
    error_kind = None
    if continuation:
        if not has_id or new_conversation_id != conversation_id:
            # Resume rejected: the server did not continue our
            # conversation. Opposite action from a mid-flight failure:
            # spawn a fresh run, do not retry this resume.
            strict_error = (
                "agy-wrapper: resume_rejected (conversation not continued; "
                "pointer missing or mismatched — spawn a fresh run, "
                "do not retry resume)")
            error_kind = "resume_rejected"
        elif not valid_envelope:
            # The resume genuinely continued (id matches, turns executed)
            # but the continued session errored — e.g. 429 mid-resume.
            # Opposite action: keep the pointer, back off, resume again.
            raw_err = (envelope.get("error") if isinstance(envelope, dict)
                       else None)
            short = (str(raw_err)[:160] if raw_err
                     else f"status {envelope_status}")
            strict_error = (
                f"agy-wrapper: resumed_then_failed ({short}; conversation "
                f"{conversation_id} was continued — back off, then resume "
                f"again; do not spawn fresh)")
            error_kind = "resumed_then_failed"
        # else: clean continuation; pointer already on disk.
    elif has_id:
        # Fresh launch (success or failure): ALWAYS persist a present id.
        # First-attempt failures (429, network) used to drop the pointer
        # via the SUCCESS gate, making them unresumable — fixed here.
        if not _write_conversation_id(args.session, new_conversation_id):
            strict_error = ("agy-wrapper: conversation_id pointer "
                            "persistence failed")
            error_kind = "pointer_persist_failed"
        else:
            conversation_id = new_conversation_id
    else:
        print("agy-wrapper: warning: no valid conversation_id envelope; "
              "fresh conversation cannot be resumed", file=sys.stderr)

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

    if strict_error:
        print(strict_error, file=sys.stderr)
        exit_code = 3
        exit_signal = None
        final_hint = "failed"
    elif envelope is not None and envelope.get("status") != "SUCCESS" and returncode == 0:
        exit_code = 1
        final_hint = "failed"

    # Partial capture: a failed run whose envelope carries a non-empty
    # `response` produced deliverable text (d2c87d-class: full summary
    # inside the error envelope, files on disk before death). Report
    # state `partial` + PARTIAL.md instead of bare `failed` so the parent
    # verifies workspace files instead of blindly respawning.
    resp_text = (envelope.get("response")
                 if isinstance(envelope, dict)
                 and isinstance(envelope.get("response"), str)
                 else None)
    if resp_text is not None and not resp_text.strip():
        resp_text = None
    partial_path = None
    if (final_hint != "completed" and resp_text
            and envelope.get("status") != "SUCCESS"):
        final_hint = "partial"
        partial_path = _write_partial_md(result_dir, args.agent_id,
                                         log_path, resp_text)

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
        "result": envelope.get("response") if valid_envelope and final_hint == "completed" else None,
        "session_continued": bool(continuation and valid_envelope and final_hint == "completed"),
        "started_at": started_at,
        "ended_at": ended_at,
        "output_path": log_path,
        "task_path": args.task,
    }
    if strict_error:
        result["error"] = strict_error
        if error_kind:
            result["error_kind"] = error_kind
    if final_hint == "partial":
        result["result_partial"] = (
            resp_text if len(resp_text) <= _PARTIAL_CAP
            else resp_text[:_PARTIAL_CAP])
        result["partial_reason"] = ("run failed but envelope response "
                                    "was non-empty")
        result["partial_path"] = partial_path

    # Write result.json atomically
    _write_result_atomic(args.result, result)

    # Strict resume miss: pointer untouched above; exit 3, never claim continued.
    if strict_error:
        sys.exit(3)

    # Exit with appropriate code
    if exit_signal is not None:
        sys.exit(128 + exit_signal)
    elif exit_code == 0:
        sys.exit(0)
    else:
        sys.exit(exit_code)


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
                         session_path=None, conversation_id=None, exit_code=1):
    """Best-effort write a failed result.json (unified schema)."""
    try:
        result = {
            "agent_id": agent_id,
            "harness": HARNESS,
            "exit_code": exit_code,
            "exit_signal": None,
            "final_state_hint": "failed",
            "duration_ms": 0,
            "wrapper_version": WRAPPER_VERSION,
            "conversation_id": conversation_id,
            "session_path": session_path,
            "result": None,
            "session_continued": False,
            "started_at": started_at,
            "ended_at": time.time(),
            "error": error_message,
        }
        _write_result_atomic(result_path, result)
    except Exception:
        pass  # Best-effort only


if __name__ == "__main__":
    main()
