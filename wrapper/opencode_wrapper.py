#!/usr/bin/env python3
"""SAM opencode-wrapper — standalone executable wrapping the OpenCode CLI.

Runs `opencode run --format json --auto -m MODEL [--variant V] [--session ID]`
with the task text as the prompt, streams stdout+stderr into output.log
between ##OPENCODE_BEGIN_<hex> / ##OPENCODE_END_<hex> sentinels, and writes
result.json atomically (unified SAM schema plus opencode extras).
Has NO sam.* imports. Stdlib only.

Session model: --session is a pointer FILE holding the opencode session id.
A fresh run (no --resume) ignores any old pointer, starts a new session,
and writes the new id to the pointer as soon as the first event names it.
--resume requires the pointer and passes `--session <id>`; a run whose
first reported session id differs is `resume_rejected` (exit 3).

Infra classification (`infra_hint`) looks only at error events, HTTP status
codes inside them, and non-JSON (stderr) lines — never at model text.
"""

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time


WRAPPER_VERSION = "0.1.0"
CHUNK_SIZE = 65536  # 64KB
HARNESS = "opencode"
EFFORTS = ("minimal", "low", "medium", "high", "max")
# Discovery D4. True: the prompt is written to opencode's stdin.
# False: the prompt is passed as one argv element after "--".
PROMPT_VIA_STDIN = True
MAX_ARGV_PROMPT_BYTES = 120000
STARTUP_DEATH_S = 120
_PARTIAL_CAP = 65536
_ID_RE = re.compile(r"[A-Za-z0-9_-]+")
SESSION_KEYS = ("sessionID", "sessionId", "session_id")
# A text event after one of these starts a new "step"; the final answer is
# the text of the last step.
BOUNDARY_TYPES = ("step_start", "tool_use")

_QUOTA_RES = [re.compile(p, re.IGNORECASE) for p in (
    r"(?<![\w:.])429(?![\w:.])",
    r"too many requests",
    r"rate[ _-]?limit",
    r"RESOURCE_EXHAUSTED",
    r"\bquota\b",
    r"FreeTierError",
    r"free[ _-]?tier",
)]
_NETWORK_RES = [re.compile(p, re.IGNORECASE) for p in (
    r"ECONNRESET",
    r"ETIMEDOUT",
    r"ENOTFOUND",
    r"EAI_AGAIN",
    r"ECONNREFUSED",
    r"fetch failed",
    r"socket hang up",
    r"unable to connect",
    r"network error",
    r"TLS handshake",
    r"connection reset",
    r"connection refused",
    r"unexpected EOF",
)]


# ── pure helpers (unit-tested) ────────────────────────────────────────────────

def _session_id_of(obj):
    """Session id from an event (top level first, then obj['part'])."""
    if not isinstance(obj, dict):
        return None
    for holder in (obj, obj.get("part")):
        if isinstance(holder, dict):
            for key in SESSION_KEYS:
                v = holder.get(key)
                if isinstance(v, str) and _ID_RE.fullmatch(v):
                    return v
    return None


def _session_id_from_line(line):
    """Session id from one raw output line (bytes or str), else None."""
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="replace")
    s = line.strip()
    if not s.startswith("{"):
        return None
    try:
        obj = json.loads(s)
    except ValueError:
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("type"), str):
        return None
    return _session_id_of(obj)


def _status_codes(value, found, depth=0):
    """Collect HTTP-like status codes (100-599) found anywhere in value."""
    if depth > 6:
        return found
    if isinstance(value, dict):
        for k, v in value.items():
            if (k in ("statusCode", "status_code", "status")
                    and isinstance(v, int) and not isinstance(v, bool)
                    and 100 <= v <= 599):
                found.add(v)
            else:
                _status_codes(v, found, depth + 1)
    elif isinstance(value, list):
        for v in value:
            _status_codes(v, found, depth + 1)
    return found


def _error_text(obj):
    """Readable text of an error event (its `error` field, else the event)."""
    err = obj.get("error")
    if err is None:
        err = {k: v for k, v in obj.items()
               if k not in ("type", "timestamp") and k not in SESSION_KEYS}
    if isinstance(err, str):
        return err[:4000]
    try:
        return json.dumps(err, sort_keys=True)[:4000]
    except (TypeError, ValueError):
        return str(err)[:4000]


