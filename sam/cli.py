#!/usr/bin/env python3
"""SAM CLI entry point — argparse dispatch, error mapping, JSON/human output.

Imports command modules lazily (only when the subcommand is invoked).
Platform check: Linux only.
"""

import argparse
import os
import sys


def main():
    """SAM CLI entry point. Parses args, dispatches to command module, maps errors.

    Line 1: Check sys.platform. If not "linux", error and exit 1.
    Lines 2-12: Argparse setup with subparsers for 7 commands.
    Lines 13-14: Set SAM_HOME from --sam-home if provided.
    Lines 15-18: Lazy-import and dispatch command.
    Lines 19-27: Error handling: SamError, KeyboardInterrupt, BrokenPipeError, generic.
    """
    # Line 1: Platform check
    if sys.platform != "linux":
        print("sam: unsupported platform — SAM requires Linux", file=sys.stderr)
        sys.exit(1)

    # Line 2-3: Parser and global flags
    # Use a two-pass approach:
    #   1. Parse global options BEFORE the subcommand using parse_known_args.
    #      This handles: sam --sam-home /tmp/test --json status <args>
    #   2. Subcommand parsers also define --json, --sam-home, --debug so they
    #      work AFTER the subcommand: sam status --json
    # The global values are applied first; subcommand values override if different.

    # Stage 1: Global parser (handles flags before the subcommand)
    parser = argparse.ArgumentParser(prog="sam", description="Sub-Agent Manager",
                                     add_help=False)
    parser.add_argument("--json", action="store_true", default=None,
                        help="JSON output mode")
    parser.add_argument("--sam-home", default=None,
                        help="Override SAM_HOME path")
    parser.add_argument("--debug", action="store_true", default=None,
                        help="Enable debug tracebacks")
    parser.add_argument("-h", "--help", action="store_true", default=None,
                        help="Show help and exit")

    # Parse known global args, leaving the rest for subcommand parsing
    global_args, remaining = parser.parse_known_args()

    # Apply global --sam-home immediately so subcommands can use sam.config
    if global_args.sam_home:
        os.environ["SAM_HOME"] = global_args.sam_home
    if global_args.debug:
        os.environ["SAM_DEBUG"] = "1"

    # Stage 2: Subcommand parser (handles flags after the subcommand)
    # All subcommands inherit --json, --sam-home, --debug via base_parser
    sub_parser = argparse.ArgumentParser(prog="sam", add_help=True)
    base_parser = argparse.ArgumentParser(add_help=False)
    base_parser.add_argument("--json", action="store_true", default=None,
                             help=argparse.SUPPRESS)
    base_parser.add_argument("--sam-home", default=None, help=argparse.SUPPRESS)
    base_parser.add_argument("--debug", action="store_true", default=None,
                             help=argparse.SUPPRESS)

    # If --help was requested globally (before subcommand), show main help
    if global_args.help and not remaining:
        sub_parser.print_help()
        sys.exit(0)

    sub = sub_parser.add_subparsers(dest="command", required=True)

    # Line 5-11: Add subcommands with parents=[base_parser]
    p_init = sub.add_parser("init", parents=[base_parser], help="Initialize SAM home directory")
    p_init.add_argument("--force", action="store_true", help="Rewrite config if exists")

    # Line 6: spawn
    p_spawn = sub.add_parser("spawn", parents=[base_parser], help="Spawn a sub-agent")
    p_spawn.add_argument("--name", required=True, help="Agent name")
    p_spawn.add_argument("--task", required=True, help="Path to task file")
    p_spawn.add_argument("--model", default=None, help="Model override")
    p_spawn.add_argument("--cwd", default=None, help="Working directory")

    # Line 7: status
    p_status = sub.add_parser("status", parents=[base_parser], help="Show agent state")
    p_status.add_argument("id_or_name", nargs="?", default=None, help="Agent ID or name")
    p_status.add_argument("--name", default=None, help="Agent name (alternative)")

    # Line 8: kill
    p_kill = sub.add_parser("kill", parents=[base_parser], help="Kill a running agent")
    p_kill.add_argument("id_or_name", nargs="?", default=None, help="Agent ID or name")
    p_kill.add_argument("--name", default=None, help="Agent name (alternative)")

    # Line 9: wait
    p_wait = sub.add_parser("wait", parents=[base_parser], help="Wait for agent completion")
    p_wait.add_argument("id_or_name", nargs="?", default=None, help="Agent ID or name")
    p_wait.add_argument("--name", default=None, help="Agent name (alternative)")
    p_wait.add_argument("--timeout", type=int, default=300,
                        help="Max wait time in seconds (default 300, 0 = wait forever)")

    # Line 10: logs
    p_logs = sub.add_parser("logs", parents=[base_parser], help="Show agent logs")
    p_logs.add_argument("id_or_name", nargs="?", default=None, help="Agent ID or name")
    p_logs.add_argument("--name", default=None, help="Agent name (alternative)")
    p_logs.add_argument("-n", type=int, default=50, help="Number of tail lines")
    p_logs.add_argument("--follow", "-f", action="store_true", help="Follow log output")
    p_logs.add_argument("--raw", action="store_true", help="Show sentinel markers")

    # Line 11: restart
    p_restart = sub.add_parser("restart", parents=[base_parser], help="Restart a terminal agent")
    p_restart.add_argument("id_or_name", nargs="?", default=None, help="Agent ID or name")
    p_restart.add_argument("--name", default=None, help="Agent name (alternative)")

    # Line 12: Parse remaining args with subcommand parser
    try:
        args = sub_parser.parse_args(remaining)
    except SystemExit:
        raise  # Let argparse handle usage errors (exit 2)

    # Merge global args into parsed args (subcommand values take priority)
    if global_args.json is not None and getattr(args, "json", None) is None:
        args.json = global_args.json
    if global_args.sam_home and not getattr(args, "sam_home", None):
        args.sam_home = global_args.sam_home
        os.environ["SAM_HOME"] = args.sam_home
    if global_args.debug and not getattr(args, "debug", None):
        args.debug = global_args.debug

    # Line 14-18: Lazy dispatch
    try:
        cmd = args.command

        # Ensure --json flag defaults to False for commands that check it
        if not hasattr(args, "json") or args.json is None:
            args.json = False
        if not hasattr(args, "debug") or args.debug is None:
            args.debug = False

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
        else:
            print(f"sam: unknown command: {cmd}", file=sys.stderr)
            sys.exit(2)

        exit_code = cmd_run(args)
        sys.exit(exit_code)

    except ImportError as e:
        # Line 15 variant: command module missing
        print(f"sam: command module not found: {e}", file=sys.stderr)
        sys.exit(1)

    except KeyboardInterrupt:
        # Line 23
        sys.exit(130)

    except BrokenPipeError:
        # Line 24
        sys.exit(0)

    except Exception as e:
        # Lines 19-22, 25-27: error handling
        is_json = getattr(args, "json", False)
        is_debug = getattr(args, "debug", False) or os.environ.get("SAM_DEBUG")

        if is_debug:
            import traceback
            traceback.print_exc()

        # Check for SamError and subclasses
        module_name = e.__class__.__module__
        class_name = e.__class__.__name__

        # Default exit code
        exit_code = 1
        error_id = "error"
        message = str(e)

        # Try to extract code and error_id from SamError-like exceptions
        if hasattr(e, "code"):
            exit_code = e.code
        if hasattr(e, "error_id"):
            error_id = e.error_id
        elif hasattr(e, "message"):
            message = e.message

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
