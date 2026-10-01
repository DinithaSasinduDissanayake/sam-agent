#!/usr/bin/env python3
"""SAM harness module — native dual-harness interface (pi + agy).

Approved refactor: one ``Harness`` interface with ``spawn`` / ``poll`` /
``result`` / ``logs`` / ``resume`` (``resume_session``) / ``activity``
methods, plus ``PiHarness`` and ``AgyHarness`` implementations.

Ownership after this refactor:

- Pi session logic lives here (``PiHarness``): session.jsonl tail parsing
  is reused from ``sam.activity.session_stats`` (kept in place for
  backward-compat imports) and the wrapper path mapping
  (``pi`` -> ``pi-wrapper``) comes from ``sam.config.HARNESS_WRAPPERS``.
- Agy session logic lives here (``AgyHarness``): the conversation_id
  pointer-file model plus the JSON-envelope extraction previously embedded
  in ``wrapper/agy_wrapper.py`` (``_read_conversation_id`` /
  ``_extract_conversation_id`` helpers, duplicated here import-free so this
  module stays stdlib-only).
- ``sam.activity.compute_agent_activity`` dispatches to
  ``harness.activity()``; ``sam.commands.restart`` / ``resume`` delegate
  session-path decisions to ``harness.resume_session()``:
  agy preserves the conversation_id pointer, pi uses a fresh
  ``run-NNN/session.jsonl`` on restart and preserves the session file on
  resume (pi CLI behavior unchanged).

Unified ``result.json`` schema (both harnesses, both success and failure)::

    {agent_id, harness, exit_code, exit_signal, final_state_hint,
     duration_ms, wrapper_version, conversation_id, session_path, result}

``result`` carries wrapper-captured output text when available (else None);
pi sets ``conversation_id`` to None. Wrappers additionally keep the legacy
``started_at`` / ``ended_at`` / ``output_path`` / ``task_path`` keys so
existing readers are unaffected.
"""

import json
import os
import re
import time
from pathlib import Path

WRAPPER_VERSION = "0.1.0"

# Unified result.json keys both wrappers must emit.
RESULT_FIELDS = (
    "agent_id",
    "harness",
    "exit_code",
    "exit_signal",
    "final_state_hint",
    "duration_ms",
    "wrapper_version",
    "conversation_id",
    "session_path",
    "result",
)

# Legacy keys wrappers keep emitting for backward compatibility.
LEGACY_RESULT_FIELDS = ("started_at", "ended_at", "output_path", "task_path")

_SENTINEL_RE = re.compile(r"^##(PI|AGY)_(BEGIN|END)_[a-f0-9]+$")


def build_result(agent_id, harness, exit_code=None, exit_signal=None,
                 final_state_hint="completed", duration_ms=0,
                 conversation_id=None, session_path=None, result=None,
                 started_at=None, ended_at=None, output_path=None,
                 task_path=None, error=None):
    """Build a unified result.json dict (success or failure).

    Always contains every key in RESULT_FIELDS; legacy keys are included
    for existing readers. ``error`` (when given) is attached as an extra.
    """
    data = {
        "agent_id": agent_id,
        "harness": harness,
        "exit_code": exit_code,
        "exit_signal": exit_signal,
        "final_state_hint": final_state_hint,
        "duration_ms": duration_ms,
        "wrapper_version": WRAPPER_VERSION,
        "conversation_id": conversation_id,
        "session_path": session_path,
        "result": result,
    }
    data["started_at"] = started_at
    data["ended_at"] = ended_at
    data["output_path"] = output_path
    data["task_path"] = task_path
    if error is not None:
        data["error"] = error
    return data


