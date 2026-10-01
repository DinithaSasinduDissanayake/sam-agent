#!/usr/bin/env python3
"""SAM doctor — Offline audit: spawn spacing, concurrency, overrides, queue.

IISA review items 1 + 4 (the acceptance instrument). `sam doctor --window`
answers: did launch spacing hold ≥ SPAWN_SPACING_S, did concurrency at
spawn stay ≤ MAX_RUNNING, how often was --override-reason used, and what
is queued for infra-retry right now. Read-only; exits 0 always.

  sam doctor                 # last 24 h
  sam doctor --window 6      # last 6 hours
  sam doctor --window        # = 24
  sam doctor --json          # machine-readable (tests, scripting)
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_SAM_PKG = _THIS_DIR.parent
if str(_SAM_PKG) not in sys.path:
    sys.path.insert(0, str(_SAM_PKG))

from sam import proc as sam_proc
from sam import registry as sam_registry
from sam import retry as sam_retry
from sam import run_times as sam_run_times
from sam import state as sam_state


def _parse_iso(value):
    return sam_run_times.parse_timestamp(value)


def _fmt_utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _spawn_events(agents, cutoff):
    """(when_dt, entry) for every spawn (registry creation) since cutoff."""
    events = []
    for a in agents:
        created = _parse_iso(a.get("created_at"))
        if created is None:
            continue
        if created >= cutoff:
            events.append((created, a))
    events.sort(key=lambda e: e[0])
    return events


def _interval(entry, now_dt):
    """(start, end) of the entry's current run, or None without a start.

    End, in order: recorded end (result.json ended_at, registry ended_at /
    completed_at); `now` only while the run is genuinely live (resolved
    running/spawning); else start + duration_ms; else the newest mtime of
    output.log / result.json; else start. A dead run is never counted as
    still running (that produced phantom OVER-CAP rows).
    """
    start = sam_run_times.run_started_at(entry)
    if start is None:
        return None
    end = sam_run_times.run_ended_at(entry)
    if end is None:
        live = False
        if entry.get("state") not in sam_state.TERMINAL_STATES:
            try:
                run_id = entry.get("run_id") or entry.get("run_count") or 1
                live = sam_state.resolve_agent_state(entry, run_id) in ("running", "spawning")
            except Exception:
                live = False
        if live:
            end = now_dt
        else:
            dur = entry.get("duration_ms")
            if isinstance(dur, (int, float)) and not isinstance(dur, bool) and dur >= 0:
                end = start + timedelta(milliseconds=dur)
            else:
                end = sam_run_times.run_end_evidence(entry) or start
    if end < start:
        end = start
    if end > now_dt >= start:
        end = now_dt
    return start, end


def _concurrency_at(agents, t, now_dt):
    n = 0
    for a in agents:
        iv = _interval(a, now_dt)
        if iv is None:
            continue
        if iv[0] <= t <= iv[1]:
            n += 1
    return n


def _harness_status():
    """Per harness: wrapper installed in $SAM_HOME/bin? binary on PATH? Read-only."""
    import shutil
    from sam import config as sam_config
    out = {}
    for name in sam_config.HARNESS_WRAPPERS:
        try:
            installed = sam_config.wrapper_path(harness=name).is_file()
        except Exception:
            installed = False
        out[name] = {"wrapper_installed": installed, "binary": shutil.which(name)}
    return out


def collect(window_hours=24.0, now=None):
    now_dt = datetime.now(timezone.utc) if now is None else now
    cutoff = now_dt - timedelta(hours=float(window_hours))
    reg = sam_registry.load_registry()
    agents = reg.get("agents", [])

    launches = sam_proc.load_launches()
    if launches:
        agent_by_id = {a.get("id"): a for a in agents}
        launch_events = []
        for l in launches:
            ts = l.get("ts")
            if ts is None:
                continue
            when = datetime.fromtimestamp(ts, timezone.utc)
            a = dict(agent_by_id.get(l.get("agent_id"), {}))
            a.update(l)
            a["spacing_bypassed"] = l.get("bypassed", a.get("spacing_bypassed", False))
            launch_events.append((when, a))
        launch_events.sort(key=lambda e: e[0])
        events = [e for e in launch_events if e[0] >= cutoff]
        prior = [e for e in launch_events if e[0] < cutoff]
    else:
        events = _spawn_events(agents, cutoff)
        # Include the last spawn before the window so gap math crosses the edge.
        prior = [(_parse_iso(a.get("created_at")), a) for a in agents]
        prior = [e for e in prior if e[0] is not None and e[0] < cutoff]
        prior.sort(key=lambda e: e[0])

    spacing_violations = []
    cap_violations = []
    rows = []
    prev_t = prior[-1][0] if prior else None
    max_conc = 0
    min_gap = None

    for when, a in events:
        gap = (when - prev_t).total_seconds() if prev_t is not None else None
        conc = _concurrency_at(agents, when, now_dt)
        max_conc = max(max_conc, conc)
        bypass = bool(a.get("spacing_bypassed"))
        row = {
            "at": _fmt_utc(when),
            "name": a.get("name"),
            "id": a.get("id"),
            "model": a.get("model"),
            "harness": a.get("harness"),
            "kind": a.get("kind", "spawn"),
            "gap_s": None if gap is None else round(gap, 3),
            "concurrency_at_spawn": conc,
            "spacing_bypassed": bypass,
            "fail_open": bool(a.get("fail_open")),
            "spawn_waited_s": a.get("spawn_waited_s"),
            "quota_override_reason": a.get("quota_override_reason"),
        }
        rows.append(row)
        if gap is not None:
            if min_gap is None or gap < min_gap:
                min_gap = gap
            if (gap < sam_proc.SPAWN_SPACING_S - 0.05) and not bypass:
                spacing_violations.append(row)
        if conc > sam_proc.MAX_RUNNING:
            cap_violations.append(row)
        prev_t = when

    overrides = [
        {"at": r["at"], "name": r["name"], "reason": r["quota_override_reason"]}
        for r in rows if r.get("quota_override_reason")
    ]

    queue = []
    corrupt_queues = []
    q_dir = sam_retry.queue_path().parent
    if q_dir.is_dir():
        for cf in sorted(q_dir.glob("retry_queue.corrupt-*")):
            corrupt_queues.append(cf.name)
    try:
        loaded_items = sam_retry.load_queue()
    except Exception as e:
        corrupt_queues.append(str(e))
        loaded_items = []

    for i in loaded_items:
        nb = i.get("not_before")
        queue.append({
            "name": i.get("name"),
            "agent_id": i.get("agent_id"),
            "kind": i.get("kind"),
            "model": i.get("model"),
            "fires_at": (_fmt_utc(datetime.fromtimestamp(nb, timezone.utc))
                         if isinstance(nb, (int, float)) else None),
            "due": isinstance(nb, (int, float)) and nb <= time.time(),
        })

    dead_retries = []
    try:
        dead_retries = sam_retry.load_dead()
    except Exception:
        pass

    ok = not spacing_violations and not cap_violations
    return {
        "window_hours": float(window_hours),
        "since": _fmt_utc(cutoff),
        "until": _fmt_utc(now_dt),
        "spawns_in_window": len(rows),
        "min_gap_s": min_gap,
        "max_concurrency_at_spawn": max_conc,
        "spacing_violations": spacing_violations,
        "cap_violations": cap_violations,
        "quota_overrides": overrides,
        "retry_queue": queue,
        "dead_retries": dead_retries,
        "corrupt_retry_queues": corrupt_queues,
        "spacing_ok": ok and len(rows) > 0,
        "no_spawns": len(rows) == 0,
        "spawn_log": rows,
        "harnesses": _harness_status(),
        "thresholds": {
            "spawn_spacing_s": sam_proc.SPAWN_SPACING_S,
            "max_running": sam_proc.MAX_RUNNING,
        },
    }


def run(args):
    as_json = getattr(args, "json", False)
    try:
        window = getattr(args, "window", None)
        if window is None:
            window = 24.0
        report = collect(window)

        if as_json:
            print(json.dumps({"status": "ok", "doctor": report}, indent=1))
            return 0

        print(f"sam doctor --window {report['window_hours']:g}h "
              f"({report['since']} .. {report['until']})")
        print(f"spawns: {report['spawns_in_window']}  "
              f"min gap: {_fmt_gap(report['min_gap_s'])}  "
              f"max concurrency at spawn: {report['max_concurrency_at_spawn']}")
        thr = report["thresholds"]
        print(f"thresholds: spacing>={thr['spawn_spacing_s']}s  "
              f"concurrency<={thr['max_running']}")
        hs = report.get("harnesses") or {}
        print("harnesses: " + "  ".join(
            f"{h}(wrapper {'ok' if v['wrapper_installed'] else 'MISSING'}, "
            f"bin {v['binary'] or 'MISSING'})" for h, v in hs.items()))
        for r in report["spawn_log"]:
            flags = []
            if r["gap_s"] is not None and r["gap_s"] < thr["spawn_spacing_s"]:
                flags.append("VIOLATION" if not r["spacing_bypassed"]
                             else f"bypassed({r['gap_s']}s)")
            if r["concurrency_at_spawn"] > thr["max_running"]:
                flags.append(f"OVER-CAP({r['concurrency_at_spawn']})")
            if r.get("fail_open"):
                flags.append("FAIL-OPEN")
            if r.get("quota_override_reason"):
                flags.append(f"override: {r['quota_override_reason']}")
            mark = " ".join(flags)
            kind_str = f" [{r['kind']}]" if r.get("kind") and r["kind"] != "spawn" else ""
            harness_str = f" <{r['harness']}>" if r.get("harness") else ""
            print(f"  {r['at']}  gap={_fmt_gap(r['gap_s'])}  "
                  f"conc={r['concurrency_at_spawn']}  {r['name']}{kind_str}{harness_str}  {mark}")
        if report["quota_overrides"]:
            print(f"quota overrides: {len(report['quota_overrides'])}")
        if report["retry_queue"]:
            print("retry queue:")
            for q in report["retry_queue"]:
                print(f"  [{'DUE' if q['due'] else 'wait'}] {q['name']} "
                      f"kind={q['kind']} fires~{q['fires_at']}")
        if report.get("dead_retries"):
            print(f"dead retries: {len(report['dead_retries'])}")
            for d in report["dead_retries"]:
                print(f"  [DEAD] {d.get('name')} ({d.get('agent_id')}) "
                      f"attempts={d.get('attempts', 3)}: {d.get('last_error', 'unknown')}")
        if report.get("corrupt_retry_queues"):
            print(f"corrupt retry queue: {', '.join(report['corrupt_retry_queues'])}")
        if report["no_spawns"]:
            print("no spawns in window")
        elif report["spacing_ok"]:
            print(f"VERDICT: SPACING OK (min gap "
                  f"{_fmt_gap(report['min_gap_s'])}, no cap breaches)")
        else:
            print(f"VERDICT: VIOLATIONS spacing="
                  f"{len(report['spacing_violations'])} "
                  f"cap={len(report['cap_violations'])}")
        return 0
    except Exception as e:
        if getattr(args, "json", False):
            print(json.dumps({"status": "error", "message": str(e)}))
        else:
            print(f"sam: {e}", file=sys.stderr)
        return 1


def _fmt_gap(gap):
    if gap is None:
        return "-"
    if gap >= 60:
        return f"{gap / 60:.1f}m"
    return f"{gap:.1f}s"
