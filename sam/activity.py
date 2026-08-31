#!/usr/bin/env python3
"""SAM activity module — conservative, read-only agent activity analysis.

Reads the tail of a pi session.jsonl and output.log to summarize *persisted*
activity. Never writes, never locks, never spawns. All lifecycle decisions
remain in sam/state.py; this module only adds a separate, opt-in activity
layer used by `sam status --detail` / `--watch`.

Terminology (deliberate and conservative):

- "recent_event_bytes" / "recent_event_count": bytes/count of persisted
  entries whose *entry timestamp* falls inside a window. This is NOT file
  growth — pi appends to the session file only at message boundaries,
  compaction/rewrites can shrink it, and a completed message may be written
  later than its timestamp suggests. True byte deltas require two file
  observations; see watch_deltas(), whose results are the only fields named
  "growth".

- "usage_tokens" / "usage_thinking_tokens": tokens attributed to COMPLETED
  assistant messages via usage.totalTokens / usage.reasoning. Cumulative per
  message, not streaming counters. Compaction usage is intentionally
  excluded (it is accounting metadata, not new generation tokens).

- "estimated_tokens": a rough chars/4 fallback for assistant entries that
  lack a usage block. Never summed into usage_tokens; the two measures stay
  in separate fields with a token_note explaining the difference.

Activity states (conservative):

  lifecycle passthrough: completed / failed / killed / spawning / unknown
  verified-live-PID only: tool_pending, active_recent_event,
                          waiting_or_idle, possibly_stalled

No live "thinking" / "producing_output" claim is made: the current wrapper
runs `pi --print` (text mode), which buffers all output until the end of the
run, so those phases are not observable from files.

Only a verified-live lifecycle state ("running", i.e. PID alive with
matching start time) may be classified as tool_pending /
active_recent_event / waiting_or_idle / possibly_stalled. A dead PID with no
result.json remains lifecycle "unknown" and is never relabeled.
"""

import json
import os
import re
import time
from datetime import datetime, timezone

from sam import state as sam_state


# ── Defaults ──────────────────────────────────────────────────────────────────

# Per-agent tail budget (bytes) for session/log parsing. Kept modest so
# `--detail` over many agents stays cheap; default `sam status` reads nothing.
DEFAULT_MAX_BYTES = 256 * 1024

# Recent-event window sizes (seconds).
WINDOW_5S = 5
WINDOW_30S = 30

# Classification thresholds (seconds).
DEFAULT_ACTIVE_WINDOW = 30
DEFAULT_STALL_SECONDS = 300

# Watch (two-sample delta) interval bounds.
WATCH_MIN = 1
WATCH_MAX = 30
WATCH_DEFAULT = 5

TOKEN_NOTE = (
    "usage_tokens come from completed-message usage.totalTokens "
    "(usage_thinking_tokens from usage.reasoning); estimated_tokens are a "
    "chars/4 fallback for messages without usage. The two measures are "
    "never combined."
)

_SENTINEL_RE = re.compile(r"^##PI_(BEGIN|END)_[a-f0-9]+$")


# ── Time helpers ──────────────────────────────────────────────────────────────

def _iso_to_epoch(value):
    """ISO 8601 string (Z suffix allowed) -> epoch seconds."""
    s = value
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).timestamp()


def _entry_epoch(entry):
    """Best-effort epoch seconds for a session entry; None when unparseable.

    Prefers the entry-level ISO timestamp, falls back to the epoch-ms
    message.timestamp used by pi message entries.
    """
    ts = entry.get("timestamp")
    if isinstance(ts, str):
        try:
            return _iso_to_epoch(ts)
        except (ValueError, TypeError):
            pass
    msg = entry.get("message")
    if isinstance(msg, dict):
        mts = msg.get("timestamp")
        if isinstance(mts, (int, float)):
            return float(mts) / 1000.0
    return None


# ── File reading ──────────────────────────────────────────────────────────────

def _read_tail(path, max_bytes):
    """Read the last max_bytes of a file. Returns (data_bytes, truncated).

    When reading from the middle of the file, the partial first line is
    dropped so every returned line is a complete line. Raises OSError on
    read failure; callers catch and record it.
    """
    max_bytes = max(1, int(max_bytes))
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        start = max(0, size - max_bytes)
        if start > 0:
            f.seek(start)
            data = f.read()
            nl = data.find(b"\n")
            if nl == -1:
                return b"", True
            return data[nl + 1:], True
        f.seek(0)
        return f.read(), False


