#!/usr/bin/env python3
"""SAM status — Read-only view of agent states.

Spec: reviews-phase-f-batch2.md — GLM-5.2 §4 + Grok-4.5 shared helpers

Default output (text and JSON) is unchanged. Opt-in enrichment:
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
from sam import registry as sam_registry
from sam import state as sam_state
from sam import activity as sam_activity


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

        if detail:
            agent["activity"] = _compute_activity(
                agent, agent["resolved_state"], stall_seconds, watch)

        if as_json:
            print(json.dumps(agent, default=str))
        else:
            s = agent.get("resolved_state", "?")
            pid = agent.get("pid", "?")
            print(f"{agent.get('id','?'):20s} {agent.get('name','?'):20s} "
                  f"{s:10s} pid={pid}")
            if detail:
                _print_activity_detail(agent["activity"], indent="  ")
        return 0

    # List mode
    # v0.1.1: filter to active (non-terminal) by default unless --all
    if not show_all:
        agents = [a for a in agents
                  if a.get("state") not in sam_state.TERMINAL_STATES]

    resolved_list = []
    for a in agents:
        try:
            resolved = sam_state.resolve_agent_state(a, a.get("run_id", 1))
            entry = dict(a)
            entry["resolved_state"] = resolved
        except Exception:
            entry = dict(a)
            entry["resolved_state"] = "failed"
        resolved_list.append(entry)

    resolved_list.sort(key=lambda x: x.get("created_at", ""), reverse=True)

    if detail and watch is not None and len(resolved_list) >= _WATCH_WARN_THRESHOLD:
        print(f"sam: warning: --watch on {len(resolved_list)} agents may be "
              f"slow (two samples each)", file=sys.stderr)

    if detail:
        for entry in resolved_list:
            entry["activity"] = _compute_activity(
                entry, entry["resolved_state"], stall_seconds, watch)

    if as_json:
        print(json.dumps(resolved_list, default=str))
    else:
        if detail:
            print(_DETAIL_HEADER)
            print("-" * len(_DETAIL_HEADER))
            for a in resolved_list:
                print(_detail_row(a))
        else:
            print(f"{'ID':20s} {'NAME':20s} {'STATE':10s} {'PID':8s}")
            print("-" * 60)
            for a in resolved_list:
                s = a.get("resolved_state", "?")
                pid = str(a.get("pid", "?"))
                print(f"{a.get('id','?'):20s} {a.get('name','?'):20s} "
                      f"{s:10s} pid={pid}")
    return 0
