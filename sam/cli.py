#!/usr/bin/env python3
"""SAM CLI entry point — argparse dispatch, error mapping, JSON/human output.

Imports command modules lazily (only when the subcommand is invoked).
Platform check: Linux only.
"""

import argparse
import os
import sys


def main():
    if sys.platform != "linux":
        print("sam: unsupported platform — SAM requires Linux", file=sys.stderr)
        sys.exit(1)

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
    p_spawn.add_argument("--cwd", default=None, help="Working directory")

    p_status = sub.add_parser("status", parents=[base_parser], help="Show agent state")
    p_status.add_argument("id_or_name", nargs="?", default=None, help="Agent ID or name")
    p_status.add_argument("--name", default=None, help="Agent name (alternative)")
    p_status.add_argument("--all", action="store_true", help="Show all agents including terminal")

    p_kill = sub.add_parser("kill", parents=[base_parser], help="Kill a running agent")
    p_kill.add_argument("id_or_name", nargs="?", default=None, help="Agent ID or name")
    p_kill.add_argument("--name", default=None, help="Agent name (alternative)")

    p_wait = sub.add_parser("wait", parents=[base_parser], help="Wait for agent completion")
    p_wait.add_argument("id_or_name", nargs="?", default=None, help="Agent ID or name")
    p_wait.add_argument("--name", default=None, help="Agent name (alternative)")
    p_wait.add_argument("--timeout", type=int, default=300,
                        help="Max wait time in seconds (default 300, 0 = wait forever)")

    p_logs = sub.add_parser("logs", parents=[base_parser], help="Show agent logs")
    p_logs.add_argument("id_or_name", nargs="?", default=None, help="Agent ID or name")
    p_logs.add_argument("--name", default=None, help="Agent name (alternative)")
    p_logs.add_argument("-n", type=int, default=50, help="Number of tail lines")
    p_logs.add_argument("--follow", "-f", action="store_true", help="Follow log output")
    p_logs.add_argument("--raw", action="store_true", help="Show sentinel markers")

    p_restart = sub.add_parser("restart", parents=[base_parser], help="Restart a terminal agent")
    p_restart.add_argument("id_or_name", nargs="?", default=None, help="Agent ID or name")
    p_restart.add_argument("--name", default=None, help="Agent name (alternative)")

    # v0.1.1: skill — print SKILL.md for AI agents
    p_skill = sub.add_parser("skill", parents=[base_parser], help="Print SKILL.md for AI agents")

    # v0.1.1: prune — remove terminal agents
    p_prune = sub.add_parser("prune", parents=[base_parser], help="Remove terminal agents from registry")

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
        is_json = args.json
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