def read_conversation_id(session_path):
    """Read conversation_id from an agy pointer file; None when absent."""
    try:
        if not session_path or not os.path.isfile(session_path):
            return None
        with open(session_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read().strip()
            return text if re.fullmatch(r"[A-Za-z0-9_-]+", text) else None
    except OSError:
        return None


def extract_conversation_id(raw):
    """Extract conversation_id from agy's JSON envelope output.

    Mirrors wrapper/agy_wrapper.py so resume decisions can be made without
    importing the wrapper. Checks whole-output JSON first, then JSON-lines
    in reverse (last envelope wins).
    """
    if not raw:
        return None
    def from_obj(obj):
        if isinstance(obj, dict):
            # Only the documented terminal stream event may nest an envelope.
            if obj.get("event") == "result":
                obj = obj.get("result")
            elif "event" in obj:
                return None
            if isinstance(obj, dict):
                if "status" not in obj:
                    return None
                v = obj.get("conversation_id")
                if isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_-]+", v):
                    return v
        return None

    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    try:
        found = from_obj(json.loads(text))
        if found:
            return found
    except (ValueError, TypeError):
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
            found = from_obj(obj)
        except (ValueError, TypeError):
            continue
        if found:
            return found
        if isinstance(obj, dict) and ("status" in obj or obj.get("event") == "result"):
            return None  # A malformed terminal envelope must not reuse an earlier ID.
    return None


def extract_pi_run_result(session_path, started_at, ended_at):
    """Recover old active-branch final only within authoritative run timestamps."""
    from sam.run_times import parse_timestamp
    start, end = parse_timestamp(started_at), parse_timestamp(ended_at)
    if start is None or end is None or end < start:
        return None
    try:
        with open(session_path, encoding="utf-8") as f:
            entries = [json.loads(line) for line in f if line.strip()]
        if any(not isinstance(e, dict) for e in entries):
            return None
        entries = [e for e in entries if e.get("type") != "session"]
        if not entries:
            return None
        leaf = entries[-1]
        branch = []
        if isinstance(leaf.get("id"), str) and "parentId" in leaf:
            by_id = {e["id"]: e for e in entries if isinstance(e.get("id"), str)}
            seen = set()
            while leaf is not None:
                if leaf.get("id") in seen:
                    return None
                seen.add(leaf.get("id"))
                branch.append(leaf)
                parent = leaf.get("parentId")
                leaf = by_id.get(parent) if isinstance(parent, str) else None
        else:
            branch = reversed(entries)
        for entry in branch:
            msg = entry.get("message")
            if not isinstance(msg, dict) or msg.get("role") not in ("assistant", "user", "toolResult"):
                continue
            stamp = msg.get("timestamp", entry.get("timestamp"))
            # Pi message timestamps are epoch milliseconds; entries use ISO.
            if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
                stamp /= 1000
            dt = parse_timestamp(stamp)
            if dt is None or not start <= dt <= end:
                return None
            if msg.get("role") != "assistant" or msg.get("stopReason") != "stop":
                return None
            blocks = msg.get("content")
            if not isinstance(blocks, list) or any(
                    isinstance(b, dict) and b.get("type") == "toolCall" for b in blocks):
                return None
            text = "\n".join(b["text"] for b in blocks if isinstance(b, dict)
                             and b.get("type") == "text" and isinstance(b.get("text"), str))
            return text if text.strip() else None
    except (OSError, ValueError, TypeError):
        pass
    return None


