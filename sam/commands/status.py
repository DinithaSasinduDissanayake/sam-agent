#!/usr/bin/env python3
"""SAM status — Read-only view of agent states.

Spec: reviews-phase-f-batch2.md — GLM-5.2 §4 + Grok-4.5 shared helpers

Lean default list: last 10 non-terminal (non-pruned) newest-first.
  --all               full list (no truncation)
  --limit N           max rows (overrides default 10 and --all)
  --fields a,b,c      comma list projection for --json (e.g. name,state,elapsed)
Default table columns: NAME STATE AGE (human elapsed; no PID/ID).
Opt-in enrichment (unchanged):
  --detail            adds the conservative activity layer (sam/activity.py)
  --watch [SECONDS]   adds two-sample byte deltas (implies --detail)
  --stall-seconds N   threshold before a verified-live agent is
                      possibly_stalled
"""

import json
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_SAM_PKG = _THIS_DIR.parent
if str(_SAM_PKG) not in sys.path:
    sys.path.insert(0, str(_SAM_PKG))

from sam import config as sam_config
from sam import locks as sam_locks
from sam import registry as sam_registry
from sam import state as sam_state
from sam import activity as sam_activity
from datetime import datetime, timezone


def _writeback_terminals(updates):
    """Persist resolved terminal states differing from stored state.

    updates: dict agent_id -> resolved terminal state. Best-effort;
    reloads under exclusive lock, sets state + updated_at, atomic save.
    """
    if not updates:
        return
    try:
        with sam_locks.registry_lock(exclusive=True, timeout=10):
            registry = sam_registry.load_registry()
            dirty = False
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            for a in registry.get("agents", []):
                rid = updates.get(a.get("id"))
                if rid is None:
                    continue
                if rid in sam_state.TERMINAL_STATES and a.get("state") != rid:
                    a["state"] = rid
                    a["updated_at"] = now_str
                    dirty = True
            if dirty:
                sam_registry.save_registry(registry)
    except Exception:
        pass


_DETAIL_HEADER = (
    f"{'ID':20s} {'NAME':20s} {'STATE':10s} {'ACTIVITY':18s} "
    f"{'LAST-EVT':10s} {'BYTES/30s':8s} {'TOK/30s':9s} {'THINK/30s':9s} "
    f"{'LOG-AGE':8s}"
)

_WATCH_WARN_THRESHOLD = 100


def _emit_error(code, message, as_json):
    if as_json:
        print(json.dumps({"status": "error", "code": code, "message": message}), file=sys.stderr)
    else:
        print(f"sam: {message}", file=sys.stderr)
    return code


def _fmt_age(age):
    if age is None:
        return "-"
    return "%ds" % int(age)


def _parse_ts(value):
    if not value:
        return None
    try:
        s = str(value).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _elapsed_seconds(entry):
    dt = _parse_ts(entry.get("created_at"))
    if dt is None:
        return None
    try:
        now = datetime.now(timezone.utc)
        return max(0, int((now - dt).total_seconds()))
    except Exception:
        return None


def _fmt_elapsed(entry):
    secs = _elapsed_seconds(entry)
    if secs is None:
        return "-"
    if secs < 60:
        return "%ds" % secs
    mins = secs // 60
    if mins < 60:
        return "%dm" % mins
    hours = mins // 60
    if hours < 24:
        rem = mins % 60
        return "%dh%02dm" % (hours, rem) if rem else "%dh" % hours
    days = hours // 24
    rem_h = hours % 24
    return "%dd%02dh" % (days, rem_h) if rem_h else "%dd" % days


def _parse_fields(raw):
    if not raw:
        return None
    fields = [f.strip().lower() for f in str(raw).split(",") if f.strip()]
    return fields or None


def _project_fields(entry, fields):
    out = {}
    for f in fields:
        if f == "name":
            out["name"] = entry.get("name")
        elif f == "state":
            out["state"] = entry.get("resolved_state", entry.get("state"))
        elif f in ("elapsed", "age"):
            out[f] = _elapsed_seconds(entry)
        else:
            out[f] = entry.get(f)
    return out


