#!/usr/bin/env python3
"""SAM TUI Dashboard — monitors ~/.sam/registry.json with auto-refresh.

Usage:
  ~/.sam/bin/sam-tui            Full-screen TUI (auto-refresh every 2s)
  ~/.sam/bin/sam-tui --once     Print once and exit (for scripts)
  ~/.sam/bin/sam-tui --all      Show all agents newest-first (incl. archived, old)

Default (recent) view: active (running/unknown <7d) + last 20
completed/failed newest-first (created_at desc). Archived and
terminals older than 7d are hidden unless --all.

Keys:
  q  Quit

Source: sam repo wrapper/sam-tui.py — installed to ~/.sam/bin/sam-tui
by `sam init`. States are resolved via sam.state.resolve_agent_state
(/proc liveness + result.json), never raw registry `state`, so dead PIDs
show as unknown?stale / completed / failed instead of stuck "running".
"""

import json
import os
import select
import sys
import time
from datetime import datetime, timezone

from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

REGISTRY = os.path.join(os.path.expanduser(os.environ.get("SAM_HOME", "~/.sam")), "registry.json")
REFRESH_SEC = 2
RECENT_DAYS = 7
RECENT_LIMIT = 20

LEGEND = ("Hints: failed! needs attention; partial* = failed run with captured "
          "deliverable text (see PARTIAL.md); retry* = awaiting infra-retry "
          "(sam retry NAME to fire, --cancel to drop); unknown?stale = PID "
          "dead/recycled, no result.json (check logs/result). AGE = since "
          "current-run start; DONE = since done (result ended_at) else -; "
          "ACT = motion (▲active ▬idle ~alive-no-task-signal …starting, "
          "from file-write age + proc liveness); Model shows [thinking] "
          "(pi) or [effort:X] (agy, when "
          "overridden).")

TERMINAL_STATES = frozenset({
    "completed", "failed", "killed", "partial", "awaiting_retry"})


def _resolve(agent):
    """True state via sam.state.resolve_agent_state; fallback to raw state."""
    try:
        from sam.state import resolve_agent_state
        return resolve_agent_state(agent, agent.get("run_id", 1))
    except Exception:
        return agent.get("state", "unknown")


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


def _fmt_dur(secs):
    if secs < 0:
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


def _run_start(entry):
    from sam.run_times import run_started_at
    dt = run_started_at(entry)
    return dt.isoformat() if dt is not None else None


def _result_ended_at(entry):
    """Authoritative finish time of the current run, or None.

    Read-only read of the current run's result.json `ended_at` (epoch
    seconds as written by the wrappers, ISO string accepted). Never falls
    back to mutable registry `updated_at`, which status/archive rewrites.
    """
    from sam.run_times import run_ended_at
    return run_ended_at(entry)


def _fmt_age(entry):
    dt = _parse_ts(_run_start(entry))
    try:
        secs = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    except Exception:
        return "-"
    return _fmt_dur(secs)


def _fmt_done(entry, state):
    if state not in TERMINAL_STATES:
        return "-"
    # DONE = since the current run actually finished (result.json ended_at).
    # Missing/unreadable finish time shows "-" (unknown) — never guessed
    # from updated_at.
    dt = _result_ended_at(entry)
    if dt is None:
        return "-"
    try:
        secs = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    except Exception:
        return "-"
    return _fmt_dur(secs)


def _fmt_model(entry):
    model = entry.get("model", "") or ""
    if "/" in model:
        model = model.split("/")[-1]
    harness = entry.get("harness") or ("agy" if entry.get("conversation_id") else "pi")
    if harness == "agy":
        effort = entry.get("effort")
        if effort:
            suffix = model.rsplit("-", 1)[-1].lower() if "-" in model else ""
            if suffix != str(effort).lower():
                return f"{model} [effort:{effort}]"
        return model
    thinking = entry.get("thinking")
    if thinking:
        return f"{model} [{thinking}]"
    return f"{model} [unknown]"


def _fmt_state(state):
    if state == "failed":
        return "failed!"
    if state == "partial":
        return "partial*"
    if state == "awaiting_retry":
        return "retry*"
    if state == "unknown":
        return "unknown?stale"
    return state or "?"


def _fmt_act(entry, state):
    """Compact motion cell from proc+stat liveness (no tail reads)."""
    try:
        from sam.activity import quick_liveness
        liv = quick_liveness(entry, state)
    except Exception:
        return "-"
    verdict = liv.get("verdict", "?")
    if verdict in ("completed", "failed", "killed", "partial", "spawning",
                   "unknown", "awaiting_retry"):
        return "-"
    age = liv.get("age")
    if verdict == "active":
        return "▲%s" % _fmt_dur(age) if age is not None else "▲"
    if verdict == "idle":
        return "▬%s" % _fmt_dur(age) if age is not None else "▬"
    if verdict == "alive":
        return "~%s" % _fmt_dur(age) if age is not None else "~"
    if verdict == "quiet-start":
        return "…"
    return verdict


def _age_days(entry):
    dt = _parse_ts(_run_start(entry))
    if dt is None:
        return 0.0  # unknown age counts as recent (don't hide)
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
    except Exception:
        return 0.0


def _sort_key(entry):
    dt = _parse_ts(_run_start(entry))
    if dt is None:
        return ""
    try:
        return dt.isoformat()
    except Exception:
        return str(entry.get("created_at", ""))


