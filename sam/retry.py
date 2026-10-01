#!/usr/bin/env python3
"""SAM retry core: infra-failure detection, SAM-owned retry queue, breaker.

Contracts (IISA failure review, rounds 1–4):
- SAM owns *when* (schedule, backoff, breaker); the parent owns *what and
  whether* (enqueue intent via task, cancel, prioritize).
- "Resets in" strings are weather, not rails: parsed as an advisory
  backoff floor (capped), never a hard gate — every gate has an
  override-reason escape.
- One clock: queued items carry `not_before`; the spawn breaker consults
  the same queue. Fresh work is delayed during an active window; queued
  infra-retries are never delayed by the breaker.
- Infra-retry = 429 RESOURCE_EXHAUSTED, or a startup-network death
  (< 120 s, network-shaped error). Mid-flight network deaths with
  captured response become `partial` instead and never enqueue.
"""

import json
import os
import random
import time

from sam import config as sam_config

QUEUE_FILE = "retry_queue.json"

#: Parsed "Resets in" values above this are distrusted (weather cap).
MAX_RESET_ADVISORY_S = 1800
#: Backoff when the error text carries no parseable reset hint.
DEFAULT_BACKOFF_S = 300
#: Jitter added to every not_before (de-synchronize queued retries).
JITTER_S = 30
#: Startup-network deaths are infra; anything longer is mid-flight.
STARTUP_DEATH_S = 120

_NETWORK_MARKERS = ("EOF", "TLS handshake", "closed network connection",
                    "connection reset", "dial tcp", "context deadline exceeded")
_QUOTA_MARKERS = ("RESOURCE_EXHAUSTED", "Individual quota reached",
                  "error_code\":429", "code 429")


def queue_path():
    return sam_config.get_sam_home() / QUEUE_FILE


def load_queue():
    try:
        with open(queue_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except (FileNotFoundError, PermissionError, ValueError):
        pass
    return []


def save_queue(items):
    path = queue_path()
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=1)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as e:
        raise RuntimeError(f"retry queue write failed: {e}")


def parse_reset_seconds(text):
    """Parse 'Resets in 18m6s' / '21m23s' / '45s' hints. None when absent.

    Advisory only: callers must cap and jitter the result.
    """
    if not text:
        return None
    import re
    m = re.search(r"(\d+)\s*m(?:in)?\s*(\d+)\s*s", text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.search(r"(\d+)\s*m(?:in)?\b", text)
    if m:
        return int(m.group(1)) * 60
    m = re.search(r"Resets in (\d+)\s*s", text)
    if m:
        return int(m.group(1))
    return None


def detect_infra_failure(result, log_text=None):
    """Classify a terminal run's failure.

    Returns (kind, error_text) where kind is one of:
      "quota"          — 429 RESOURCE_EXHAUSTED (any duration)
      "startup-network" — network-shaped error within STARTUP_DEATH_S
      None             — not infrastructure (quality/operator/etc.)
    """
    if not isinstance(result, dict):
        return None, ""
    if result.get("final_state_hint") == "partial":
        return None, ""  # deliverables captured; parent decides
    exit_code = result.get("exit_code")
    if exit_code in (0, None) and result.get("exit_signal") is None:
        # clean exit or unknown — not an infra death
        if result.get("final_state_hint") != "failed":
            return None, ""
    err = str(result.get("error") or "")
    resp = str(result.get("result") or "")
    dur_ms = result.get("duration_ms")
    dur_s = (dur_ms / 1000.0) if isinstance(dur_ms, (int, float)) else None

    haystack = " ".join((err, resp, log_text or ""))
    if any(m in haystack for m in _QUOTA_MARKERS):
        return "quota", (err or log_text or "")
    if any(m in haystack for m in _NETWORK_MARKERS):
        if dur_s is None or dur_s <= STARTUP_DEATH_S:
            return "startup-network", (err or log_text or "")
    return None, ""


def compute_not_before(kind, error_text, now=None):
    """Advisory backoff: parsed reset (capped) or default, plus jitter."""
    now = time.time() if now is None else now
    if kind == "quota":
        reset = parse_reset_seconds(error_text)
        if reset is not None:
            backoff = min(reset, MAX_RESET_ADVISORY_S)
        else:
            backoff = DEFAULT_BACKOFF_S
    else:
        backoff = DEFAULT_BACKOFF_S
    backoff += random.randint(0, JITTER_S)
    return now + backoff


def enqueue(agent_id, name, model, kind, not_before,
            reset_advisory_s=None, reason=""):
    """Add/replace the queue item for agent_id. Returns the item."""
    items = [i for i in load_queue() if i.get("agent_id") != agent_id]
    item = {
        "agent_id": agent_id,
        "name": name,
        "model": model,
        "kind": kind,
        "enqueued_at": time.time(),
        "not_before": not_before,
        "reset_advisory_s": reset_advisory_s,
        "reason": reason,
        "attempts": 0,
    }
    items.append(item)
    save_queue(items)
    return item


def find_for(agent_id):
    for i in load_queue():
        if i.get("agent_id") == agent_id:
            return i
    return None


def find_for_name(name):
    for i in load_queue():
        if i.get("name") == name:
            return i
    return None


def remove(agent_id):
    items = load_queue()
    kept = [i for i in items if i.get("agent_id") != agent_id]
    if len(kept) != len(items):
        save_queue(kept)
        return True
    return False


def active_window(model, now=None):
    """Earliest future not_before for `model` (None when no window).

    The breaker only sees *future* windows: a due-but-unlaunched item
    does not block fresh spawns — its own retry is what rides the reset.
    """
    now = time.time() if now is None else now
    times = [i["not_before"] for i in load_queue()
             if isinstance(i.get("not_before"), (int, float))
             and i["not_before"] > now
             and (model is None or i.get("model") == model
                  or i.get("model") is None)]
    return min(times) if times else None


def due_items(now=None):
    now = time.time() if now is None else now
    return [i for i in load_queue()
            if isinstance(i.get("not_before"), (int, float))
            and i["not_before"] <= now]


def promote_if_infra(agent_entry, result_path=None, log_path=None):
    """Promote a failed run to awaiting_retry when it died of infra.

    Idempotent: returns the existing queue item when already queued.
    Returns (True, item) on promotion/already-queued, (False, None) when
    the failure is not infra.
    """
    if agent_entry.get("state") == "awaiting_retry":
        item = find_for(agent_entry.get("id"))
        return (item is not None), item
    rp = result_path or agent_entry.get("result_path")
    result = None
    try:
        if rp and os.path.exists(rp):
            with open(rp, "r", encoding="utf-8") as f:
                result = json.load(f)
    except (OSError, ValueError):
        return False, None
    log_text = None
    lp = log_path or agent_entry.get("log_path")
    try:
        if lp and os.path.exists(lp):
            # Only the tail: AGY_ERROR lines land near the end.
            with open(lp, "r", encoding="utf-8", errors="replace") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 8192))
                log_text = f.read()
    except OSError:
        pass
    kind, err_text = detect_infra_failure(result, log_text)
    if not kind:
        return False, None
    existing = find_for(agent_entry.get("id"))
    if existing:
        return True, existing
    not_before = compute_not_before(kind, err_text)
    reset_s = parse_reset_seconds(err_text)
    item = enqueue(
        agent_entry.get("id"),
        agent_entry.get("name"),
        agent_entry.get("model"),
        kind, not_before,
        reset_advisory_s=reset_s,
        reason=err_text[:200] or kind)
    return True, item