def analyze_output(raw):
    """Parse captured opencode output. Pure; never raises.

    Returns dict: session_ids (first-seen order), final_text (text of the
    last step or None), all_text, usage (dict or None), errors (list of
    str), status_codes (sorted list), stderr_lines (non-JSON lines),
    event_count, event_types ({type: count}).
    """
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = raw or ""
    out = {"session_ids": [], "final_text": None, "all_text": "",
           "usage": None, "errors": [], "status_codes": [],
           "stderr_lines": [], "event_count": 0, "event_types": {}}
    texts_by_part = {}
    segment = {}
    usage = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0,
             "cache_read_tokens": 0, "cache_write_tokens": 0, "cost": 0.0}
    usage_seen = False
    codes = set()
    anon = 0
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith(("##OPENCODE_BEGIN_", "##OPENCODE_END_")):
            continue
        obj = None
        if s.startswith("{"):
            try:
                obj = json.loads(s)
            except ValueError:
                obj = None
        if not isinstance(obj, dict) or not isinstance(obj.get("type"), str):
            out["stderr_lines"].append(s[:500])
            continue
        etype = obj["type"]
        out["event_count"] += 1
        out["event_types"][etype] = out["event_types"].get(etype, 0) + 1
        sid = _session_id_of(obj)
        if sid and sid not in out["session_ids"]:
            out["session_ids"].append(sid)
        part = obj.get("part") if isinstance(obj.get("part"), dict) else {}
        if etype in BOUNDARY_TYPES:
            segment = {}
        if etype == "text":
            t = part.get("text") if isinstance(part.get("text"), str) else obj.get("text")
            if isinstance(t, str):
                pid = part.get("id") if isinstance(part.get("id"), str) else None
                if pid is None:
                    pid = "anon-%d" % anon
                    anon += 1
                texts_by_part[pid] = t
                segment[pid] = t
        elif etype == "step_finish":
            tok = part.get("tokens") if isinstance(part.get("tokens"), dict) else obj.get("tokens")
            if isinstance(tok, dict):
                usage_seen = True
                for src, dst in (("input", "input_tokens"), ("output", "output_tokens"),
                                 ("reasoning", "reasoning_tokens")):
                    v = tok.get(src)
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        usage[dst] += int(v)
                cache = tok.get("cache") if isinstance(tok.get("cache"), dict) else {}
                for src, dst in (("read", "cache_read_tokens"), ("write", "cache_write_tokens")):
                    v = cache.get(src)
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        usage[dst] += int(v)
            cost = part.get("cost", obj.get("cost"))
            if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                usage["cost"] += float(cost)
        elif etype == "error":
            out["errors"].append(_error_text(obj))
            _status_codes(obj.get("error", obj), codes)
    final = "\n".join(segment.values())
    out["final_text"] = final if final.strip() else None
    out["all_text"] = "\n".join(texts_by_part.values())
    out["usage"] = usage if usage_seen else None
    out["status_codes"] = sorted(codes)
    return out


def classify_infra(analysis, duration_s):
    """'quota' | 'startup-network' | None.

    Looks ONLY at error events, status codes inside them, and non-JSON
    (stderr) lines. Model text is never inspected. A generic 403 is not
    infra; a free-tier 403 counts as quota.
    """
    codes = set(analysis.get("status_codes") or [])
    haystack = "\n".join(list(analysis.get("errors") or [])
                         + list(analysis.get("stderr_lines") or []))
    if 429 in codes or any(r.search(haystack) for r in _QUOTA_RES):
        return "quota"
    if any(r.search(haystack) for r in _NETWORK_RES):
        if duration_s is None or duration_s <= STARTUP_DEATH_S:
            return "startup-network"
    return None


# ── file helpers ──────────────────────────────────────────────────────────────