# ── Session entry helpers ─────────────────────────────────────────────────────

def _content_blocks(message):
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        return [c for c in content if isinstance(c, dict)]
    return []


def _block_chars(block):
    """Character count of a text/thinking content block; 0 for other types."""
    if block.get("type") == "text" and isinstance(block.get("text"), str):
        return len(block["text"])
    if block.get("type") == "thinking" and isinstance(block.get("thinking"), str):
        return len(block["thinking"])
    return 0


def _entry_usage(entry):
    """usage dict for a COMPLETED assistant message, else None.

    Compaction usage is intentionally excluded (accounting metadata, not
    new generation tokens) to avoid double counting.
    """
    msg = entry.get("message")
    if isinstance(msg, dict) and msg.get("role") == "assistant":
        u = msg.get("usage")
        if isinstance(u, dict):
            return u
    return None


def _entry_estimated_tokens(entry):
    """Rough chars/4 estimate for assistant entries WITHOUT usage.

    Returns None when the entry has no usage AND no text/thinking content,
    or when it has a usage block (those are counted as usage_tokens instead).
    """
    msg = entry.get("message")
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return None
    if isinstance(msg.get("usage"), dict):
        return None
    chars = sum(_block_chars(b) for b in _content_blocks(msg))
    if chars == 0:
        return None
    return (chars + 3) // 4


def _pending_tool_state(parsed):
    """Return (pending_ids, resolved_ids) for tool calls in the tail.

    Conservative rule: only toolCall ids from the LAST assistant entry that
    contains toolCalls are considered; any later toolResult that matches an
    id clears it. An assistant entry without toolCalls resets the pending
    set (the agent moved on), so an aborted old call cannot be mistaken for
    an in-flight tool.
    """
    pending = []
    resolved = set()
    for _epoch, entry, _raw in parsed:
        msg = entry.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant":
            calls = [b for b in _content_blocks(msg)
                     if b.get("type") == "toolCall"]
            pending = [b.get("id") for b in calls if b.get("id")]
        elif role == "toolResult":
            tid = msg.get("toolCallId")
            if tid in pending:
                resolved.add(tid)
    return [tid for tid in pending if tid not in resolved], sorted(resolved)


# ── Public analysis functions ─────────────────────────────────────────────────

