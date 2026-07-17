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
    parser = argparse.ArgumentParser(prog="sam", description="Sub-Agent Manager")
    parser.add_argument("--json", action="store_true", help="JSON output mode")
    parser.add_argument("--sam-home", default=None, help="Override SAM_HOME path")
    parser.add_argument("--debug", action="store_true", help="Enable debug tracebacks")

    # Line 4: Subparsers
    sub = parser.add_subparsers(dest="command", required=True)

    # Line 5: init
    p_init = sub.add_parser("init", help="Initialize SAM home directory")
    p_init.add_argument("--force", action="store_true", help="Rewrite config if exists")

    # Line 6: spawn
    p_spawn = sub.add_parser("spawn", help="Spawn a sub-agent")
    p_spawn.add_argument("--name", required=True, help="Agent name")
    p_spawn.add_argument("--task", required=True, help="Path to task file")
    p_spawn.add_argument("--model", default=None, help="Model override")
    p_spawn.add_argument("--cwd", default=None, help="Working directory")

    # Line 7: status
    p_status = sub.add_parser("status", help="Show agent state")
    p_status.add_argument("id_or_name", nargs="?", default=None, help="Agent ID or name")
    p_status.add_argument("--name", default=None, help="Agent name (alternative)")

    # Line 8: kill
    p_kill = sub.add_parser("kill", help="Kill a running agent")
    p_kill.add_argument("id_or_name", nargs="?", default=None, help="Agent ID or name")
    p_kill.add_argument("--name", default=None, help="Agent name (alternative)")

    # Line 9: wait
    p_wait = sub.add_parser("wait", help="Wait for agent completion")
    p_wait.add_argument("id_or_name", nargs="?", default=None, help="Agent ID or name")
    p_wait.add_argument("--name", default=None, help="Agent name (alternative)")
    p_wait.add_argument("--timeout", type=int, default=None, help="Max wait time in seconds")

    # Line 10: logs
    p_logs = sub.add_parser("logs", help="Show agent logs")
    p_logs.add_argument("id_or_name", nargs="?", default=None, help="Agent ID or name")
    p_logs.add_argument("--name", default=None, help="Agent name (alternative)")
    p_logs.add_argument("-n", type=int, default=50, help="Number of tail lines")
    p_logs.add_argument("--follow", "-f", action="store_true", help="Follow log output")
    p_logs.add_argument("--raw", action="store_true", help="Show sentinel markers")

    # Line 11: restart
    p_restart = sub.add_parser("restart", help="Restart a terminal agent")
    p_restart.add_argument("id_or_name", nargs="?", default=None, help="Agent ID or name")
    p_restart.add_argument("--name", default=None, help="Agent name (alternative)")

    # Line 12: Parse
    try:
        args = parser.parse_args()
    except SystemExit:
        raise  # Let argparse handle usage errors (exit 2)

    # Line 13: Apply --sam-home if provided
    if args.sam_home:
        os.environ["SAM_HOME"] = args.sam_home

    # Line 14-18: Lazy dispatch
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
