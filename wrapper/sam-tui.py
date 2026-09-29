#!/usr/bin/env python3
"""SAM TUI Dashboard — monitors ~/.sam/registry.json with auto-refresh.

Usage:
  ~/.sam/bin/sam-tui            Full-screen TUI (auto-refresh every 2s)
  ~/.sam/bin/sam-tui --once     Print once and exit (for scripts)
  ~/.sam/bin/sam-tui --all      Include archived agents (hidden by default)

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

REGISTRY = os.path.expanduser("~/.sam/registry.json")
REFRESH_SEC = 2

LEGEND = ("Hints: failed! needs attention; unknown?stale = PID dead/recycled, "
          "no result.json (check logs/result). AGE = elapsed since created_at.")


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


def _fmt_age(entry):
    dt = _parse_ts(entry.get("created_at"))
    if dt is None:
        return "-"
    try:
        secs = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    except Exception:
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


def _fmt_state(state):
    if state == "failed":
        return "failed!"
    if state == "unknown":
        return "unknown?stale"
    return state or "?"


def _load_agents(show_archived=False):
    with open(REGISTRY) as f:
        data = json.load(f)
    agents = data.get("agents", [])
    if not show_archived:
        agents = [a for a in agents if not a.get("archived")]
    resolved = []
    for a in agents:
        entry = dict(a)
        entry["resolved_state"] = _resolve(a)
        resolved.append(entry)
    return resolved


def build_dashboard(show_archived=False):
    """Read registry.json and return a rich Layout with summary + agent table."""
    try:
        agents = _load_agents(show_archived)
    except FileNotFoundError:
        return Panel("Waiting for registry.json to appear...", title="SAM Dashboard", border_style="yellow")
    except json.JSONDecodeError:
        return Panel("registry.json is corrupt (invalid JSON)", title="SAM Dashboard", border_style="red")

    running = [a for a in agents if a["resolved_state"] == "running"]
    completed = [a for a in agents if a["resolved_state"] == "completed"]
    failed = [a for a in agents if a["resolved_state"] == "failed"]
    unknown = [a for a in agents if a["resolved_state"] == "unknown"]

    # ── Summary bar (resolved counts) ──
    parts = []
    if running:
        parts.append(f"  ▶ Running: [yellow]{len(running)}[/]")
    if completed:
        parts.append(f"  ✓ Completed: [green]{len(completed)}[/]")
    if failed:
        parts.append(f"  ✗ Failed: [red]{len(failed)}[/]")
    if unknown:
        parts.append(f"  ? Unknown: [yellow]{len(unknown)}[/]")
    parts.append(f"  ━ Total: {len(agents)}")
    summary = Panel("   ".join(parts), title="SAM Agents", border_style="blue")

    # ── Agent table: Agent State AGE Runs Harness Model ──
    table = Table(box=None, padding=(0, 1))
    table.add_column("", width=2)  # status icon
    table.add_column("Agent", style="cyan", no_wrap=True)
    table.add_column("State")
    table.add_column("AGE", justify="right")
    table.add_column("Runs", justify="right")
    table.add_column("Harness", style="dim")
    table.add_column("Model", style="dim")

    for a in agents:
        state = a["resolved_state"]
        if state == "running":
            icon, style = "▶", "yellow"
        elif state == "completed":
            icon, style = "✓", "green"
        elif state == "unknown":
            icon, style = "?", "yellow"
        else:
            icon, style = "✗", "red"

        runs = a.get("run_count")
        if runs is None:
            runs = a.get("run_id", "-")

        harness = a.get("harness") or ("agy" if a.get("conversation_id") else "pi")

        model = a.get("model", "") or ""
        if "/" in model:
            model = model.split("/")[-1]

        table.add_row(
            f"[{style}]{icon}[/]",
            a.get("name", "?"),
            f"[{style}]{_fmt_state(state)}[/]",
            _fmt_age(a),
            str(runs),
            harness,
            model,
        )

    # ── Layout ──
    layout = Layout()
    layout.split_column(
        Layout(summary, size=3),
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