def session_stats(session_path, now=None, max_bytes=DEFAULT_MAX_BYTES):
    """Analyze the tail of a pi session.jsonl file. Read-only, never raises.

    Returns a dict with conservative, clearly-labeled fields:
      exists / size / mtime_age, last_event_* (persisted tail entry),
      tool_pending + pending_tool_call_ids, recent_event_count_5s/30s,
      recent_event_bytes_5s/30s, usage_tokens_* / usage_thinking_tokens_*
      (completed-message usage), estimated_tokens_5s/30s (chars/4 fallback,
      separate), parse_errors, truncated, token_note.
    On failure the dict is still well-formed and carries an "error" key.
    """
    now = time.time() if now is None else now
    out = {
        "path": None if session_path is None else str(session_path),
        "exists": False,
        "size": 0,
        "mtime_age": None,
        "last_event_at": None,
        "last_event_age": None,
        "last_event_role": None,
        "last_event_stop_reason": None,
        "last_event_tool_names": [],
        "last_event_content_types": [],
        "tool_pending": False,
        "pending_tool_call_ids": [],
        "recent_event_count_5s": 0,
        "recent_event_count_30s": 0,
        "recent_event_bytes_5s": 0,
        "recent_event_bytes_30s": 0,
        "usage_tokens_total": None,
        "usage_tokens_5s": None,
        "usage_tokens_30s": None,
        "usage_thinking_tokens_total": None,
        "usage_thinking_tokens_5s": None,
        "usage_thinking_tokens_30s": None,
        "estimated_tokens_5s": None,
        "estimated_tokens_30s": None,
        "parse_errors": 0,
        "truncated": False,
        "token_note": TOKEN_NOTE,
    }
    if session_path is None:
        out["error"] = "no session_path in registry"
        return out
    try:
        st = os.stat(session_path)
    except OSError as e:
        out["error"] = "stat failed: %s" % e
        return out
    out["exists"] = True
    out["size"] = st.st_size
    out["mtime_age"] = max(0.0, now - st.st_mtime)
    try:
        data, truncated = _read_tail(session_path, max_bytes)
    except OSError as e:
        out["error"] = "read failed: %s" % e
        return out
    out["truncated"] = truncated

    parsed = []  # (epoch, entry, raw_line_bytes)
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            out["parse_errors"] += 1
            continue
        if not isinstance(entry, dict):
            out["parse_errors"] += 1
            continue
        parsed.append((_entry_epoch(entry), entry, len(raw)))

    # Windows + tokens (single pass over the tail).
    count_5 = count_30 = 0
    bytes_5 = bytes_30 = 0
    usage_total = thinking_total = 0
    usage_5 = usage_30 = think_5 = think_30 = 0
    est_5 = est_30 = 0
    seen_usage = seen_usage_5 = seen_usage_30 = False
    seen_think_5 = seen_think_30 = False
    seen_est_5 = seen_est_30 = False
    for epoch, entry, raw_len in parsed:
        in_window = epoch is not None and epoch <= now
        if in_window:
            if epoch > now - WINDOW_30S:
                count_30 += 1
                bytes_30 += raw_len
            if epoch > now - WINDOW_5S:
                count_5 += 1
                bytes_5 += raw_len
        usage = _entry_usage(entry)
        if usage is not None:
            tt = usage.get("totalTokens")
            rn = usage.get("reasoning")
            if isinstance(tt, (int, float)):
                seen_usage = True
                usage_total += int(tt)
                if in_window and epoch > now - WINDOW_30S:
                    seen_usage_30 = True
                    usage_30 += int(tt)
                if in_window and epoch > now - WINDOW_5S:
                    seen_usage_5 = True
                    usage_5 += int(tt)
            if isinstance(rn, (int, float)):
                thinking_total += int(rn)
                if in_window and epoch > now - WINDOW_30S:
                    seen_think_30 = True
                    think_30 += int(rn)
                if in_window and epoch > now - WINDOW_5S:
                    seen_think_5 = True
                    think_5 += int(rn)
        else:
            est = _entry_estimated_tokens(entry)
            if est is not None and in_window:
                if epoch > now - WINDOW_30S:
                    seen_est_30 = True
                    est_30 += est
                if epoch > now - WINDOW_5S:
                    seen_est_5 = True
                    est_5 += est

    out["recent_event_count_5s"] = count_5
    out["recent_event_count_30s"] = count_30
    out["recent_event_bytes_5s"] = bytes_5
    out["recent_event_bytes_30s"] = bytes_30
    out["usage_tokens_total"] = usage_total if seen_usage else None
    out["usage_tokens_5s"] = usage_5 if seen_usage_5 else None
    out["usage_tokens_30s"] = usage_30 if seen_usage_30 else None
    out["usage_thinking_tokens_total"] = thinking_total if seen_usage else None
    out["usage_thinking_tokens_5s"] = think_5 if seen_think_5 else None
    out["usage_thinking_tokens_30s"] = think_30 if seen_think_30 else None
    out["estimated_tokens_5s"] = est_5 if seen_est_5 else None
    out["estimated_tokens_30s"] = est_30 if seen_est_30 else None

    # Last persisted entry (file order) + pending-tool state.
    if parsed:
        last_epoch, last_entry, _ = parsed[-1]
        out["last_event_at"] = last_entry.get("timestamp")
        if last_epoch is not None:
            out["last_event_age"] = max(0.0, now - last_epoch)
        msg = last_entry.get("message")
        if isinstance(msg, dict):
            out["last_event_role"] = msg.get("role")
            out["last_event_stop_reason"] = msg.get("stopReason")
            blocks = _content_blocks(msg)
            out["last_event_content_types"] = [
                b.get("type") for b in blocks if b.get("type")]
            out["last_event_tool_names"] = [
                b.get("name") for b in blocks
                if b.get("type") == "toolCall" and b.get("name")]
        pending, _resolved = _pending_tool_state(parsed)
        out["pending_tool_call_ids"] = pending
        out["tool_pending"] = bool(pending)

    return out