class Harness:
    """Abstract harness interface."""

    name = "base"
    sentinel_tag = "PI"
    session_kind = "file"

    def build_argv(self, wrapper, agent_id, model, session_path,
                   task_path, result_path, thinking=None, effort=None, resume=False):
        raise NotImplementedError

    def spawn(self, wrapper, agent_id, model, session_path, task_path,
              result_path, cwd, env, thinking=None, effort=None):
        """Launch the wrapper detached (new session). Returns Popen."""
        from sam import util as sam_util  # lazy: avoid import cycles
        return sam_util.launch_wrapper(
            wrapper, agent_id, model, session_path, task_path,
            result_path, cwd, env, harness=self.name,
            thinking=thinking, effort=effort)

    def poll(self, agent, run_id=None):
        """Resolve lifecycle state for a registry entry (read-only)."""
        from sam import state as sam_state  # lazy
        return sam_state.resolve_agent_state(
            agent, run_id if run_id is not None else agent.get("run_id", 1))

    def result(self, result_path):
        """Read unified result.json; None when absent/unparseable."""
        try:
            if result_path and os.path.exists(result_path):
                with open(result_path, encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            pass
        return None

    def logs(self, log_path, n=50, raw=False):
        """Tail log file; strips sentinel markers unless raw=True."""
        with open(log_path, "r", errors="replace") as f:
            lines = f.readlines()
        if n and n > 0:
            lines = lines[-n:]
        if raw:
            return lines
        return [ln for ln in lines
                if not _SENTINEL_RE.match(ln.rstrip("\n\r"))]

    def resume_session(self, prev_session_path, new_run_dir, for_resume=False):
        """Session path for a restart/resume into new_run_dir."""
        raise NotImplementedError

    def activity(self, agent, lifecycle_state, stall_seconds=300,
                 watch=None, max_bytes=256 * 1024, now=None, sleep_fn=None):
        """Full activity block for one agent. Read-only, never raises."""
        raise NotImplementedError


class PiHarness(Harness):
    """Pi harness: session.jsonl tail + output.log sentinels.

    Pi logic moved here from sam/activity.py (session_stats tail parsing)
    and the sam.config wrapper mapping (pi -> pi-wrapper). The activity
    helpers stay importable from sam.activity for backward compatibility;
    this class delegates to them so pi CLI output is byte-identical.
    """

    name = "pi"
    sentinel_tag = "PI"
    session_kind = "jsonl"

    def build_argv(self, wrapper, agent_id, model, session_path,
                   task_path, result_path, thinking=None, effort=None, resume=False):
        if effort:
            raise ValueError("--effort requires harness 'agy'")
        argv = [str(wrapper), "--agent-id", agent_id, "--model", model,
                "--session", str(session_path), "--task", str(task_path),
                "--result", str(result_path)]
        if thinking:
            argv.extend(["--thinking", thinking])
        return argv

    def resume_session(self, prev_session_path, new_run_dir, for_resume=False):
        if for_resume and prev_session_path:
            return str(prev_session_path)  # pi resume continues session file
        return str(Path(new_run_dir) / "session.jsonl")  # restart: fresh

    def activity(self, agent, lifecycle_state, stall_seconds=300,
                 watch=None, max_bytes=256 * 1024, now=None, sleep_fn=None):
        from sam import activity as act  # lazy: activity dispatches here
        now = time.time() if now is None else now
        session = act.session_stats(agent.get("session_path"), now=now,
                                    max_bytes=max_bytes)
        log = act.log_stats(agent.get("log_path"), now=now,
                            max_bytes=max_bytes)
        cls = act.classify(agent, lifecycle_state, session, log, now=now,
                           stall_seconds=stall_seconds)
        out = {"lifecycle_state": lifecycle_state,
               "activity_state": cls["activity_state"],
               "evidence": cls["evidence"],
               "session": session, "log": log}
        if watch is not None:
            interval = act.clamp_watch_seconds(watch)
            deltas = act.watch_deltas(
                {"session": agent.get("session_path"),
                 "log": agent.get("log_path")}, interval, sleep_fn=sleep_fn)
            out["watch"] = {
                "interval_seconds": interval,
                "note": "two-sample byte delta; growth_bytes is None when "
                        "not measurable (missing/replaced/shrunk/error)",
                "session": deltas["session"], "log": deltas["log"]}
        return out


class AgyHarness(Harness):
    """Agy harness: conversation_id pointer file + JSON envelope.

    Agy logic moved here from wrapper/agy_wrapper.py (pointer read/write,
    envelope extraction). The session "file" is a tiny pointer holding the
    conversation_id; resume always preserves it so the conversation
    continues. Usage/token signals are unavailable (None) — only pointer
    presence, log sentinels, and watch deltas are reported.
    """

    name = "agy"
    sentinel_tag = "AGY"
    session_kind = "pointer"

    def build_argv(self, wrapper, agent_id, model, session_path,
                   task_path, result_path, thinking=None, effort=None, resume=False):
        if thinking:
            raise ValueError(
                "--thinking cannot be used with harness 'agy'; use --effort")
        argv = [str(wrapper), "--agent-id", agent_id, "--model", model,
                "--session", str(session_path), "--task", str(task_path),
                "--result", str(result_path)]
        if effort:
            argv.extend(["--effort", effort])
        if resume:
            argv.append("--resume")
        return argv

    def resume_session(self, prev_session_path, new_run_dir, for_resume=False):
        # Agy always preserves the conversation_id pointer.
        if prev_session_path:
            return str(prev_session_path)
        return str(Path(new_run_dir) / "session.jsonl")

    def activity(self, agent, lifecycle_state, stall_seconds=300,
                 watch=None, max_bytes=256 * 1024, now=None, sleep_fn=None):
        from sam import activity as act  # lazy
        now = time.time() if now is None else now
        spath = agent.get("session_path")
        conversation_id = read_conversation_id(spath)
        session = {
            "path": None if spath is None else str(spath),
            "exists": False, "size": 0, "mtime_age": None,
            "harness": "agy", "session_kind": "pointer",
            "conversation_id": conversation_id,
            "last_event_at": None, "last_event_age": None,
            "tool_pending": False, "pending_tool_call_ids": [],
            "recent_event_count_5s": 0, "recent_event_count_30s": 0,
            "recent_event_bytes_5s": 0, "recent_event_bytes_30s": 0,
            "usage_tokens_total": None, "usage_tokens_5s": None,
            "usage_tokens_30s": None,
            "usage_thinking_tokens_total": None,
            "usage_thinking_tokens_5s": None,
            "usage_thinking_tokens_30s": None,
            "estimated_tokens_5s": None, "estimated_tokens_30s": None,
            "parse_errors": 0, "truncated": False,
            "token_note": "agy harness has no per-message usage stream; "
                          "token fields stay None.",
        }
        if spath is not None:
            try:
                st = os.stat(spath)
                session["exists"] = True
                session["size"] = st.st_size
                session["mtime_age"] = max(0.0, now - st.st_mtime)
            except OSError as e:
                session["error"] = "stat failed: %s" % e
        else:
            session["error"] = "no session_path in registry"
        log = act.log_stats(agent.get("log_path"), now=now,
                            max_bytes=max_bytes)
        envelope = act.agy_envelope_stats(agent.get("log_path"), now=now,
                                          max_bytes=max_bytes)
        cls = act.classify(agent, lifecycle_state, session, log, now=now,
                           stall_seconds=stall_seconds)
        out = {"lifecycle_state": lifecycle_state,
               "activity_state": cls["activity_state"],
               "evidence": cls["evidence"],
               "session": session, "log": log, "envelope": envelope}
        if watch is not None:
            interval = act.clamp_watch_seconds(watch)
            deltas = act.watch_deltas(
                {"session": agent.get("session_path"),
                 "log": agent.get("log_path")}, interval, sleep_fn=sleep_fn)
            out["watch"] = {
                "interval_seconds": interval,
                "note": "two-sample byte delta; growth_bytes is None when "
                        "not measurable (missing/replaced/shrunk/error)",
                "session": deltas["session"], "log": deltas["log"]}
        return out


_HARNESSES = {"pi": PiHarness(), "agy": AgyHarness()}


def resolve_harness(entry):
    """Harness for a registry entry, with backfill for pre-harness rows.

    Old entries lack the ``harness`` field; those with a conversation_id
    are agy, everything else defaults to pi. Read-only fallback — callers
    persist it only via their normal resolved write-back path.
    """
    entry = entry or {}
    if entry.get("harness"):
        return entry["harness"]
    if entry.get("conversation_id"):
        return "agy"
    return "pi"


def get_harness(name=None):
    """Return the harness instance for 'pi' (default) or 'agy'."""
    key = name or "pi"
    try:
        return _HARNESSES[key]
    except KeyError:
        raise ValueError(
            "unknown harness %r, expected one of %s" % (name, sorted(_HARNESSES)))
