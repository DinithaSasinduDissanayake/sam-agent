#!/usr/bin/env python3
"""SAM CLI entry point — argparse dispatch, error mapping, JSON/human output.

Imports command modules lazily (only when the subcommand is invoked).
Platform check: Linux only.
"""

import argparse
import os
import sys


def main():
    if sys.platform not in ("linux", "win32"):
        print("sam: unsupported platform — SAM requires Linux or Windows", file=sys.stderr)
        sys.exit(1)
    if sys.platform == "win32":
        # Console default is cp1252: never crash on non-ASCII agent output.
        for _stream in (sys.stdout, sys.stderr):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass

    # Base parser with shared global flags (add_help=False to avoid duplicate -h)
    base_parser = argparse.ArgumentParser(add_help=False)
    base_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                             help="JSON output mode")
    base_parser.add_argument("--sam-home", default=argparse.SUPPRESS,
                             help="Override SAM_HOME path")
    base_parser.add_argument("--debug", action="store_true", default=argparse.SUPPRESS,
                             help="Enable debug tracebacks")

    # Main parser inherits base_parser flags and adds its own help
    parser = argparse.ArgumentParser(prog="sam", description="Sub-Agent Manager",
                                     parents=[base_parser])
    sub = parser.add_subparsers(dest="command", required=True)

    # Subcommands — each inherits base_parser flags so --json works after subcommand
    p_init = sub.add_parser("init", parents=[base_parser], help="Initialize SAM home directory")
    p_init.add_argument("--force", action="store_true", help="Rewrite config if exists")

    p_spawn = sub.add_parser("spawn", parents=[base_parser], help="Spawn a sub-agent")
    p_spawn.add_argument("--name", required=True, help="Agent name")
    p_spawn.add_argument("--task", required=True, help="Path to task file")
    p_spawn.add_argument("--model", default=None, help="Model override")
    p_spawn.add_argument("--thinking", default=None, choices=["off", "minimal", "low", "medium", "high", "xhigh", "max"],
                         help="Thinking/reasoning level for model (off, minimal, low, medium, high, xhigh, max)")
    p_spawn.add_argument("--harness", default=None, choices=["pi", "agy", "opencode", "claude", "codex"],
                         help="Harness wrapper to use (default $SAM_HARNESS, config defaults.harness, or pi)")
    p_spawn.add_argument("--effort", default=None,
                         help="Effort level (agy, opencode, claude, codex); pi uses --thinking")
    p_spawn.add_argument("--cwd", default=None, help="Working directory")
    p_spawn.add_argument("--no-space", action="store_true",
                         help="Experiment-only: bypass the global ≥15s launch-spacing "
                              "guard (e.g. instrumented burst canaries). The launch is "
                              "still timestamped. Never use for real work.")
    p_spawn.add_argument("--override-reason", default=None,
                         help="Force-launch during an active 429 quota window "
                              "(reason is logged to the registry and shown by "
                              "sam doctor). Reason required.")

    p_status = sub.add_parser("status", parents=[base_parser], help="Show agent state")
    p_status.add_argument("id_or_name", nargs="?", default=None, help="Agent ID or name")
    p_status.add_argument("--name", default=None, help="Agent name (alternative)")
    p_status.add_argument("--all", action="store_true", help="Show all agents including terminal and archived (full list)")
    p_status.add_argument("--archived", action="store_true", help="Show only archived agents")
    p_status.add_argument("--limit", type=int, default=None,
                          help="Max agents to list (default 10 newest, --all for full)")
    p_status.add_argument("--fields", default=None,
                          help="Comma list for --json (e.g. name,state,elapsed)")
    p_status.add_argument("--remote", action="append", default=None, metavar="SSH_HOST",
                          help="Also list agents of another machine (runs `sam status --json` "
                               "there over ssh; repeatable)")
    p_status.add_argument("--detail", action="store_true",
                          help="Show detailed activity signals (read-only, opt-in)")
    p_status.add_argument("--watch", nargs="?", const=5, type=int, default=None,
                          metavar="SECONDS",
                          help="Two-sample byte deltas over SECONDS (1-30, default 5); implies --detail")
    p_status.add_argument("--stall-seconds", type=int, default=300,
                          help="Seconds before a verified-live agent is silent (reported `alive (no task signal Xm)`, never bare stalled; default 300)")

    p_kill = sub.add_parser("kill", parents=[base_parser], help="Kill a running agent")
    p_kill.add_argument("id_or_name", nargs="?", default=None, help="Agent ID or name")
    p_kill.add_argument("--name", default=None, help="Agent name (alternative)")

    p_wait = sub.add_parser("wait", parents=[base_parser], help="Wait for agent completion")
    p_wait.add_argument("id_or_name", nargs="?", default=None, help="Agent ID or name")
    p_wait.add_argument("--name", default=None, help="Agent name (alternative)")
    p_wait.add_argument("--timeout", type=int, default=0,
                        help="0 (default) = wait forever; nonzero = DEPRECATED "
                             "detach (exit 0, agent untouched) — use --timeout 0 "
                             "to block, sam status to peek, or --kill-after N "
                             "to terminate on expiry")
    p_wait.add_argument("--kill-after", type=int, default=None, metavar="N",
                        help="Explicit kill opt-in: if still not terminal after "
                             "N seconds, SIGTERM→SIGKILL, exit 4, state killed. "
                             "0 = no bound")

    p_logs = sub.add_parser("logs", parents=[base_parser], help="Show agent logs")
    p_logs.add_argument("id_or_name", nargs="?", default=None, help="Agent ID or name")
    p_logs.add_argument("--name", default=None, help="Agent name (alternative)")
    p_logs.add_argument("-n", type=int, default=50, help="Number of tail lines")
    p_logs.add_argument("--follow", "-f", action="store_true", help="Follow log output")
    p_logs.add_argument("--raw", action="store_true", help="Show sentinel markers")

    p_restart = sub.add_parser("restart", parents=[base_parser], help="Restart a terminal agent")
    p_restart.add_argument("id_or_name", nargs="?", default=None, help="Agent ID or name")
    p_restart.add_argument("--name", default=None, help="Agent name (alternative)")
    p_restart.add_argument("--harness", default=None, choices=["pi", "agy", "opencode", "claude", "codex"],
                           help="Harness wrapper to use (default stored harness, $SAM_HARNESS, config, or pi)")
    p_restart.add_argument("--thinking", default=None, choices=["off", "minimal", "low", "medium", "high", "xhigh", "max"],
                           help="Thinking/reasoning level override for model (pi only)")
    p_restart.add_argument("--effort", default=None,
                           help="Effort level for agy harness (agy only)")
    p_restart.add_argument("--override-reason", default=None,
                           help="Override active 429 quota window with logged reason")
    p_restart.add_argument("--no-space", action="store_true",
                           help="Bypass launch spacing (experiment only)")

    # v0.1.1: skill — print SKILL.md for AI agents
    p_skill = sub.add_parser("skill", parents=[base_parser], help="Print SKILL.md for AI agents")

    # v0.1.1: prune — hide terminal agents (archived flag, never deletes)
    p_prune = sub.add_parser("prune", parents=[base_parser], help="Archive terminal agents (hide, never deletes)")
    p_prune.add_argument("id", nargs="?", default=None, help="Agent ID or name to archive")
    p_prune.add_argument("--all", action="store_true", help="Archive all terminal agents")

    p_unprune = sub.add_parser("unprune", parents=[base_parser], help="Restore an archived agent")
    p_unprune.add_argument("id", help="Agent ID or name to restore")

    # v0.1.2: resume — continue agent session with new task
    p_resume = sub.add_parser("resume", parents=[base_parser], help="Resume terminal agent with new task")
    p_resume.add_argument("id_or_name", nargs="?", default=None, help="Agent ID or name")
    p_resume.add_argument("--name", default=None, help="Agent name (alternative)")
    p_resume.add_argument("--task", required=True, help="Path to new task file")
    p_resume.add_argument("--model", default=None, help="Model override")
    p_resume.add_argument("--thinking", default=None, choices=["off", "minimal", "low", "medium", "high", "xhigh", "max"],
                         help="Thinking/reasoning level override for model")
    p_resume.add_argument("--harness", default=None, choices=["pi", "agy", "opencode", "claude", "codex"],
                          help="Harness wrapper to use (default stored harness, $SAM_HARNESS, config, or pi)")
    p_resume.add_argument("--effort", default=None,
                          help="Effort level for agy harness (agy only)")
    p_resume.add_argument("--override-reason", default=None,
                          help="Override active 429 quota window with logged reason")
    p_resume.add_argument("--no-space", action="store_true",
                          help="Bypass launch spacing (experiment only)")

    p_result = sub.add_parser("result", parents=[base_parser], help="Print agent final result")
    p_result.add_argument("id_or_name", nargs="?", default=None, help="Agent ID or name")
    p_result.add_argument("--name", default=None, help="Agent name (alternative)")

    # v0.2: retry — SAM-owned infra-retry queue (fire/cancel/list)
    p_retry = sub.add_parser("retry", parents=[base_parser],
                             help="Fire/cancel/list SAM-owned infra-retries")
    p_retry.add_argument("id_or_name", nargs="?", default=None,
                         help="Agent ID or name (omit to list the queue)")
    p_retry.add_argument("--name", default=None, help="Agent name (alternative)")
    p_retry.add_argument("--cancel", action="store_true",
                         help="Dequeue; agent -> killed (retry_cancelled)")
    p_retry.add_argument("--due", action="store_true",
                         help="Fire every due queued retry")
    p_retry.add_argument("--override-reason", default=None,
                         help="Fire before not_before (reason is logged)")

    # v0.2: doctor — offline spacing/concurrency/override audit
    p_doctor = sub.add_parser("doctor", parents=[base_parser],
                              help="Audit spawn spacing, concurrency, overrides, queue")
    p_doctor.add_argument("--window", nargs="?", const=24.0, type=float,
                          default=24.0, metavar="HOURS",
                          help="Look-back window in hours (default 24)")

    # Parse everything at once — argparse handles help natively
    args = parser.parse_args()

    # Apply --sam-home immediately so subcommands can use sam.config
    if hasattr(args, "sam_home") and args.sam_home:
        os.environ["SAM_HOME"] = args.sam_home

    # Lazy dispatch
    try:
        cmd = args.command

        if cmd == "init":
            from sam.commands.init_cmd import run as cmd_run
        elif cmd == "spawn":
            from sam.commands.spawn import run as cmd_run
        elif cmd == "status":
            from sam.commands.status import run as cmd_run
        elif cmd == "kill":
            from sam.commands.kill import run as cmd_run
        elif cmd == "wait":
            from sam.commands.wait import run as cmd_run
        elif cmd == "logs":
            from sam.commands.logs import run as cmd_run
        elif cmd == "restart":
            from sam.commands.restart import run as cmd_run
        elif cmd == "skill":
            # Read SKILL.md from package or filesystem and print to stdout
            import pkgutil
            data = pkgutil.get_data(__package__ or "sam", "../SKILL.md")
            if data is None:
                import pathlib
                skill_path = pathlib.Path(__file__).resolve().parent.parent / "SKILL.md"
                data = skill_path.read_bytes()
            sys.stdout.buffer.write(data)
            sys.exit(0)
        elif cmd == "prune":
            from sam.commands.prune import run as cmd_run
        elif cmd == "unprune":
            from sam.commands.unprune import run as cmd_run
        elif cmd == "resume":
            from sam.commands.resume import run as cmd_run
        elif cmd == "result":
            from sam.commands.result import run as cmd_run
        elif cmd == "retry":
            from sam.commands.retry import run as cmd_run
        elif cmd == "doctor":
            from sam.commands.doctor import run as cmd_run
        else:
            print(f"sam: unknown command: {cmd}", file=sys.stderr)
            sys.exit(2)

        exit_code = cmd_run(args)
        sys.exit(exit_code)

    except ImportError as e:
        print(f"sam: command module not found: {e}", file=sys.stderr)
        sys.exit(1)

    except KeyboardInterrupt:
        sys.exit(130)

    except BrokenPipeError:
        sys.exit(0)

    except Exception as e:
        is_json = getattr(args, "json", False)
        is_debug = args.debug or os.environ.get("SAM_DEBUG")

        if is_debug:
            import traceback
            traceback.print_exc()

        exit_code = getattr(e, "code", 1)
        error_id = getattr(e, "error_id", "error")
        message = str(e)

        if is_json:
            import json
            print(json.dumps({
                "status": "error",
                "code": exit_code,
                "error": error_id,
                "message": message,
            }), file=sys.stderr)
        else:
            print(f"Error: {message}", file=sys.stderr)

        sys.exit(exit_code)


if __name__ == "__main__":
    main()