def _load_agents(show_archived=False):
    with open(REGISTRY) as f:
        data = json.load(f)
    agents = data.get("agents", [])
    total = len(agents)
    if not show_archived:
        agents = [a for a in agents if not a.get("archived")]
    unarchived_total = len(agents)
    resolved = []
    for a in agents:
        entry = dict(a)
        entry["resolved_state"] = _resolve(a)
        resolved.append(entry)
    if show_archived:
        resolved.sort(key=_sort_key, reverse=True)
        return resolved, total
    # Recent default: active (running/unknown <7d) + last 20
    # completed/failed newest-first (created_at desc).
    active = [e for e in resolved
               if e["resolved_state"] in ("spawning", "running", "unknown")
              and _age_days(e) < RECENT_DAYS]
    terminals = [e for e in resolved
                  if e["resolved_state"] in TERMINAL_STATES]
    terminals.sort(key=_sort_key, reverse=True)
    recent_terminals = terminals[:RECENT_LIMIT]
    seen = {id(e) for e in active}
    shown = list(active) + [e for e in recent_terminals if id(e) not in seen]
    shown.sort(key=_sort_key, reverse=True)
    return shown, unarchived_total


def build_dashboard(show_archived=False):
    """Read registry.json and return a rich Layout with summary + agent table."""
    try:
        agents, total = _load_agents(show_archived)
    except FileNotFoundError:
        return Panel("Waiting for registry.json to appear...", title="SAM Dashboard", border_style="yellow")
    except json.JSONDecodeError:
        return Panel("registry.json is corrupt (invalid JSON)", title="SAM Dashboard", border_style="red")

    running = [a for a in agents if a["resolved_state"] == "running"]
    completed = [a for a in agents if a["resolved_state"] == "completed"]
    failed = [a for a in agents if a["resolved_state"] == "failed"]
    partial = [a for a in agents if a["resolved_state"] == "partial"]
    queued = [a for a in agents if a["resolved_state"] == "awaiting_retry"]
    unknown = [a for a in agents if a["resolved_state"] == "unknown"]

    # ── Summary bar (resolved counts) ──
    parts = []
    if running:
        parts.append(f"  ▶ Running: [yellow]{len(running)}[/]")
    if completed:
        parts.append(f"  ✓ Completed: [green]{len(completed)}[/]")
    if failed:
        parts.append(f"  ✗ Failed: [red]{len(failed)}[/]")
    if partial:
        parts.append(f"  ◐ Partial: [magenta]{len(partial)}[/]")
    if queued:
        parts.append(f"  ↻ Retry queued: [yellow]{len(queued)}[/]")
    if unknown:
        parts.append(f"  ? Unknown: [yellow]{len(unknown)}[/]")
    parts.append(f"  ━ Total: {len(agents)}")
    mode = "all" if show_archived else "recent"
    parts.append(f"  ━ Showing {len(agents)} of {total} ({mode})")
    summary = Panel("   ".join(parts), title="SAM Agents", border_style="blue")

    # ── Agent table: Agent State AGE DONE Runs Harness Model ──
    table = Table(box=None, padding=(0, 1))
    table.add_column("", width=2)  # status icon
    table.add_column("Agent", style="cyan", no_wrap=True)
    table.add_column("State")
    table.add_column("ACT", justify="right", no_wrap=True)
    table.add_column("AGE", justify="right", no_wrap=True)
    table.add_column("DONE", justify="right", no_wrap=True)
    table.add_column("Runs", justify="right")
    table.add_column("Harness", style="dim")
    table.add_column("Model", style="dim")

    for a in agents:
        state = a["resolved_state"]
        if state == "running":
            icon, style = "▶", "yellow"
        elif state == "completed":
            icon, style = "✓", "green"
        elif state == "partial":
            icon, style = "◐", "magenta"
        elif state == "awaiting_retry":
            icon, style = "↻", "yellow"
        elif state == "unknown":
            icon, style = "?", "yellow"
        else:
            icon, style = "✗", "red"

        runs = a.get("run_count")
        if runs is None:
            runs = a.get("run_id", "-")

        harness = a.get("harness") or ("agy" if a.get("conversation_id") else "pi")

        model = _fmt_model(a)

        table.add_row(
            f"[{style}]{icon}[/]",
            a.get("name", "?"),
            f"[{style}]{_fmt_state(state)}[/]",
            _fmt_act(a, state),
            _fmt_age(a),
            _fmt_done(a, state),
            str(runs),
            harness,
            model,
        )

    # ── Layout ──
    layout = Layout()
    layout.split_column(
        Layout(summary, size=4),
        Layout(table),
        Layout(Panel(LEGEND, border_style="dim"), size=3),
    )
    return layout


def main():
    once = "--once" in sys.argv
    show_archived = "--all" in sys.argv or os.environ.get("SAM_TUI_SHOW_ARCHIVED") == "1"

    if once:
        # Single-shot: print table once, no live display
        from rich.console import Console
        console = Console()
        dashboard = build_dashboard(show_archived)
        console.print(dashboard)
        return

    try:
        with Live(build_dashboard(show_archived), refresh_per_second=8, screen=True) as live:
            while True:
                # Check for 'q' key (non-blocking on Unix)
                if select.select([sys.stdin], [], [], 0)[0]:
                    key = sys.stdin.read(1)
                    if key == "q":
                        break
                    # Consume any remaining buffered input
                    while select.select([sys.stdin], [], [], 0)[0]:
                        sys.stdin.read(1)
                time.sleep(REFRESH_SEC)
                live.update(build_dashboard(show_archived))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
