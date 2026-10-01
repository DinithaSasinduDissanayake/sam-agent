#!/usr/bin/env python3
"""SAM retry — Fire / cancel / list SAM-owned infra-retries.

IISA review item 4 (with the spawn breaker): SAM owns *when* — the queue
carries `not_before` (parsed 429 reset as advisory backoff + jitter, or
a default backoff for startup-network deaths). The operator owns
*what/whether* — cancel, override, or re-enqueue only.

  sam retry                    # list the queue
  sam retry NAME               # fire a due queued retry (relaunches task)
  sam retry --due              # fire every due item
  sam retry NAME --cancel      # dequeue; agent -> killed (retry_cancelled)
  sam retry NAME --override-reason TEXT   # fire before not_before (logged)

The fire path is resume with `_infra_retry=True`: same agent, new run,
bypasses the operator restart budget, resumes the conversation when an
agy pointer exists, else starts a fresh conversation on the same task.
The breaker never gates this path — not_before above is the only clock.
"""

import io
import json
import sys
import time
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_SAM_PKG = _THIS_DIR.parent
if str(_SAM_PKG) not in sys.path:
    sys.path.insert(0, str(_SAM_PKG))

from sam import locks as sam_locks
from sam import registry as sam_registry
from sam import retry as sam_retry
from sam.commands import resume as sam_resume


def _emit_error(code, message, as_json):
    if as_json:
        print(json.dumps({"status": "error", "code": code, "message": message}),
              file=sys.stderr)
    else:
        print(f"sam: {message}", file=sys.stderr)
    return code


def _fmt_ts(ts):
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OSError):
        return "?"


def _resolve_agent(ref):
    reg = sam_registry.load_registry()
    return sam_registry.resolve_ref(reg.get("agents", []), ref)


