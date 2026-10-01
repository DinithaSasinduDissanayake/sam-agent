---
name: sam
description: Spawn and resume detached pi/agy workers via SAM CLI from any invoker. Use when asked to run background workers, continue a worker with a follow-up task, or inspect worker status and final results. Spawn-and-forget is the default.
---
# SKILL.md — SAM: Sub-Agent Manager

## What is SAM?

SAM is a CLI tool that lets you (an AI agent, human operator, or any CLI invoker) spawn
background sub-agents,
wait for them to finish, read their output, and kill or restart them.

Each sub-agent is an independent `pi`/`agy` worker process running a task you define in a
markdown file. SAM tracks process state, captures output, and handles cleanup.

**Platform:** Linux only. Python 3.9+. Core CLI is stdlib-only, zero
dependencies. The optional `sam-tui` dashboard requires `rich`
(`sam status --detail` exposes activity without it).

---

## Quick Start (spawn-and-forget)

Spawn-and-forget is the default. Do NOT auto-wait after spawn — the parent/invoking process
keeps working. Do not sleep or poll in the invoking turn. Wait only when the
user explicitly requests it or requires consuming worker output before returning;
explain that it blocks the turn. A worker receives a scoped task, not a special persona.

```bash
# 1. Spawn and keep working (no auto-wait)
sam spawn --name research-auth-n1 --task ./tasks/refactor-auth.md --cwd /home/user/project

# 2. At a later useful check, inspect compact status/final output
sam status research-auth-n1 --json
sam result research-auth-n1
# 3. Continue the same history (terminal worker required), then return
sam resume research-auth-n1 --task ./tasks/followup.md --json
```

Name agents with long kebab-case `<area>-<task>-<n>`
(e.g. `research-auth-n1`, `frozen-meatballs-prices-n2`). Never reuse a
non-terminal agent's name.

---

## The Contract

1. **`sam spawn`** — Creates a sub-agent process, assigns it a stable `<area>-<task>-<n>` kebab name, copies your task file, launches it in the background. Forget it; keep working.
2. **`sam wait`** — Optional blocking rendezvous. Its observation timeout attempts worker termination; this is not a passive status call. Read JSON status and the nested result.
3. **`sam result`** — Final-only output text. Cheapest read after `status`.
4. **`sam logs -n 50`** — Full stream tail when `result` is not enough. Sentinels like `##PI_BEGIN_...` are stripped by default.
5. **`sam resume`** — Tier-1 alongside `status`/`result`: continue a
   *terminal* agent with a new task as a new background run (never
   interactive, never attached). Pi continues its session file; agy
   continues its conversation pointer. Example:
   ```bash
   sam resume research-auth-n1 --task ./tasks/followup.md --json
    # return now; inspect status/result later
   ```

---

## Commands

### `sam init`

Initialize SAM home. Run once after installation.

```bash
sam init [--force]
```

| Flag | Effect |
|------|--------|
| `--force` | Rewrite config + wrapper. Never deletes agents or registry. |

### `sam spawn`

Start a background sub-agent.

```bash
sam spawn --name <name> --task <path> [--cwd <dir>] [--model <model>] [--thinking <level>] [--harness <pi|agy>] [--effort <level>]
```

