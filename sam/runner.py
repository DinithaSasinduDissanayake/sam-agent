#!/usr/bin/env python3
"""sam.runner - generic harness runner (replaces the per-harness wrapper scripts).

Started detached by ``sam spawn/resume/restart`` as::

    python <this file> --harness H --agent-id ID --model M --session S
                       --task T --result R [--thinking X | --effort X] [--resume]

It owns one run: starts the harness CLI through its adapter (sam/adapters),
streams output to ``output.log`` (next to result.json) between sentinel lines,
watches for hangs, and writes ``result.json`` atomically (unified SAM schema).

Lifetime contract (Windows): the runner joins a kill-on-close Job Object named
``sam-job-<runner pid>``. Everything the harness starts is inside that job, so
``sam kill`` (terminate job) and a dead runner both take the whole tree down.
A dead runner therefore ALWAYS kills its agent - no orphans burning quota.
On POSIX the runner is the process-group leader (started with a new session),
exactly like the legacy wrappers.

Exit codes: 0 completed; 3 strict resume failure; otherwise the harness exit
code (or 1).
"""

import argparse
import json
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from sam import plat  # noqa: E402
from sam.adapters import get_adapter, json_objects, valid_id  # noqa: E402

RUNNER_VERSION = "0.2.0"
CHUNK = 65536
PARTIAL_CAP = 65536

_QUOTA_MARKERS = ("RESOURCE_EXHAUSTED", "Individual quota reached", "quota",
                  "rate limit", "rate_limit", "HTTP 429", "code 429",
                  "error_code\":429", "overloaded", "HTTP 503", "HTTP 529")
_NETWORK_MARKERS = ("EOF", "TLS handshake", "closed network connection",
                    "connection reset", "dial tcp", "context deadline exceeded",
                    "Cannot connect to API", "Unable to connect",
                    "Connection error", "ECONNRESET", "ENOTFOUND", "ETIMEDOUT",
                    "getaddrinfo", "proxyconnect", "error sending request")
#: Not infra even though they look like quota/network: retrying cannot help.
_NOT_INFRA = ("FreeTierError", "invalid_refresh_token", "unauthorized",
              "Authentication required", "authentication failed")


def infra_hint(errors, duration_s, watchdog):
    """'quota' | 'startup-network' | None (same vocabulary as sam.retry)."""
    hay = " ".join(errors)
    if any(m.lower() in hay.lower() for m in _NOT_INFRA):
        return None
    if any(m.lower() in hay.lower() for m in _QUOTA_MARKERS):
        return "quota"
    if watchdog == "first_output":
        return "startup-network"
    if any(m in hay for m in _NETWORK_MARKERS) and duration_s <= 120:
        return "startup-network"
    return None


def read_pointer(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read().strip()
        return text if valid_id(text) else None
    except OSError:
        return None


def write_atomic(path, text):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        plat.replace(tmp, path)
        plat.fsync_dir(d)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _inside(child, parent):
    child = os.path.normcase(os.path.abspath(child))
    parent = os.path.normcase(os.path.abspath(parent))
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:
        return False


def _timeout(env_name, default):
    try:
        return float(os.environ.get(env_name, default))
    except (TypeError, ValueError):
        return float(default)


def _kill_child_tree(child):
    """Stop the harness and what it started (the runner itself survives)."""
    if plat.IS_WINDOWS:
        try:
            import psutil
            procs = psutil.Process(child.pid).children(recursive=True)
            for p in procs:
                try:
                    p.kill()
                except psutil.Error:
                    pass
        except Exception:
            pass
        try:
            child.kill()
        except OSError:
            pass
        return
    try:
        child.terminate()
        child.wait(5)
    except Exception:
        try:
            child.kill()
        except OSError:
            pass


def _die_with_parent():
    """Linux only (runs in the harness process right before exec): ask the kernel
    to SIGKILL the harness when the runner dies, so a dead runner never leaves a
    harness burning quota. (Windows gets the same guarantee from the Job Object.)"""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, int(signal.SIGKILL))  # PR_SET_PDEATHSIG
    except Exception:
        pass


def _tree_cpu(child):
    """CPU seconds used so far by the harness and its live descendants, or
    None when psutil is not installed (POSIX without psutil: output-only idle)."""
    try:
        import psutil
        root = psutil.Process(child.pid)
        total = 0.0
        for p in [root] + root.children(recursive=True):
            try:
                t = p.cpu_times()
                total += t.user + t.system
            except psutil.Error:
                pass
        return total
    except Exception:
        return None