def _clear_queue_fields(agent_id):
    try:
        with sam_locks.registry_lock(exclusive=True, timeout=10):
            reg = sam_registry.load_registry()
            for a in reg.get("agents", []):
                if a.get("id") == agent_id:
                    a.pop("retry_not_before", None)
                    a.pop("retry_kind", None)
                    a["updated_at"] = datetime.now(timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ")
                    break
            sam_registry.save_registry(reg)
    except Exception:
        pass


def _fire(item, as_json, override_reason=None):
    agent_id = item.get("agent_id")
    agent = _resolve_agent(agent_id)
    if agent is None:
        sam_retry.remove(agent_id)
        return _emit_error(3, f"agent not found for queue item {agent_id}",
                           as_json)

    if agent.get("state") not in (None, "awaiting_retry"):
        # Ran/was touched meanwhile: the queue item is stale.
        sam_retry.remove(agent_id)
        _clear_queue_fields(agent_id)
        return _emit_error(1, f"not awaiting_retry (state={agent.get('state')}); "
                              f"queue item dropped", as_json)

    if not override_reason and item.get("not_before", 0) > time.time():
        return _emit_error(
            5,
            f"already_queued: fires ~{_fmt_ts(item.get('not_before'))} "
            f"(advisory). --override-reason to fire now (logged).",
            as_json)

    task_path = agent.get("task_path")
    if not task_path or not Path(task_path).expanduser().is_file():
        return _emit_error(1, f"task snapshot missing: {task_path}", as_json)

    re_args = type("Args", (), {})()
    re_args.id_or_name = agent_id
    re_args.name = None
    re_args.task = task_path
    re_args.harness = None        # keep agent's harness
    re_args.model = None          # keep agent's model
    re_args.thinking = None
    re_args.effort = None
    re_args.json = True           # capture resume's output as JSON text
    re_args._infra_retry = True

    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        rc = sam_resume.run(re_args)

    out = buf.getvalue().strip()
    if rc != 0:
        # Surface resume's structured error; keep the queue item (a
        # not-before/pointer error is retryable after fixing).
        err_msg = out
        try:
            err = json.loads(out.splitlines()[-1])
            err_msg = err.get("message", out)
            item["_last_err"] = err_msg
            return _emit_error(err.get("code", rc),
                               err_msg, as_json)
        except (ValueError, IndexError, KeyError):
            item["_last_err"] = out or "infra-retry launch failed"
            return _emit_error(rc, out or "infra-retry launch failed", as_json)

    sam_retry.remove(agent_id)
    _clear_queue_fields(agent_id)
    try:
        launched = json.loads(out.splitlines()[-1])
    except (ValueError, IndexError):
        launched = {}

    if override_reason:
        try:
            with sam_locks.registry_lock(exclusive=True, timeout=10):
                reg = sam_registry.load_registry()
                for a in reg.get("agents", []):
                    if a.get("id") == agent_id:
                        a["retry_override_reason"] = override_reason
                        break
                sam_registry.save_registry(reg)
        except Exception:
            pass

    result = {
        "status": "ok",
        "agent_id": agent_id,
        "name": item.get("name"),
        "run_id": launched.get("run_id"),
        "pid": launched.get("pid"),
        "infra_retry": True,
        "fired_early": bool(override_reason),
        "override_reason": override_reason,
        "resume_output": launched,
    }
    if as_json:
        print(json.dumps(result))
    else:
        print(f"Infra-retry fired for {item.get('name')} "
              f"({item.get('kind')}, run {launched.get('run_id')}, "
              f"pid {launched.get('pid')})")
    return 0


def _cancel(item, as_json):
    agent_id = item.get("agent_id")
    sam_retry.remove(agent_id)
    _clear_queue_fields(agent_id)
    try:
        with sam_locks.registry_lock(exclusive=True, timeout=10):
            reg = sam_registry.load_registry()
            for a in reg.get("agents", []):
                if a.get("id") == agent_id:
                    if a.get("state") == "awaiting_retry":
                        a["state"] = "killed"
                        a["killed_reason"] = "retry_cancelled"
                        a["updated_at"] = datetime.now(timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%SZ")
                    break
            sam_registry.save_registry(reg)
    except Exception:
        pass
    if as_json:
        print(json.dumps({"status": "ok", "agent_id": agent_id,
                          "outcome": "cancelled"}))
    else:
        print(f"Cancelled queued retry for {item.get('name')} ({agent_id})")
    return 0


def _list_queue(as_json):
    items = sam_retry.load_queue()
    now = time.time()
    rows = []
    for i in items:
        nb = i.get("not_before")
        rows.append({
            "agent_id": i.get("agent_id"),
            "name": i.get("name"),
            "model": i.get("model"),
            "kind": i.get("kind"),
            "due": isinstance(nb, (int, float)) and nb <= now,
            "not_before": nb,
            "fires_at": _fmt_ts(nb) if nb else None,
            "retry_in_s": int(nb - now) if isinstance(nb, (int, float)) else None,
            "enqueued_at": _fmt_ts(i.get("enqueued_at")),
            "reason": i.get("reason"),
            "attempts": i.get("attempts", 0),
            "last_error": i.get("last_error"),
        })
    dead_items = sam_retry.load_dead()
    if as_json:
        print(json.dumps({
            "status": "ok",
            "queue": rows,
            "count": len(rows),
            "dead": dead_items,
            "dead_count": len(dead_items),
        }))
        return 0
    if not rows and not dead_items:
        print("Retry queue empty.")
        return 0
    for r in rows:
        mark = "DUE " if r["due"] else "wait"
        att = f" attempts={r['attempts']}" if r.get("attempts") else ""
        print(f"[{mark}] {r['name']} ({r['agent_id']}) kind={r['kind']} "
              f"model={r['model']}{att} fires~{r['fires_at']} "
              f"in {r['retry_in_s']}s")
    if dead_items:
        print(f"\nDead-letter retries ({len(dead_items)}):")
        for d in dead_items:
            print(f"  [DEAD] {d.get('name')} ({d.get('agent_id')}) "
                  f"attempts={d.get('attempts', 3)}: {d.get('last_error', 'unknown')}")
    print("Fire: sam retry NAME   Cancel: sam retry NAME --cancel")
    return 0


def run(args):
    as_json = getattr(args, "json", False)
    try:
        cancel = bool(getattr(args, "cancel", False))
        due_only = bool(getattr(args, "due", False))
        override_reason = (getattr(args, "override_reason", None)
                           or "").strip() or None
        ref = (getattr(args, "id_or_name", None)
               or getattr(args, "name", None))

        if ref is None:
            if due_only:
                items = sam_retry.due_items()
                if not items:
                    if as_json:
                        print(json.dumps({"status": "ok", "fired": 0}))
                    else:
                        print("No due retries.")
                    return 0
                fired = 0
                last_rc = 0
                for item in items:
                    rc = _fire(item, as_json, override_reason)
                    if rc == 0:
                        fired += 1
                    elif rc == 6:
                        last_rc = rc
                        break
                    else:
                        last_rc = rc
                        err_msg = item.get("_last_err", f"exit code {rc}")
                        sam_retry.record_failure(item.get("agent_id"), err_msg)
                        continue
                if not as_json and fired:
                    print(f"Fired {fired}/{len(items)} due retries.")
                return 0 if fired else last_rc
            return _list_queue(as_json)

        if cancel:
            item = sam_retry.find_for_name(ref) or sam_retry.find_for(ref)
            if item is None:
                # fall back to agent lookup for the message
                return _emit_error(1, f"not queued: {ref}", as_json)
            return _cancel(item, as_json)

        item = sam_retry.find_for_name(ref) or sam_retry.find_for(ref)
        if item is None:
            return _emit_error(
                1,
                f"not queued: {ref} (sam enqueues only infra failures: "
                f"429 quota / startup-network deaths)",
                as_json)
        return _fire(item, as_json, override_reason)

    except sam_locks.LockTimeout as e:
        return _emit_error(8, f"lock timeout: {e}", as_json)
    except Exception as e:
        return _emit_error(1, str(e), as_json)