def _fmt_num(value):
    if value is None:
        return "-"
    return str(value)


def _fmt_tokens(ss, key):
    """TOK/30s cell: usage tokens, else ~estimated, else -."""
    v = ss.get(key)
    if v is not None:
        return str(v)
    est_key = "estimated_" + key[len("usage_"):]
    est = ss.get(est_key)
    if est is not None:
        return "~%d" % est
    return "-"


def _fmt_growth(delta):
    """Compact growth status for a watch delta dict."""
    if not delta:
        return "?"
    status = delta.get("status")
    if status == "measured":
        return "+%d" % delta.get("growth_bytes", 0)
    if status == "missing":
        return "-"
    if status == "replaced":
        return "R"
    if status == "shrunk":
        return "S"
    return "E"


def _growth_str(delta):
    """Human string for a watch delta dict."""
    if not delta:
        return "error"
    status = delta.get("status")
    if status == "measured":
        return "+%dB" % delta.get("growth_bytes", 0)
    return status


def _detail_row(a):
    """One table row for --detail list mode."""
    act = a.get("activity") or {}
    ss = act.get("session") or {}
    lg = act.get("log") or {}
    st = act.get("activity_state") or a.get("resolved_state") or "?"
    row = (
        f"{a.get('id', '?'):20s} {a.get('name', '?'):20s} "
        f"{a.get('resolved_state', '?'):10s} {st:18s} "
        f"{_fmt_age(ss.get('last_event_age')):10s} "
        f"{_fmt_num(ss.get('recent_event_bytes_30s')):8s} "
        f"{_fmt_tokens(ss, 'usage_tokens_30s'):9s} "
        f"{_fmt_num(ss.get('usage_thinking_tokens_30s')):9s} "
        f"{_fmt_age(lg.get('mtime_age')):8s}"
    )
    watch = act.get("watch")
    if watch:
        row += "  s%s l%s" % (_fmt_growth(watch.get("session")),
                              _fmt_growth(watch.get("log")))
    return row


def _print_activity_detail(act, indent=""):
    """Human block for a single agent in --detail mode."""
    st = act.get("activity_state", "?")
    ss = act.get("session") or {}
    lg = act.get("log") or {}
    watch = act.get("watch")
    print(f"{indent}Activity: {st}")
    print(f"{indent}  lifecycle: {act.get('lifecycle_state', '?')}")
    if ss.get("error"):
        print(f"{indent}  session: error ({ss['error']})")
    elif ss.get("exists"):
        print(f"{indent}  session: size={ss.get('size')} "
              f"mtime={_fmt_age(ss.get('mtime_age'))} "
              f"last_event={_fmt_age(ss.get('last_event_age'))} "
              f"role={ss.get('last_event_role')} "
              f"stop={ss.get('last_event_stop_reason')} "
              f"tool_pending={ss.get('tool_pending')}")
        if ss.get("pending_tool_call_ids"):
            print(f"{indent}    pending tools: "
                  f"{', '.join(ss['pending_tool_call_ids'])}")
        print(f"{indent}  recent events (5s/30s): "
              f"count={ss.get('recent_event_count_5s')}/"
              f"{ss.get('recent_event_count_30s')} bytes="
              f"{ss.get('recent_event_bytes_5s')}/"
              f"{ss.get('recent_event_bytes_30s')}")
        print(f"{indent}  usage tokens (5s/30s): "
              f"{_fmt_num(ss.get('usage_tokens_5s'))}/"
              f"{_fmt_num(ss.get('usage_tokens_30s'))} "
              f"(total={_fmt_num(ss.get('usage_tokens_total'))}) "
              f"thinking: {_fmt_num(ss.get('usage_thinking_tokens_5s'))}/"
              f"{_fmt_num(ss.get('usage_thinking_tokens_30s'))} "
              f"(total={_fmt_num(ss.get('usage_thinking_tokens_total'))})")
        if ss.get("estimated_tokens_5s") is not None or \
                ss.get("estimated_tokens_30s") is not None:
            print(f"{indent}  estimated tokens (5s/30s): "
                  f"{_fmt_num(ss.get('estimated_tokens_5s'))}/"
                  f"{_fmt_num(ss.get('estimated_tokens_30s'))} "
                  f"(chars/4 fallback, separate from usage tokens)")
        if ss.get("truncated"):
            print(f"{indent}    note: session tail truncated at "
                  f"{sam_activity.DEFAULT_MAX_BYTES} byte budget")
    else:
        print(f"{indent}  session: missing")
    if lg.get("error"):
        print(f"{indent}  log: error ({lg['error']})")
    elif lg.get("exists"):
        print(f"{indent}  log: size={lg.get('size')} "
              f"mtime={_fmt_age(lg.get('mtime_age'))} "
              f"began={lg.get('began')} ended={lg.get('ended')}")
    else:
        print(f"{indent}  log: missing")
    if watch:
        print(f"{indent}  watch ({watch.get('interval_seconds')}s): "
              f"session {_growth_str(watch.get('session'))} "
              f"log {_growth_str(watch.get('log'))}")
    for e in act.get("evidence") or []:
        print(f"{indent}  evidence: {e}")