def _load_env_file(argv):
    """``--env-file PATH`` (first argument, used by the WMI launch path): load
    the environment the spawner wanted us to have, then delete the file."""
    if len(argv) >= 2 and argv[0] == "--env-file":
        try:
            with open(argv[1], "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                os.environ.clear()
                os.environ.update({str(k): str(v) for k, v in data.items()})
        except (OSError, ValueError):
            pass
        try:
            os.unlink(argv[1])
        except OSError:
            pass
        return argv[2:]
    return argv


def main(argv=None):
    argv = _load_env_file(list(sys.argv[1:] if argv is None else argv))
    ap = argparse.ArgumentParser(prog="sam-runner")
    ap.add_argument("--harness", required=True)
    ap.add_argument("--agent-id", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--session", required=True)
    ap.add_argument("--task", required=True)
    ap.add_argument("--result", required=True)
    ap.add_argument("--thinking", default=None)
    ap.add_argument("--effort", default=None)
    ap.add_argument("--resume", action="store_true", default=False)
    args = ap.parse_args(argv)

    started_at = time.time()
    result_dir = os.path.dirname(os.path.abspath(args.result))
    log_path = os.path.join(result_dir, "output.log")
    cwd = os.getcwd()
    job = None
    if plat.IS_WINDOWS:
        from sam.plat import windows as win
        job = win.enter_own_job()   # keep the handle open until exit

    base = {"agent_id": args.agent_id, "harness": args.harness,
            "wrapper_version": RUNNER_VERSION, "runner": "generic",
            "host": plat.hostname(), "session_path": args.session,
            "output_path": log_path, "task_path": args.task,
            "started_at": started_at, "exit_signal": None,
            "job": bool(job) if plat.IS_WINDOWS else None}

    status_path = os.path.join(result_dir, "runner.json")

    def note_status(phase, **more):
        """Crash-safe breadcrumb: if the runner dies, this file still says how far it got."""
        data = {"phase": phase, "runner_pid": os.getpid(), "host": plat.hostname(),
                "harness": args.harness, "agent_id": args.agent_id,
                "started_at": started_at, "updated_at": time.time()}
        data.update(more)
        try:
            write_atomic(status_path, json.dumps(data))
        except Exception:
            pass

    note_status("starting")

    def finish(final_hint, exit_code, error=None, error_kind=None, code=None, **extra):
        note_status("finished", final_state_hint=final_hint)
        ended = time.time()
        data = dict(base)
        data.update({"exit_code": exit_code, "final_state_hint": final_hint,
                     "duration_ms": int((ended - started_at) * 1000),
                     "ended_at": ended, "conversation_id": None, "result": None,
                     "session_continued": False, "usage": None, "infra_hint": None})
        data.update(extra)
        if error is not None:
            data["error"] = error
        if error_kind is not None:
            data["error_kind"] = error_kind
        try:
            write_atomic(args.result, json.dumps(data, indent=2))
        except Exception as e:  # noqa
            print("sam-runner: cannot write result.json: %s" % e, file=sys.stderr)
        return code if code is not None else (0 if final_hint == "completed" else (exit_code or 1))

    try:
        adapter = get_adapter(args.harness)
    except ValueError as e:
        return finish("failed", 1, error=str(e), error_kind="unknown_harness")

    # ── validation (same rules as the legacy wrappers) ──
    if not os.path.isfile(args.task):
        return finish("failed", 1, error="task file not found: %s" % args.task)
    try:
        with open(args.task, "r", encoding="utf-8-sig", errors="replace") as f:
            prompt_text = f.read()
    except OSError as e:
        return finish("failed", 1, error="cannot read task file: %s" % e)
    if not prompt_text.strip():
        return finish("failed", 1, error="task file is empty")
    agent_root = os.path.dirname(result_dir)
    if not _inside(args.session, agent_root):
        return finish("failed", 1, error="security violation - session path outside agent dir")
    if adapter.prompt_via == "argv" and len(prompt_text) > adapter.max_argv_chars:
        return finish("failed", 1, error_kind="task_too_large",
                      error="task is %d characters; harness %s takes the prompt as one "
                            "command-line argument (limit %d). Shorten the task or put the "
                            "details in a file and tell the agent to read it."
                            % (len(prompt_text), adapter.name, adapter.max_argv_chars))

    reasoning = args.effort if adapter.reasoning == "effort" else args.thinking
    pointer_id = read_pointer(args.session) if adapter.session_kind == "pointer" else None
    if adapter.session_kind == "pointer":
        if args.resume and not pointer_id:
            return finish("failed", 3, code=3, error_kind="resume_rejected",
                          error="sam-runner: no session id, use spawn not resume")
        continuation = bool(pointer_id)
    else:
        if args.resume and not os.path.isfile(args.session):
            return finish("failed", 3, code=3, error_kind="resume_rejected",
                          error="sam-runner: session file not found, use spawn not resume")
        continuation = bool(args.resume)

    override = os.environ.get("SAM_%s_BIN" % adapter.name.upper())
    if override:
        # Explicit executable (tests, non-PATH installs). A .py file is run with Python.
        prefix = [sys.executable, override] if override.endswith(".py") else [override]
    elif plat.IS_WINDOWS:
        prefix = win.resolve_executable(adapter.executable)
    else:
        import shutil
        found = shutil.which(adapter.executable)
        prefix = [found] if found else None
    if not prefix:
        return finish("failed", 1, error="%s binary not found" % adapter.executable,
                      error_kind="binary_not_found")

    cmd = adapter.build_command(prefix, args.model, args.task, prompt_text,
                                args.session, pointer_id, reasoning, cwd)
    env = plat.fix_child_env(os.environ.copy(), cwd)
    for var in ("SSH_CLIENT", "SSH_CONNECTION", "SSH_TTY"):
        env.pop(var, None)

    sentinel = secrets.token_hex(4)
    captured = bytearray()
    state = {"last_output": time.time(), "any_output": False, "sniffed": None,
             "final_at": None}
    lingered = False
    os.makedirs(result_dir, exist_ok=True)
    try:
        log = open(log_path, "wb", buffering=0)
    except OSError as e:
        return finish("failed", 1, error="log open failed: %s" % e)
    watchdog = None
    try:
        log.write(("##%s_BEGIN_%s\n" % (adapter.tag, sentinel)).encode())
        stdin = open(args.task, "rb") if adapter.prompt_via == "stdin" else subprocess.DEVNULL
        try:
            child = subprocess.Popen(
                cmd, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                bufsize=0, env=env, cwd=cwd,
                creationflags=0x08000000 if plat.IS_WINDOWS else 0,
                preexec_fn=_die_with_parent if sys.platform == "linux" else None)
        except Exception as e:  # noqa
            log.write(("##%s_END_%s\n" % (adapter.tag, sentinel)).encode())
            return finish("failed", 1, error="Popen failed: %s" % e, error_kind="launch_failed")
        finally:
            if stdin is not subprocess.DEVNULL:
                stdin.close()

        def pump():
            pending = b""
            while True:
                try:
                    chunk = child.stdout.read(CHUNK)
                except (OSError, ValueError):
                    break
                if not chunk:
                    break
                try:
                    log.write(chunk)
                except OSError:
                    pass
                captured.extend(chunk)
                state["last_output"] = time.time()
                state["any_output"] = True
                pending += chunk
                *lines, pending = pending.split(b"\n")
                for obj in json_objects(b"\n".join(lines).decode("utf-8", "replace")):
                    try:
                        if adapter.is_final_event(obj):
                            state["final_at"] = time.time()
                        if adapter.session_kind != "pointer" or state["sniffed"]:
                            continue
                        sid = adapter.sniff_session_id(obj)
                    except Exception:
                        continue
                    if sid:
                        state["sniffed"] = sid
                        # Persist at once so a run that dies early is resumable.
                        if not continuation:
                            try:
                                write_atomic(args.session, sid + "\n")
                            except Exception:
                                pass

        note_status("running", child_pid=child.pid)
        t = threading.Thread(target=pump, daemon=True)
        t.start()
        first_s = _timeout("SAM_FIRST_OUTPUT_TIMEOUT_S", adapter.first_output_timeout_s)
        idle_s = _timeout("SAM_IDLE_TIMEOUT_S", adapter.idle_timeout_s)
        grace_s = _timeout("SAM_EXIT_GRACE_S", adapter.exit_grace_s)
        cpu_checked, cpu_seen = time.time(), None
        tick = time.time()
        while True:
            try:
                child.wait(1.0)
                break
            except subprocess.TimeoutExpired:
                pass
            now = time.time()
            if now - tick > 30:
                # The machine slept/hibernated (or was frozen): do not count that
                # time as silence. The harness gets a full timeout to recover.
                state["last_output"] = now
                if state["final_at"]:
                    state["final_at"] = now
            tick = now
            if state["final_at"] and now - state["final_at"] > grace_s:
                # Known hang-at-exit bugs (pi extensions, opencode): the work is
                # done and reported, the process just does not leave.
                lingered = True
                _kill_child_tree(child)
                try:
                    child.wait(15)
                except subprocess.TimeoutExpired:
                    pass
                break
            if now - cpu_checked >= 30:
                cpu_checked = now
                cpu = _tree_cpu(child)
                if cpu is not None and cpu_seen is not None and cpu - cpu_seen >= 1.0:
                    state["last_output"] = now      # busy (long tool run): not idle
                if cpu is not None:
                    cpu_seen = cpu
            silent = now - state["last_output"]
            if not state["any_output"] and first_s > 0 and silent > first_s:
                watchdog = "first_output"
            elif state["any_output"] and idle_s > 0 and silent > idle_s:
                watchdog = "idle"
            if watchdog:
                _kill_child_tree(child)
                try:
                    child.wait(15)
                except subprocess.TimeoutExpired:
                    pass
                break
        t.join(10)
        ended_at = time.time()
        log.write(("##%s_END_%s\n" % (adapter.tag, sentinel)).encode())
    finally:
        try:
            log.close()
        except OSError:
            pass

    returncode = child.returncode if child.returncode is not None else 1
    if lingered:
        returncode = 0     # finished its work; we only had to remove the process
    exit_signal = None
    if returncode < 0:           # POSIX: killed by a signal
        exit_signal = -returncode
    text = bytes(captured).decode("utf-8", "replace")
    try:
        parsed = adapter.parse(text, returncode)
    except Exception as e:  # noqa - a parser bug must still produce a result.json
        parsed = {"result": None, "session_id": None, "usage": None,
                  "errors": ["adapter parse error: %s" % e]}
    errors = list(parsed.get("errors") or [])
    result_text = parsed.get("result")
    new_id = parsed.get("session_id") or state["sniffed"]
    duration_s = ended_at - started_at

    error_kind = None
    strict = None
    session_id = pointer_id
    if adapter.session_kind == "pointer":
        if continuation:
            if not new_id or new_id != pointer_id:
                # agy silently starts a NEW conversation on a bad id (exit 0).
                strict = ("sam-runner: resume_rejected (%s did not continue session %s; "
                          "it reported %s - spawn a fresh run, do not retry resume)"
                          % (adapter.name, pointer_id, new_id or "no session id"))
                error_kind = "resume_rejected"
        elif new_id:
            session_id = new_id
            if read_pointer(args.session) != new_id:
                try:
                    write_atomic(args.session, new_id + "\n")
                except Exception:
                    strict = "sam-runner: session pointer persistence failed"
                    error_kind = "pointer_persist_failed"
    if watchdog:
        errors.insert(0, "watchdog: no %soutput for %ds - harness killed"
                      % ("" if watchdog == "first_output" else "new ",
                         first_s if watchdog == "first_output" else idle_s))
        error_kind = error_kind or "watchdog_" + watchdog

    exit_code = None if exit_signal is not None else returncode
    if strict:
        final, exit_code, exit_signal, code = "failed", 3, None, 3
        errors.insert(0, strict)
    elif returncode == 0 and not errors and result_text and not watchdog:
        final, code = "completed", 0
    else:
        final = "failed"
        if exit_signal is None and exit_code == 0:
            exit_code = 1          # exit 0 but an error / no final text
        if not errors:
            errors.append("%s exited with code %s and no final text"
                          % (adapter.name, returncode))
        code = (128 + exit_signal) if exit_signal is not None else (exit_code or 1)

    extra = {"conversation_id": session_id if adapter.session_kind == "pointer" else None,
             "usage": parsed.get("usage"), "exit_signal": exit_signal,
             "session_continued": bool(continuation and final == "completed")}
    if final == "completed":
        extra["result"] = result_text
    else:
        extra["infra_hint"] = None if strict else infra_hint(errors, duration_s, watchdog)
        if result_text and not strict:
            # Failed run that still produced text: partial, parent must verify.
            final = "partial"
            body = result_text[:PARTIAL_CAP]
            ppath = os.path.join(result_dir, "PARTIAL.md")
            try:
                with open(ppath, "w", encoding="utf-8") as f:
                    f.write("# PARTIAL run - verify before respawn\n\n- agent: %s\n"
                            "- workspace: %s\n- log: %s\n- error: %s\n\n"
                            "Parent contract: read this file AND the workspace files it "
                            "names before respawning.\n\n## Captured response\n\n%s\n"
                            % (args.agent_id, cwd, log_path, errors[0][:300], body))
                extra["partial_path"] = ppath
            except OSError:
                extra["partial_path"] = None
            extra["result_partial"] = body
            extra["partial_reason"] = "run failed but the harness produced text"
    if lingered:
        extra["lingered_after_final_event"] = True
    rc = finish(final, exit_code,
                error="; ".join(errors)[:2000] if errors else None,
                error_kind=error_kind, code=code, **extra)
    if watchdog and not plat.IS_WINDOWS and os.getpgrp() == os.getpid():
        # Take down anything the harness left in our process group.
        try:
            os.killpg(os.getpgrp(), signal.SIGKILL)
        except OSError:
            pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
