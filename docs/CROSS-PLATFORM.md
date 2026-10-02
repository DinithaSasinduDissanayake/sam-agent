# SAM on Windows and Linux (cross-platform notes)

SAM runs on Linux and on native Windows (no WSL). The CLI and its verbs are the same on both.

## Harnesses

`--harness pi | agy | opencode | claude | codex` on `spawn`, `resume`, `restart`.

| Harness | Reasoning flag | Default model (config key) | Prompt goes in via |
|---|---|---|---|
| pi | `--thinking` | Linux `opencode/muse-spark-1.3-contributor-free`, Windows `nvidia/meta/muse-glimmer-30b` (`defaults.model`) | `@taskfile` |
| agy | `--effort` | `gemini-3.8-flash-low` (`defaults.agy_model`) | one command-line argument (max 30000 characters) |
| opencode | `--effort` (-> `--variant`) | `opencode/muse-spark-1.3-contributor-free` (`defaults.opencode_model`) | stdin |
| claude | `--effort` | `sonnet` (`defaults.claude_model`) | stdin |
| codex | `--effort` | codex's own default (`defaults.codex_model`, `default` = do not pass `-m`) | stdin |

Every harness is started with its "never ask for permission" switch (`--auto`, `--dangerously-skip-permissions`,
`--dangerously-bypass-approvals-and-sandbox`): a headless agent cannot answer prompts.

## One runner per agent

On Windows every agent, and on both systems every opencode / claude / codex agent, is run by `sam/runner.py`:
one small process per agent, no daemon. (On Linux pi and agy still use the legacy wrapper scripts unless
`SAM_RUNNER=generic` is set.) The runner starts the harness, streams its output to `output.log`, watches for hangs
and writes `result.json` and `runner.json` (a breadcrumb: `starting` / `running` / `finished`) in the run directory.

**A dead runner always takes its agent down with it.** On Windows the runner lives in its own kill-on-close Job
Object (`sam-job-<runner pid>`); on Linux the harness is started with a parent-death signal. Nothing keeps running
(and burning quota) unseen. `sam status NAME` then shows `unknown` plus the breadcrumb; continue with
`sam resume NAME --task <file>` (same session for agy / opencode / claude / codex; same session file for pi).

### What can still stop an agent involuntarily

| Event | What happens | State afterwards | What to do |
|---|---|---|---|
| The shell, terminal, console or orchestrator session that ran `sam spawn` exits, is closed or is tree-killed | nothing: the runner is detached (new process group, no console, outside the caller's Job Object; through WMI when the caller's job forbids breakaway) | still `running` | - |
| `sam kill`, `sam wait --kill-after` | whole tree killed | `killed` | - |
| Runner process killed (Task Manager, `taskkill`, antivirus, out of memory) | whole tree killed with it | `unknown` + breadcrumb | `sam resume` |
| Windows sign-out, shutdown, reboot, power loss | all processes end | `unknown` + breadcrumb | `sam resume` after sign-in |
| Sleep / hibernate | everything is frozen and continues on wake; the watchdog ignores the time slept; network connections of the harness are usually dead afterwards | `running`, then normally `failed` with a network error, or a watchdog kill after the idle timeout | `sam resume` (or the SAM retry queue when the error is classed as infra) |
| Harness prints nothing at all for `first_output_timeout` (300 s; opencode 600 s) | watchdog kills the harness | `failed`, `error_kind: watchdog_first_output`, `infra_hint: startup-network` | SAM retry queue / `sam resume` |
| No new output AND no CPU use for `idle_timeout` (3600 s) | watchdog kills the harness | `failed` or `partial`, `error_kind: watchdog_idle` | `sam resume` |
| Harness exits by itself with an error (quota, 503, auth, crash) | - | `failed` / `partial` / `awaiting_retry` | per message |
| Disk full | `result.json` cannot be written | `unknown` | free space, `sam resume` |

Timeouts: `SAM_FIRST_OUTPUT_TIMEOUT_S`, `SAM_IDLE_TIMEOUT_S`, `SAM_EXIT_GRACE_S` (seconds; `0` disables the first two).

## Many agents

Launches stay spaced 15 s apart. The running cap is `SAM_MAX_RUNNING`, else `defaults.max_running` in
`config.json`, else 4. For 10-12 agents set `"max_running": 12`. Each agent is independent: its own runner, its own
Job Object, its own run directory; the registry is only touched by short `sam` commands under a file lock.

## Workspaces

`sam spawn` without `--cwd` runs the agent in the task file's directory. If that directory is a temp directory
(`%TEMP%`, `/tmp`, ...) the agent instead gets the persistent `<SAM_HOME>/workspaces/<name>/`. A default workspace
is therefore never wiped by a reboot. `SAM_WORKSPACE_MODE=task-dir` restores the old rule.

## One dashboard for several machines

Every registry entry and `result.json` carries `host`. `sam status --json` prints a JSON list of entries.

    sam status --all --remote sithu-cf              # local agents + the agents of ssh host "sithu-cf"
    ssh sithu-cf "sam status --json --all"          # what --remote runs on the other side

`--remote` may be repeated. An unreachable host is a warning, not an error.

## Windows specifics

- Install: `python -m pip install -e <repo>` (needs `psutil`, installed automatically). `sam` is then on PATH for
  PowerShell, cmd, Git Bash and ssh sessions.
- SAM home: `C:\Users\<you>\.sam` (override with `SAM_HOME`).
- npm-installed CLIs (`opencode`, `claude`, `pi`, `codex`) are `.cmd` shims; SAM starts the real `.exe` / `node`
  script behind them, because a `.cmd` cuts a multi-line prompt at the first newline.
- There is no graceful stop on Windows: every kill is a hard kill of the whole Job Object.
- `SAM_<HARNESS>_BIN` (for example `SAM_OPENCODE_BIN`) overrides the executable.
- The legacy test suite (`tests/test_*.py`) is POSIX-only and is skipped on Windows; `tests/xplat/` runs on both.

## Coming from wsam (temporary Windows launcher)

Same verbs. `wsam spawn NAME TASK.md --harness opencode --cwd DIR` becomes
`sam spawn --name NAME --task TASK.md --harness opencode --cwd DIR`. `status`, `wait`, `result`, `logs`, `kill`
take the name the same way. Differences: `sam wait NAME` blocks until done (use `sam status NAME` to peek;
`--timeout N` detaches); a full queue makes `sam spawn` exit 6 with "wait and re-run" instead of queueing silently;
`sam resume` / `sam restart` / `sam retry` exist. State lives in `~/.sam`, not `~/.wsam`; wsam's old agents are
not imported.