def _compute_activity(agent, resolved, stall_seconds, watch):
    """Activity block for one agent; never raises."""
    try:
        return sam_activity.compute_agent_activity(
            agent, resolved, stall_seconds=stall_seconds, watch=watch)
    except Exception as e:  # defensive: analysis must never fail status
        return {
            "lifecycle_state": resolved,
            "activity_state": "error",
            "evidence": ["activity analysis failed: %s" % e],
            "error": str(e),
        }


def run(args):
    as_json = getattr(args, "json", False)
    try:
        registry = sam_registry.load_registry()
        agents = registry.get("agents", [])
    except Exception as e:
        return _emit_error(1, f"cannot load registry: {e}", as_json)

    # v0.1.1: --all flag shows terminal agents too; default hides them
    show_all = getattr(args, "all", False)
    show_archived_only = getattr(args, "archived", False)
    limit = getattr(args, "limit", None)
    if limit is not None:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            return _emit_error(1, "invalid --limit (must be an integer)", as_json)
        if limit < 0:
            return _emit_error(1, "invalid --limit (must be >= 0)", as_json)
    fields = _parse_fields(getattr(args, "fields", None))

    # v0.1.2: opt-in activity enrichment (read-only, conservative).
    detail = getattr(args, "detail", False)
    watch = getattr(args, "watch", None)
    if watch is not None:
        watch = sam_activity.clamp_watch_seconds(watch)
    detail = detail or watch is not None
    stall_seconds = getattr(args, "stall_seconds", None)
    if stall_seconds is None:
        stall_seconds = sam_activity.DEFAULT_STALL_SECONDS
    stall_seconds = max(1, int(stall_seconds))

    ref = getattr(args, "id_or_name", None) or getattr(args, "name", None)

    if ref:
        # Single agent mode
        agent = None
        # Try exact ID first
        for a in agents:
            if a.get("id") == ref:
                agent = a
                break
        # Try exact name
        if agent is None:
            matches = [a for a in agents if a.get("name") == ref]
            # Among matches, prefer non-terminal
            non_term = [a for a in matches if a.get("state") not in sam_state.TERMINAL_STATES]
            if len(non_term) > 1:
                return _emit_error(1, f"ambiguous name '{ref}'", as_json)
            if non_term:
                agent = non_term[0]
            elif matches:
                agent = matches[0]

        if agent is None:
            return _emit_error(1, f"agent not found: {ref}", as_json)

        # Resolve state
        try:
            resolved = sam_state.resolve_agent_state(
                agent, agent.get("run_id", 1))
            agent = dict(agent)
            agent["resolved_state"] = resolved
        except Exception:
            agent = dict(agent)
            agent["resolved_state"] = "unknown"
            resolved = "unknown"

        _writeback_terminals({agent["id"]: resolved} if resolved in sam_state.TERMINAL_STATES and resolved != agent.get("state") else {})

        if detail:
            agent["activity"] = _compute_activity(
                agent, agent["resolved_state"], stall_seconds, watch)

        if as_json:
            out = _project_fields(agent, fields) if fields else agent
            print(json.dumps(out, default=str))
        else:
            s = agent.get("resolved_state", "?")
            if detail:
                print(f"{agent.get('id','?'):20s} {agent.get('name','?'):20s} "
                      f"{s:10s} pid={agent.get('pid', '?')}")
                _print_activity_detail(agent["activity"], indent="  ")
            else:
                print(f"{'NAME':20s} {'STATE':10s} {'AGE':8s}")
                print("-" * 40)
                print(f"{agent.get('name','?'):20s} {s:10s} "
                      f"{_fmt_elapsed(agent):8s}")
        return 0

    # List mode: resolve first, then filter default view on resolved state.
    resolved_list = []
    updates = {}
    for a in agents:
        try:
            resolved = sam_state.resolve_agent_state(a, a.get("run_id", 1))
            entry = dict(a)
            entry["resolved_state"] = resolved
        except Exception:
            entry = dict(a)
            entry["resolved_state"] = "failed"
            resolved = "failed"
        if resolved in sam_state.TERMINAL_STATES and resolved != a.get("state"):
            updates[a.get("id")] = resolved
        resolved_list.append(entry)

    _writeback_terminals(updates)

    # Lean default: non-terminal + non-pruned + non-archived, newest-first,
    # last 10; --all keeps everything unfiltered but still attaches
    # resolved_state; --archived lists only archived entries.
    # Single-agent lookup above always works on archived entries, as do
    # `sam logs` and `sam result` (no archived filtering there).
    show_archived_col = bool(show_all or show_archived_only)
    if show_archived_only:
        resolved_list = [e for e in resolved_list if e.get("archived")]
    elif not show_all:
        resolved_list = [e for e in resolved_list
                         if e.get("resolved_state") not in sam_state.TERMINAL_STATES
                         and not e.get("pruned")
                         and not e.get("archived")]

    resolved_list.sort(key=lambda x: x.get("created_at", ""), reverse=True)

    # Truncation: explicit --limit wins; else default 10 unless --all.
    if limit is not None:
        resolved_list = resolved_list[:limit]
    elif not show_all:
        resolved_list = resolved_list[:10]

    if detail and watch is not None and len(resolved_list) >= _WATCH_WARN_THRESHOLD:
        print(f"sam: warning: --watch on {len(resolved_list)} agents may be "
              f"slow (two samples each)", file=sys.stderr)

    if detail:
        for entry in resolved_list:
            entry["activity"] = _compute_activity(
                entry, entry["resolved_state"], stall_seconds, watch)

    if as_json:
        out = [_project_fields(e, fields) for e in resolved_list] if fields else resolved_list
        print(json.dumps(out, default=str))
    else:
        if detail:
            print(_DETAIL_HEADER)
            print("-" * len(_DETAIL_HEADER))
            for a in resolved_list:
                print(_detail_row(a))
                if a.get("archived"):
                    print(f"{'':20s} {'':20s} {'':10s} archived")
        elif show_archived_col:
            print(f"{'NAME':20s} {'STATE':10s} {'AGE':8s} {'ARCHIVED':8s}")
            print("-" * 49)
            for a in resolved_list:
                s = a.get("resolved_state", "?")
                flag = "archived" if a.get("archived") else "-"
                print(f"{a.get('name','?'):20s} {s:10s} "
                      f"{_fmt_elapsed(a):8s} {flag:8s}")
        else:
            print(f"{'NAME':20s} {'STATE':10s} {'AGE':8s}")
            print("-" * 40)
            for a in resolved_list:
                s = a.get("resolved_state", "?")
                print(f"{a.get('name','?'):20s} {s:10s} "
                      f"{_fmt_elapsed(a):8s}")
    return 0