def _read_pointer(session_path):
    """Session id from the pointer file; None when absent/invalid."""
    try:
        if not session_path or not os.path.isfile(session_path):
            return None
        with open(session_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read().strip()
        return text if _ID_RE.fullmatch(text) else None
    except OSError:
        return None


def _write_pointer(session_path, session_id):
    """Atomically write the session pointer; True on success."""
    try:
        parent = os.path.dirname(session_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=parent or ".", prefix=".tmp-sess-",
                                        suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(session_id + "\n")
            os.replace(tmp_path, session_path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return True
    except Exception as e:
        print(f"opencode-wrapper: session pointer write failed: {e}", file=sys.stderr)
        return False


def _write_partial_md(result_dir, agent_id, log_path, text):
    """Write PARTIAL.md next to result.json; return its path or None."""
    path = os.path.join(result_dir, "PARTIAL.md")
    body = text
    if len(body) > _PARTIAL_CAP:
        body = body[:_PARTIAL_CAP] + "\n\n... [truncated by sam wrapper]"
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                "# PARTIAL run — verify before respawn\n\n"
                f"- agent: {agent_id}\n"
                f"- workspace: {os.getcwd()}\n"
                f"- log: {log_path}\n"
                "- state: partial (run failed, but opencode produced text "
                "before failing)\n\n"
                "Parent contract: read this file AND the workspace files it "
                "names before respawning. Respawning without reading this "
                "file is an operator error.\n\n"
                "## Captured text\n\n" + body + "\n")
        return path
    except OSError:
        return None


def _write_result_atomic(result_path, result_data):
    """Write result.json atomically using temp file + fsync + os.replace."""
    tmp_path = None
    fd = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(result_path),
                                        prefix=".tmp-", suffix=".json")
        os.close(fd)
        fd = None
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.write(fd, json.dumps(result_data, indent=2).encode("utf-8"))
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(tmp_path, result_path)
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


def _base_result(agent_id, session_path, started_at, ended_at, task_path,
                 log_path, variant):
    return {
        "agent_id": agent_id,
        "harness": HARNESS,
        "exit_code": None,
        "exit_signal": None,
        "final_state_hint": "failed",
        "duration_ms": max(0, int(((ended_at or started_at) - started_at) * 1000)),
        "wrapper_version": WRAPPER_VERSION,
        "conversation_id": None,
        "session_path": session_path,
        "result": None,
        "session_id": None,
        "session_continued": False,
        "variant": variant,
        "usage": None,
        "infra_hint": None,
        "event_count": 0,
        "prompt_via": "stdin" if PROMPT_VIA_STDIN else "argv",
        "started_at": started_at,
        "ended_at": ended_at,
        "output_path": log_path,
        "task_path": task_path,
    }


def _write_failed_result(result_path, agent_id, started_at, error_message,
                         session_path=None, exit_code=1, error_kind=None,
                         task_path=None, log_path=None, variant=None):
    """Best-effort failed result.json (unified schema)."""
    try:
        data = _base_result(agent_id, session_path, started_at, time.time(),
                            task_path, log_path, variant)
        data["exit_code"] = exit_code
        data["error"] = error_message
        if error_kind:
            data["error_kind"] = error_kind
        _write_result_atomic(result_path, data)
    except Exception:
        pass  # best-effort only


def _feed_stdin(pipe, data):
    try:
        pipe.write(data)
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            pipe.close()
        except (BrokenPipeError, OSError, ValueError):
            pass


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(prog="opencode-wrapper")
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--effort", default=None, choices=list(EFFORTS))
    parser.add_argument("--resume", action="store_true", default=False,
                        help="Continue the session named in the pointer file "
                             "(opencode run --session <id>); exit 3 when the "
                             "pointer is missing or opencode used another session.")
    args = parser.parse_args()

    task_path = args.task
    if not os.path.isfile(task_path):
        print(f"opencode-wrapper: task file not found: {task_path}", file=sys.stderr)
        sys.exit(1)
    try:
        with open(task_path, "r", encoding="utf-8", errors="replace") as f:
            prompt_text = f.read()
    except OSError as e:
        print(f"opencode-wrapper: cannot read task file: {e}", file=sys.stderr)
        sys.exit(1)
    if not prompt_text.strip():
        print("opencode-wrapper: task file is empty", file=sys.stderr)
        sys.exit(1)

    result_dir = os.path.dirname(args.result)
    log_path = os.path.join(result_dir, "output.log")

    # Verify paths are inside the same run directory (security).
    run_dir = os.path.normpath(result_dir)
    for path_name, path_value in [("result", args.result), ("log", log_path)]:
        resolved = os.path.normpath(os.path.dirname(path_value))
        if not resolved.startswith(run_dir + "/") and resolved != run_dir:
            print(f"opencode-wrapper: security violation — {path_name} path outside run dir",
                  file=sys.stderr)
            sys.exit(1)
    # Session pointer may be at the agent root (above the run dir).
    agent_root = os.path.normpath(os.path.dirname(run_dir))
    session_resolved = os.path.normpath(os.path.dirname(args.session))
    if not session_resolved.startswith(agent_root + "/") and session_resolved != agent_root:
        print("opencode-wrapper: security violation — session path outside agent dir",
              file=sys.stderr)
        sys.exit(1)

    os.makedirs(result_dir, exist_ok=True)
    started_at = time.time()
    common = dict(session_path=args.session, task_path=args.task,
                  log_path=log_path, variant=args.effort)

    pointer_id = _read_pointer(args.session) if args.resume else None
    if args.resume and not pointer_id:
        msg = "opencode-wrapper: no session id, use spawn not resume"
        print(msg, file=sys.stderr)
        _write_failed_result(args.result, args.agent_id, started_at, msg,
                             exit_code=3, error_kind="resume_no_pointer", **common)
        sys.exit(3)

    prompt_bytes = prompt_text.encode("utf-8")
    if not PROMPT_VIA_STDIN and len(prompt_bytes) > MAX_ARGV_PROMPT_BYTES:
        msg = ("opencode-wrapper: task file too large for argv (%d bytes > %d); "
               "reference large inputs by file path inside a shorter task"
               % (len(prompt_bytes), MAX_ARGV_PROMPT_BYTES))
        print(msg, file=sys.stderr)
        _write_failed_result(args.result, args.agent_id, started_at, msg,
                             exit_code=1, error_kind="task_too_large", **common)
        sys.exit(1)

    sentinel = secrets.token_hex(4)
    captured = bytearray()
    persisted_id = None
    pointer_write_failed = False

    try:
        with open(log_path, "wb", buffering=0) as log:
            log.write(f"##OPENCODE_BEGIN_{sentinel}\n".encode())

            oc_bin = shutil.which("opencode")
            if oc_bin is None:
                _write_failed_result(args.result, args.agent_id, started_at,
                                     "opencode binary not found", **common)
                print("opencode-wrapper: opencode binary not found", file=sys.stderr)
                sys.exit(1)
            oc_basename = os.path.basename(oc_bin)
            if oc_basename != "opencode":
                _write_failed_result(args.result, args.agent_id, started_at,
                                     f"allowlist: expected 'opencode', got '{oc_basename}'",
                                     **common)
                print(f"opencode-wrapper: allowlist violation — {oc_basename}", file=sys.stderr)
                sys.exit(1)

            oc_argv = [oc_bin, "run", "--format", "json", "--auto", "-m", args.model]
            if args.effort:
                oc_argv.extend(["--variant", args.effort])
            if args.resume:
                oc_argv.extend(["--session", pointer_id])
            if not PROMPT_VIA_STDIN:
                oc_argv.extend(["--", prompt_text])

            child_env = os.environ.copy()
            for var in ("SSH_CLIENT", "SSH_CONNECTION", "SSH_TTY"):
                child_env.pop(var, None)
            if "DBUS_SESSION_BUS_ADDRESS" not in child_env:
                uid_bus = f"/run/user/{os.getuid()}/bus"
                if os.path.exists(uid_bus):
                    child_env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={uid_bus}"

            try:
                child = subprocess.Popen(
                    oc_argv,
                    stdin=subprocess.PIPE if PROMPT_VIA_STDIN else subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=0,
                    env=child_env,
                )
            except Exception as e:
                _write_failed_result(args.result, args.agent_id, started_at,
                                     f"Popen failed: {e}", **common)
                print(f"opencode-wrapper: Popen failed: {e}", file=sys.stderr)
                sys.exit(1)

            if PROMPT_VIA_STDIN:
                threading.Thread(target=_feed_stdin, args=(child.stdin, prompt_bytes),
                                 daemon=True).start()

            pending = b""
            try:
                while True:
                    chunk = child.stdout.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    log.write(chunk)
                    captured.extend(chunk)
                    # Fresh run: persist the session id as soon as it is seen,
                    # so a killed run can still be resumed.
                    if (not args.resume and persisted_id is None
                            and not pointer_write_failed):
                        pending += chunk
                        lines = pending.split(b"\n")
                        pending = lines.pop()
                        if len(pending) > 1048576:
                            pending = b""
                        for ln in lines:
                            sid = _session_id_from_line(ln)
                            if sid:
                                if _write_pointer(args.session, sid):
                                    persisted_id = sid
                                else:
                                    pointer_write_failed = True
                                break
            except Exception as e:
                print(f"opencode-wrapper: stream error: {e}", file=sys.stderr)

            child.wait()
            ended_at = time.time()
            log.write(f"##OPENCODE_END_{sentinel}\n".encode())
            log.flush()
    except OSError as e:
        _write_failed_result(args.result, args.agent_id, started_at,
                             f"log write failed: {e}", **common)
        print(f"opencode-wrapper: {e}", file=sys.stderr)
        sys.exit(1)

    analysis = analyze_output(bytes(captured))
    duration_s = ended_at - started_at
    returncode = child.returncode
    exit_code = returncode
    exit_signal = None
    if returncode < 0:
        exit_signal = -returncode
        exit_code = None

    observed = analysis["session_ids"]
    first_id = observed[0] if observed else None
    strict_error = None
    error_kind = None
    continued = False
    session_id = first_id

    if args.resume:
        if first_id is not None and first_id != pointer_id:
            strict_error = (
                "opencode-wrapper: resume_rejected (opencode used session %s, "
                "expected %s; spawn a fresh run, do not retry resume)"
                % (first_id, pointer_id))
            error_kind = "resume_rejected"
        else:
            session_id = pointer_id
            continued = first_id == pointer_id
    else:
        if first_id and persisted_id != first_id and not pointer_write_failed:
            if _write_pointer(args.session, first_id):
                persisted_id = first_id
            else:
                pointer_write_failed = True
        if pointer_write_failed:
            strict_error = "opencode-wrapper: session pointer persistence failed"
            error_kind = "pointer_persist_failed"
        if not first_id:
            print("opencode-wrapper: warning: no session id in opencode output; "
                  "this run cannot be resumed", file=sys.stderr)

    no_events = analysis["event_count"] == 0
    if (returncode == 0 and not analysis["errors"] and not no_events
            and not strict_error):
        final_hint = "completed"
    else:
        final_hint = "failed"
        if exit_signal is None and exit_code == 0:
            exit_code = 1  # opencode exited 0 but reported an error / nothing
            if no_events and not analysis["errors"] and not strict_error:
                error_kind = "no_events"

    if strict_error:
        print(strict_error, file=sys.stderr)
        exit_code = 3
        exit_signal = None
        final_hint = "failed"

    partial_text = analysis["all_text"] if analysis["all_text"].strip() else None
    partial_path = None
    if final_hint == "failed" and partial_text and not strict_error:
        final_hint = "partial"
        partial_path = _write_partial_md(result_dir, args.agent_id, log_path, partial_text)

    infra_hint = None
    if final_hint == "failed" and not strict_error:
        infra_hint = classify_infra(analysis, duration_s)

    error = None
    if final_hint != "completed":
        if strict_error:
            error = strict_error
        elif analysis["errors"]:
            error = "; ".join(analysis["errors"])[:2000]
        elif analysis["stderr_lines"]:
            error = "\n".join(analysis["stderr_lines"][-20:])[:2000]
        elif exit_signal is not None:
            error = "opencode killed by signal %d" % exit_signal
        elif error_kind == "no_events":
            error = "opencode exited 0 without printing any JSON event"
        else:
            error = "opencode exited with code %s and no error event" % returncode

    result = _base_result(args.agent_id, args.session, started_at, ended_at,
                          args.task, log_path, args.effort)
    result.update({
        "exit_code": exit_code,
        "exit_signal": exit_signal,
        "final_state_hint": final_hint,
        "conversation_id": session_id,
        "session_id": session_id,
        "session_continued": bool(continued and final_hint == "completed"),
        "result": analysis["final_text"] if final_hint == "completed" else None,
        "usage": analysis["usage"],
        "infra_hint": infra_hint,
        "event_count": analysis["event_count"],
    })
    if error is not None:
        result["error"] = error
    if error_kind:
        result["error_kind"] = error_kind
    if final_hint == "partial":
        result["result_partial"] = partial_text[:_PARTIAL_CAP]
        result["partial_reason"] = "run failed after opencode produced text"
        result["partial_path"] = partial_path

    _write_result_atomic(args.result, result)

    if strict_error:
        sys.exit(3)
    if exit_signal is not None:
        sys.exit(128 + exit_signal)
    sys.exit(0 if exit_code == 0 else (exit_code or 1))


if __name__ == "__main__":
    main()