def log_stats(log_path, now=None, max_bytes=DEFAULT_MAX_BYTES):
    """Basic output.log stats: existence, size, mtime age, sentinel markers.

    Note: with the current buffered `pi --print` wrapper, output.log grows
    only at run start (BEGIN sentinel) and run end (final text + END
    sentinel), so mtime age here is a coarse signal, never claimed as live
    streaming. Read-only, never raises; carries "error" on failure.
    """
    now = time.time() if now is None else now
    out = {
        "path": None if log_path is None else str(log_path),
        "exists": False,
        "size": 0,
        "mtime_age": None,
        "began": False,
        "ended": False,
    }
    if log_path is None:
        out["error"] = "no log_path in registry"
        return out
    try:
        st = os.stat(log_path)
    except OSError as e:
        out["error"] = "stat failed: %s" % e
        return out
    out["exists"] = True
    out["size"] = st.st_size
    out["mtime_age"] = max(0.0, now - st.st_mtime)
    try:
        data, _truncated = _read_tail(log_path, max_bytes)
    except OSError as e:
        out["error"] = "read failed: %s" % e
        return out
    for raw in data.split(b"\n"):
        line = raw.strip()
        if not line:
            continue
        text = line.decode("utf-8", "replace")
        if _SENTINEL_RE.match(text):
            if text.startswith("##PI_BEGIN_"):
                out["began"] = True
            elif text.startswith("##PI_END_"):
                out["ended"] = True
    return out


def clamp_watch_seconds(seconds):
    """Clamp a watch interval to [WATCH_MIN, WATCH_MAX]. None -> default."""
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        return WATCH_DEFAULT
    if seconds < WATCH_MIN:
        return WATCH_MIN
    if seconds > WATCH_MAX:
        return WATCH_MAX
    return int(seconds)


def watch_deltas(files, interval, sleep_fn=None):
    """Two-sample byte deltas for a mapping {name: path}. Read-only.

    Stats each file, waits `interval` (clamped to [WATCH_MIN, WATCH_MAX]),
    stats again. Returns per name:
      {"status": "measured"|"missing"|"replaced"|"shrunk"|"error",
       "growth_bytes": int|None, "size_before": int|None,
       "size_after": int|None}
    growth_bytes is only set for "measured". A file replaced between samples
    (inode change) or shrunk yields None — never a misleading delta. No file
    descriptors are held open across the wait.
    """
    if sleep_fn is None:
        sleep_fn = time.sleep
    interval = clamp_watch_seconds(interval)

    before = {}
    for name, path in files.items():
        if path is None:
            before[name] = None
            continue
        try:
            st = os.stat(path)
            before[name] = (st.st_size, st.st_ino)
        except OSError:
            before[name] = None

    sleep_fn(interval)

    result = {}
    for name, path in files.items():
        if path is None:
            result[name] = {
                "status": "missing", "growth_bytes": None,
                "size_before": None, "size_after": None,
            }
            continue
        try:
            st = os.stat(path)
            after = (st.st_size, st.st_ino)
        except OSError:
            after = None
        b = before[name]
        if b is None and after is None:
            result[name] = {
                "status": "missing", "growth_bytes": None,
                "size_before": None, "size_after": None,
            }
        elif b is None or after is None:
            result[name] = {
                "status": "error", "growth_bytes": None,
                "size_before": b[0] if b else None,
                "size_after": after[0] if after else None,
            }
        elif after[1] != b[1]:
            result[name] = {
                "status": "replaced", "growth_bytes": None,
                "size_before": b[0], "size_after": after[0],
            }
        elif after[0] < b[0]:
            result[name] = {
                "status": "shrunk", "growth_bytes": None,
                "size_before": b[0], "size_after": after[0],
            }
        else:
            result[name] = {
                "status": "measured", "growth_bytes": after[0] - b[0],
                "size_before": b[0], "size_after": after[0],
            }
    return result


def _started_age(agent, now):
    """Age in seconds since the registry updated_at/created_at; None if bad."""
    val = agent.get("updated_at") or agent.get("created_at")
    if not isinstance(val, str):
        return None
    try:
        return max(0.0, now - _iso_to_epoch(val))
    except (ValueError, TypeError):
        return None