| Flag | Required | Description |
|------|----------|-------------|
| `--name` | Yes | Unique name for this agent `^[a-zA-Z0-9_-]{1,64}$` |
| `--task` | Yes | Path to task markdown file |
| `--cwd` | No | Working directory (default: task file's parent directory) |
| `--model` | No | Model override (precedence: `--model` flag → `$SAM_MODEL` → config per-harness default → builtin default: pi `opencode/muse-spark-1.3-contributor-free`, agy `gemini-3.8-flash-low`) |
| `--thinking` | No | Thinking/reasoning level, **pi only** (`off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`); rejected with `agy` |
| `--harness` | No | Harness wrapper (`pi`, `agy`; default: `pi` via $SAM_HARNESS or config defaults.harness) |
| `--effort` | No | Effort level, **agy only** (`low`, `medium`, `high`, `max`); rejected without `agy` |
| `--override-reason` | No | Force-launch during an open 429 quota window (reason logged; visible in `sam doctor`). Reason required |

**Output:** Agent ID like `sam-20260716-103042-a1b2c3`.

**Deferrals (not errors):** spawn may exit `6` `deferred` while the global
≥15 s launch-spacing slot or a model's 429 quota window is open. The message
says `NOT an error`: sleep `retry_after_s` and re-run the same spawn, or pass
`--override-reason`. Exit `5` `already_queued` means that name already has a
pending infra-retry (wait, `sam retry <name> --cancel`, or pick another name).

**Agy example:**
```bash
sam spawn --name research-auth --task /tmp/task-refactor.md --harness agy --effort high
```

### `sam status`

Check agent state. Lean by default: last 10 non-terminal agents,
newest-first, columns `NAME STATE AGE` (no PID/ID noise).

```bash
sam status [<id-or-name>] [--json] [--all] [--limit N] [--detail] [--watch [SECONDS]]
```

| Flag | Effect |
|------|--------|
| `--all` | Full list incl. terminal + archived |
| `--limit N` | Max rows (overrides default 10 and `--all`) |
| `--detail` | Activity layer (tokens/progress or null with reason) |
| `--watch [SECONDS]` | Two-sample byte deltas over SECONDS (1–30, default 5), then exits; implies `--detail`. Not a live view — for continuous monitoring use `sam-tui` |

States: `spawning`, `running`, `completed`, `failed`, `killed`, `partial`,
`awaiting_retry`, `unknown`.
Resolved dynamically; terminal states are written back to the registry, and
`exit_code`/`duration_ms` are backfilled from `result.json` (registry-only
readers see the final exit code without opening the result file).
`partial` = failed run with captured deliverable text (see `PARTIAL.md`);
`awaiting_retry` = infra death (429 quota / startup-network) queued for
SAM-owned relaunch — fire with `sam retry`, cancel with `sam retry --cancel`
(or `sam kill`); `sam status <name>` prints the fires-at time.
`AGE` = elapsed since the current run started (`run_started_at`, original
`created_at` kept for history). Wrapper `started_at` takes precedence; missing
start time for an older resumed run shows `-` rather than its original age.

Live dashboard: `sam-tui` (continuous auto-refresh every 2s, `q` to quit;
`--once` for a single snapshot, `--all` for history, `--archived` for archive-only). `sam status
--watch` takes exactly two samples and exits. Both resolve states the same
way; both hide archived entries by default. There is no notify daemon —
poll with `status` or rendezvous with `wait`. The TUI `DONE` column shows
time since the current run's `result.json` `ended_at`, or authoritative
registry `ended_at`/`completed_at` (`-` when unknown,
never guessed); `Model` shows pi `[thinking]` / agy `[effort:X]`
(`[unknown]` when a pi level was never recorded).

### `sam result`

Print an agent's final output only (from `result.json`).

```bash
sam result <id-or-name> [--json]
```

Read tiers, cheapest first: `status` → `result` → `logs -n 50`.
`result` is final-only text; reach for `logs` only when you need the full
stream. `logs` tails 50 lines by default (`-n`), sentinels stripped unless
`--raw`. No transcript is printed by default — `result` never dumps the
full log. For old completed runs without a persisted result, `result`
falls back to a read-only structured extraction (agy response envelope;
pi active-branch final with timestamps inside authoritative run start/end).
Pi text without an attributable current-run record reports `unavailable`
rather than a previous run's answer. `still running` vs `unavailable` are
distinct messages.

### `sam wait`

Block until an agent reaches a terminal state.

```bash
sam wait <id-or-name> [--timeout <seconds>] [--kill-after <seconds>] [--json]
```

| Flag | Description |
|------|-------------|
| `--timeout <seconds>` | **0 (default) = wait forever.** Nonzero is **deprecated and detaches**: returns immediately with exit 0, state unchanged, warning on stderr — it never waits and never signals. Migrate to `--timeout 0` (real wait) or `sam status` (peek). |
| `--kill-after <seconds>` | Explicit kill opt-in (the old timed-wait semantics under a safe name): if still not terminal after N seconds, SIGTERM → SIGKILL, exit 4, state `killed`. `0` = no bound. Takes precedence over a nonzero `--timeout`. |

Distinct from harness execution limits: agy is explicitly unlimited (`--print-timeout 0s`).

**Exit codes:**
- 0 = completed, killed, unknown, partial, or awaiting_retry (read JSON `status` field to tell them apart); also a deprecated `--timeout N` detach (JSON `detached:true`, state `running`)
- 1 = failed (read JSON `status`/`exit_code`) or lock/error
- 4 = `--kill-after N` exceeded — termination attempted, state persisted as `killed`
- 5 = not found

**JSON output:**
```json
{"status":"completed","agent_id":"sam-...","exit_code":0,"result":{...},"elapsed_seconds":12.34}
```

### `sam kill`

Terminate a running agent.

```bash
sam kill <id-or-name>
```

Sends SIGTERM → waits 5s → sends SIGKILL if needed. `sam kill <unknown>` marks dead-PID unknown as `killed` (no signal if proc dead/recycled). On `awaiting_retry` it cancels the queued retry instead (no signal; state → `killed`, reason `retry_cancelled`).

### `sam logs`

View agent output.

```bash
sam logs <id-or-name> [-n <lines>] [--follow] [--raw]
```

| Flag | Description |
|------|-------------|
| `-n <lines>` | Number of tail lines (default: 50) |
| `--follow`, `-f` | Stream new output in real-time |
| `--raw` | Show `##PI_...` sentinel markers |

Sentinels are stripped by default for clean output.

### `sam resume`

Continue a terminal agent with a new task as a new background run.

```bash
sam resume <id-or-name> --task <path> [--model <model>] [--thinking <level> | --effort <level>]
```

Same name, preserved session, new process, new `run-NNN/` (task snapshot
per run; old runs untouched). Only works on terminal agents. Requires
`--task`. Cannot change harness; agy requires a valid conversation pointer
(use `spawn`, not `resume`, when it is missing). `--thinking`/`--effort`
overrides are stored on the new run. Exit codes: 0 ok; 1 validation/launch
error; 2 bad harness/flag mix; 3 not found; 5 missing id, or `already_queued`
when the run awaits an infra-retry (wait or use `sam retry`); 6 not terminal;
7 max restarts; 8 lock timeout.
Resume model precedence: `--model` → stored model → `SAM_MODEL` →
    per-harness config/builtin. Reasoning flags apply to this run only; absent
flags defer to the harness, rather than inheriting an old explicit override.

### `sam restart`

Restart a terminal agent with a fresh run directory.

```bash
sam restart <id-or-name> [--harness <pi|agy>] [--thinking <level> | --effort <level>]
```

Same name, same task, new process. Only works on terminal agents.
Agy restarts preserve the `conversation_id` pointer so the conversation
continues; pi restarts use a fresh `run-NNN/session.jsonl`.
`--thinking`/`--effort` overrides are stored on the new run; switching
harness clears the other harness's stale setting. Exit codes: 0 ok;
1 validation/launch error; 2 bad harness/flag mix; 3 not found;
6 not terminal; 7 max restarts; 8 lock timeout. Restart retains the stored model.

### `sam retry`

SAM-owned infra-retry queue: fire, cancel, or list queued relaunches.

```bash
sam retry                          # list queue (fires-at, due flag)
sam retry <id-or-name>             # fire a DUE queued retry (relaunches same task)
sam retry <id-or-name> --cancel    # dequeue; agent -> killed (retry_cancelled)
sam retry --due                    # fire every due item
sam retry <id-or-name> --override-reason <why>   # fire early (logged)
```

SAM auto-enqueues infra deaths only: 429 `RESOURCE_EXHAUSTED` (any length)
and startup-network deaths (<2 min). `not_before` = parsed `Resets in`
(advisory, capped 30 min) or default backoff + jitter — weather, not rails:
`--override-reason` escapes it. Firing goes through the resume path (same
agent, new run, operator restart budget bypassed; agy resumes the
conversation when a pointer exists, else a fresh conversation on the same
task). The spawn breaker never gates a queued retry; `sam kill` or
`--cancel` drop it. Exit codes: 0 fired/listed; 1 not queued/launch error;
3 not found; 5 not due (already_queued); 8 lock timeout.

### `sam doctor`

Offline audit of launch spacing, concurrency, overrides, and the retry
queue (read-only; exits 0 unless the registry is unreadable).

```bash
sam doctor [--window [HOURS]] [--json]    # default window: 24 h
```

Per-spawn rows show `gap` since the previous spawn, concurrency at that
instant, `bypassed` (`--no-space`), and `--override-reason` usage.
`VERDICT: SPACING OK` = every gap ≥ 15 s (bypassed launches exempt) and
concurrency at spawn ≤ 4; otherwise a `VIOLATIONS` count. JSON mode is
scriptable. Use it to verify a burst window or review an override.

### `sam prune` / `sam unprune`

Prune hides, never deletes. `sam prune` sets `archived=true` on terminal
agents + unknowns (unknown archived with reason stale; directories, logs, results stay intact); `sam unprune` restores.
Resuming/restarting an archived worker clears `archived` only once the new
worker's PID is persisted (a failed launch keeps the archive).

```bash
sam prune [id|--all]   # no args = all terminal
sam unprune <id-or-name>
```

---

## Agent-to-Agent Contract

When an invoker (any agent/human/CLI) spawns a child sub-agent:

1. **Write a self-contained task file** — New workers receive this task plus native workspace/harness instructions, not the invoker's conversation. Resumed workers retain their own native history. Include goal, constraints, deliverables, and verification steps. For tasks longer than ~10 minutes, require incremental progress artifacts (e.g. append findings to `WORK_LOG.md` / `SUMMARY.md` as each unit completes, never only at the end) — a silent worker gets at most `alive (no task signal Xm)` from `status --detail` (proc-verified, never a stuck claim), so the artifacts are what distinguish real progress from a hang for humans and dashboards.

2. **Spawn the sub-agent (forget by default — no auto-wait):**
   ```bash
   sam spawn --name research-auth-n1 --task /tmp/task-refactor.md --cwd /home/user/project
   ```
   Return or keep working; no automatic wait, sleep, or polling loop.

3. **Wait only with explicit synchronous intent:**
   ```bash
   sam wait research-auth-n1 --json
   ```
   Explain that this blocks the turn until the agent is terminal (default
   `--timeout 0` = wait forever). Use only when explicitly requested or
   required to consume output before returning. JSON contains `status` and
   nested `result` (possibly null); completed/failed include `exit_code`,
   killed/unknown/partial/awaiting_retry omit it. Inspect `status` even on
   exit 0.
   NEVER pass a nonzero `--timeout`: it is deprecated and detaches
   immediately (exit 0, warning) — useless as a wait and useless as a
   liveness check. Passive checks are `sam status` (state) and
   `sam status <name> --detail` (working-vs-blocked verdict).
   Termination-on-expiry is explicit only: `--kill-after N`.
   NEVER spawn a progress-checker subagent to inspect another worker —
   one `--detail` call answers it without a new agent, task file, or run.

4. **Inspect the output, cheapest tier first:**
   ```bash
   sam result research-auth-n1
   sam logs research-auth-n1 -n 50
   ```
   `status` → `result` → `logs -n 50`. `result` is final-only;
   `logs` (default 50 lines) is for the full stream.

5. **Handle timeout:** `--kill-after N` attempts termination and persists `killed`; a plain wait (or a deprecated `--timeout N` detach) never signals. Check compact status before deciding whether a further kill or continuation is necessary.

6. **Ignore sentinels:** Lines like `##PI_BEGIN_a1b2c3d4` and `##PI_END_a1b2c3d4` are SAM framing markers. They are stripped by default in `sam logs`. Use `--raw` to see them.

---

## Rules & Constraints

1. **Name format:** `^[a-zA-Z0-9_-]{1,64}$`. Slashes, spaces, and dots are not allowed.
2. **Name uniqueness:** Never reuse a non-terminal agent's name. Terminal names can be reused. Prefer long kebab-case `<area>-<task>-<n>` (e.g. `research-auth-n1`).
3. **Depth limit:** Max 4 levels. Top-level = 0. Attempting deeper returns an error.
4. **Task files must be self-contained.** The sub-agent has no access to the parent/invoking process's conversation history. Include all necessary context.
5. **Never edit `~/.sam/registry.json` directly.** Always use SAM commands.
6. **Spawn-and-forget by default.** Never auto-wait, sleep, or poll after spawn. `wait` requires explicit synchronous intent as above.
7. **Read tiers: `status` → `status --detail` → `result` → `logs -n 50`.** `status` gives state; `--detail` adds the `Liveness:` verdict (`active`/`idle`/`working` with signal age, plus `alive (no task signal Xm)` for proc-verified but file-silent workers — tiered pgid+start-time check, resource probe, heartbeat bonus; never a bare "stalled") — the working-vs-blocked answer, no checker agent needed; `result` is final-only; `logs` (default 50 lines) is the last resort for the full stream.
8. **Pass `--model` only when overriding the default.** Children inherit `SAM_MODEL` automatically. Any model the underlying `pi`/`agy` CLI accepts can be used — the config default is a default, not an allowlist.
9. **Spawning recovery:** a worker that has not persisted its PID within 30s of launch resolves as `failed` (not stuck `spawning`); recover with `sam restart` or `sam kill`.

---

## Exit Codes

| Code | Meaning | Commands |
|------|---------|----------|
| 0 | OK, or wait rendezvous on any terminal state incl. partial/awaiting_retry (read JSON `status`) | All |
| 1 | General error; wait rendezvous on failed; lock timeout (wait/kill/logs); result unavailable; retry not queued | All |
| 2 | Name exists (non-terminal), bad harness/flag mix, ambiguous ref, missing result identifier; argparse misuse | spawn, resume, restart, result, logs, prune, unprune; parser |
| 3 | Not found | kill, logs, restart, resume, result, retry |
| 4 | `--kill-after N` exceeded (terminated; state persisted `killed`). Deprecated nonzero `--timeout` instead detaches with exit 0 | wait |
| 5 | Not found / identifier required; `already_queued` (resume on queued run; retry not due); spawn refused (name has queued retry) | wait, resume, retry, spawn |
| 6 | Not terminal; `deferred` (spacing slot or 429 window open — not an error) | restart, resume, spawn |
| 7 | Max restarts reached | restart, resume |
| 8 | Lock timeout | spawn, resume, restart, retry |
| 130 | KeyboardInterrupt (Ctrl+C) | All |

---

## Limitations (v0.1)

- **Linux only.** SAM uses `/proc/<pid>/stat` and `fcntl.flock`.
- **No daemon.** If the parent/invoking process exits, orphaned sub-agents may continue running. SAM tracks PIDs in the registry so you can find them later.
- **No automatic recovery.** If the CLI crashes during `sam spawn`, an agent may sit in `spawning` for up to 30s (launch window), then resolves `failed`. Use `sam restart` or `sam kill` to recover.
- **Install:** CLI entry points and links depend on the installation. SAM uses
  wrapper copies in `$SAM_HOME/bin/`; source edits alone do not update these.
  `sam init` installs wrappers without rewriting an existing config unless
  `--force` is used. Never manually delete managed history; prune archives it.
- **Provider restriction (confirmed September 2026):** Muse's free provider
  rejects non-OpenCode clients. The configured pi default
  `opencode/muse-spark-1.3-contributor-free` may fail in pi; override with
  `--model` or `SAM_MODEL` using a provider/model your pi supports. Native
  OpenCode is next-stage work, not a supported SAM harness yet.
- **Environment passthrough & isolation.** Sub-agents inherit the invoker's environment variables (including API keys), except interactive SSH session markers (`SSH_CLIENT`, `SSH_CONNECTION`, `SSH_TTY`), which are stripped to ensure headless harnesses like `agy` do not request re-login. `SSH_AUTH_SOCK` is preserved for git operations.
- **Registry is a single JSON file.** No concurrent modification protection beyond file locking. Do not edit it manually.
