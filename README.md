# SAM — Sub-Agent Manager

SAM lets any harness or human spawn, track, resume, and restart scoped background pi/agy workers.
Prevents orphaned processes, lost PIDs, and unreliable `nohup` + `&` workflows.

**Linux only. Python 3.9+; core is stdlib-only, zero dependencies** (the optional `sam-tui` dashboard needs `rich`).

> **Are you an AI agent? Stop reading this and read [`SKILL.md`](SKILL.md) instead.**

---

## Install

```bash
pip install git+https://github.com/DinithaSasinduDissanayake/sam-agent.git
```

## Quick start

```bash
sam init
sam spawn --name auth-review-n1 --task task.md --json
# Return now; check later when you need the output:
sam status auth-review-n1 --json
sam result auth-review-n1
# Continue the same history as another background run:
sam resume auth-review-n1 --task followup.md --json
```

## Commands

| Command | Purpose |
|---------|---------|
| `init` | Initialize SAM home directory |
| `spawn` | Spawn a sub-agent (accepts `--model`, `--thinking <level>` pi-only, `--effort <level>` agy-only, `--override-reason`, `--no-space`) |
| `status` | Show agent state & liveness (`active`/`idle`/`alive`/`working`/`<state>` with `movement` side-field: `moving`/`still`/`null`; `--detail`, `--watch`) |
| `kill` | Kill a running agent (cancels a queued `awaiting_retry` instead of signaling) |
| `wait` | Wait for agent completion (runs in `awaiting_retry` omit `exit_code` as lifecycle continues; `--timeout 0` default = wait forever; `--kill-after N` = terminate exit 4) |
| `logs` | Show agent logs |
| `restart` | Restart a terminal agent (same task; accepts `--thinking`/`--effort`, `--override-reason`, `--no-space`) |
| `resume` | Continue a terminal agent with a new task (accepts `--model`, `--thinking`/`--effort`, `--override-reason`, `--no-space`) |
| `result` | Print final result (structured fallback for old runs; `unavailable`, never a prior answer) |
| `retry` | Fire / cancel / list SAM-owned infra-retries (429 + startup-network queue; exit 6 on deferral) |
| `doctor` | Audit `$SAM_HOME/launches.jsonl` audit log, spawn spacing, concurrency cap 4, `--override-reason`, and retry queue (`FAIL-OPEN`: exits 0 if audit log missing) |
| `prune` / `unprune` | Archive / restore terminal agents (never deletes) |
| `skill` | Print SKILL.md |

All commands accept `--json` for machine-readable output and `--sam-home <path>` to override the data directory.

Spawn-and-forget is the default. Invoking agents must not automatically wait,
sleep, or poll in the spawning turn. Use `sam wait` only when explicitly asked
or when the user requires consuming the output before returning; explain that
it blocks the turn. Inspect compact `status` / `result` before debugging logs.

Live dashboard: `sam-tui` (continuous auto-refresh; needs `rich`), recent running
and finished workers by default, `--all` for history, `--archived` for archive-only.
`sam status --watch 1 --json` takes two samples, then exits. AGE uses the
authoritative current-run start; DONE uses actual finish time (`-` if unknown).
Resuming/restarting restores an archived worker after its new PID is persisted.

Defaults: pi `opencode/muse-spark-1.3-contributor-free`, agy
`gemini-3.8-flash-low`. Spawn precedence: `--model` → `SAM_MODEL` →
per-harness config → builtin. Resume keeps its stored model unless overridden;
restart keeps its stored model. Pi `--thinking` and agy `--effort` are optional,
harness-specific overrides, not a model allowlist.

The Muse free provider currently rejects non-OpenCode clients, including pi.
The configured pi default therefore may fail; select a model/provider supported
by your pi installation with `--model` or `SAM_MODEL`. Native OpenCode support
is planned for the next stage and is not implemented here. No live inference
is needed to inspect or manage SAM workers.

## How it works

- Agents are tracked in a JSON registry with flock-based locking
- Each agent gets its own directory with run-NNN/ history
- Exit codes 0–8 plus 130 for scriptable handling (0=ok, 1=error/failed-rendezvous, 2=name/flag misuse, 3=not found, 4=wait `--kill-after` exceeded, 5=wait/resume not found or `already_queued`, 6=not terminal or launch `deferred` [spawn, resume, restart, retry], 7=max restarts, 8=lock timeout; see SKILL.md for the per-command table)

## Limitations (v0.1)

- Linux only. Relies on `/proc/<pid>/stat`, `fcntl.flock`, `os.killpg`.
- No daemon mode. Agents run as background processes. Orphans possible if parent crashes.
- No automatic recovery. A worker that misses its 30s launch window resolves `failed`; manual `sam status` and `sam restart` required.
- No SAM execution timeout: agy explicitly uses `--print-timeout 0s`. `sam wait --timeout 0` (the default) waits indefinitely; a nonzero `--timeout` is deprecated and detaches without touching the worker; termination-on-expiry requires the explicit `--kill-after N`.
- Environment hygiene: Strips SSH session variables (`SSH_CLIENT`, `SSH_CONNECTION`, `SSH_TTY`) and unsets invalid `DBUS_SESSION_BUS_ADDRESS` to prevent interactive keyring or headless hangs.
- Old-run final fallback is read-only: agy structured response; pi active-branch final within authoritative start/end timestamps. Unattributable output is unavailable, never replaced by a prior answer.
- Disk space grows with per-run task/log/result history. Prune only archives; it does not delete history.

## License

MIT