def _lifecycle_evidence(state):
    if state in ("completed", "failed"):
        return ["lifecycle %s (result.json final_state_hint)" % state]
    if state == "killed":
        return ["lifecycle killed (registry state)"]
    if state == "spawning":
        return ["lifecycle spawning (no pid yet)"]
    return ["lifecycle unknown (pid dead or identity mismatch, no result.json)"]


def classify(agent, lifecycle_state, session, log, now=None,
             stall_seconds=DEFAULT_STALL_SECONDS,
             active_window=DEFAULT_ACTIVE_WINDOW):
    """Map lifecycle + file signals to a conservative activity state.

    lifecycle_state is the existing resolve_agent_state() result. Only a
    verified-live lifecycle ("running" — PID alive with matching start
    time) may be classified as tool_pending / active_recent_event /
    waiting_or_idle / possibly_stalled. Every other lifecycle state is
    passed through unchanged, so a dead PID with no result.json stays
    "unknown" and is never relabeled as stalled.

    session/log are the dicts returned by session_stats() / log_stats().
    """
    now = time.time() if now is None else now
    if lifecycle_state in sam_state.TERMINAL_STATES or \
            lifecycle_state in ("spawning", "unknown"):
        return {
            "activity_state": lifecycle_state,
            "evidence": _lifecycle_evidence(lifecycle_state),
        }

    # lifecycle_state == "running" → verified live PID.
    evidence = ["pid alive with start-time match"]
    pending = session.get("pending_tool_call_ids") or []
    if session.get("tool_pending") and pending:
        evidence.append("unresolved persisted tool call(s): %s"
                        % ", ".join(pending))
        return {"activity_state": "tool_pending", "evidence": evidence}

    signals = []
    if session.get("exists") and session.get("last_event_age") is not None:
        signals.append(("session event", session["last_event_age"]))
    if log.get("exists") and log.get("mtime_age") is not None:
        signals.append(("log write", log["mtime_age"]))

    if signals:
        kind, age = min(signals, key=lambda x: x[1])
        evidence.append("most recent observable signal: %s %.0fs ago"
                        % (kind, age))
        if age <= active_window:
            return {"activity_state": "active_recent_event",
                    "evidence": evidence}
        if age <= stall_seconds:
            return {"activity_state": "waiting_or_idle",
                    "evidence": evidence}
        return {"activity_state": "possibly_stalled", "evidence": evidence}

    # No session and no log at all.
    started_age = _started_age(agent, now)
    if started_age is not None and started_age <= stall_seconds:
        evidence.append("no session/log files yet (started %.0fs ago)"
                        % started_age)
        return {"activity_state": "waiting_or_idle", "evidence": evidence}
    evidence.append("no session/log files despite start")
    return {"activity_state": "possibly_stalled", "evidence": evidence}


def compute_agent_activity(agent, lifecycle_state,
                           stall_seconds=DEFAULT_STALL_SECONDS,
                           watch=None, max_bytes=DEFAULT_MAX_BYTES,
                           now=None, sleep_fn=None):
    """Full opt-in activity block for one agent. Read-only, never raises.

    Returns:
      lifecycle_state, activity_state, evidence,
      session (session_stats), log (log_stats),
      watch ({interval_seconds, session delta, log delta}) when requested.
    """
    now = time.time() if now is None else now
    session = session_stats(agent.get("session_path"), now=now,
                            max_bytes=max_bytes)
    log = log_stats(agent.get("log_path"), now=now, max_bytes=max_bytes)
    cls = classify(agent, lifecycle_state, session, log, now=now,
                   stall_seconds=stall_seconds)
    out = {
        "lifecycle_state": lifecycle_state,
        "activity_state": cls["activity_state"],
        "evidence": cls["evidence"],
        "session": session,
        "log": log,
    }
    if watch is not None:
        interval = clamp_watch_seconds(watch)
        deltas = watch_deltas(
            {"session": agent.get("session_path"),
             "log": agent.get("log_path")},
            interval, sleep_fn=sleep_fn)
        out["watch"] = {
            "interval_seconds": interval,
            "note": "two-sample byte delta; growth_bytes is None when "
                    "not measurable (missing/replaced/shrunk/error)",
            "session": deltas["session"],
            "log": deltas["log"],
        }
    return out
