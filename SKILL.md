---
name: sam
description: Spawn/wait/read/kill background pi/agy sub-agents via SAM CLI. Use for parallel delegation, spawn-and-forget workers.
---
# SKILL.md — SAM: Sub-Agent Manager

## What is SAM?

SAM is a CLI tool that lets you (an AI agent, human operator, or any CLI invoker) spawn
background sub-agents,
wait for them to finish, read their output, and kill or restart them.

Each sub-agent is an independent `pi`/`agy` worker process running a task you define in a
markdown file. SAM tracks process state, captures output, and handles cleanup.

**Platform:** Linux only. Python 3.9+. No external dependencies.

---

## Quick Start (spawn-and-forget)

Spawn-and-forget is the default. Do NOT auto-wait after spawn — the parent/invoking process
keeps working and only rendezvous when it needs the child's output.

```bash
# 1. Spawn and keep working (no auto-wait)
sam spawn --name research-auth-n1 --task ./tasks/refactor-auth.md --cwd /home/user/project

# 2. Rendezvous only when the output is needed
sam wait research-auth-n1 --json

# 3. Read final output first, then logs if needed
sam result research-auth-n1
sam logs research-auth-n1 -n 50
```

Name agents with long kebab-case `<area>-<task>-<n>`
(e.g. `research-auth-n1`, `frozen-meatballs-prices-n2`). Never reuse a
non-terminal agent's name.

---

## The Contract

1. **`sam spawn`** — Creates a sub-agent process, assigns it a stable `<area>-<task>-<n>` kebab name, copies your task file, launches it in the background. Forget it; keep working.
2. **`sam wait`** — Rendezvous only: blocks until the agent reaches a terminal state (completed/failed/killed). Returns JSON with status, exit code, and output paths.
3. **`sam result`** — Final-only output text. Cheapest read after `status`.
4. **`sam logs -n 50`** — Full stream tail when `result` is not enough. Sentinels like `##PI_BEGIN_...` are stripped by default.
5. **`sam resume`** — Tier-1 alongside `status`/`result`: reattach to a running agent's session instead of tailing logs; use when you need interactive follow-up rather than final-only output.

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
| `--model` | No | Model to use (default: from config or SAM_MODEL env) |
| `--thinking` | No | Thinking/reasoning level for model (`off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`) |
| `--harness` | No | Harness wrapper (`pi`, `agy`; default: `pi` via $SAM_HARNESS or config defaults.harness) |
| `--effort` | No | Effort level for `agy` harness only; cannot combine `--thinking` with `agy` |

**Output:** Agent ID like `sam-20260716-103042-a1b2c3`.

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
| `--watch [SECONDS]` | Live dashboard: two-sample byte deltas over SECONDS (1–30, default 5); implies `--detail` |

States: `spawning`, `running`, `completed`, `failed`, `killed`, `unknown`.
Resolved dynamically; terminal states are written back to the registry.

Live dashboard entry: `sam status --watch [SECONDS]` (alias entry point
`sam-tui` where installed runs the same view). There is no notify daemon —
poll with `status` or rendezvous with `wait`.

### `sam result`

Print an agent's final output only (from `result.json`).

```bash
sam result <id-or-name> [--json]
```

Read tiers, cheapest first: `status` → `result` → `logs -n 50`.
`result` is final-only text; reach for `logs` only when you need the full
stream. `logs` tails 50 lines by default (`-n`), sentinels stripped unless
`--raw`.

### `sam wait`

Block until an agent reaches a terminal state.

```bash
sam wait <id-or-name> [--timeout <seconds>] [--json]
```

| Flag | Description |
|------|-------------|
| `--timeout <seconds>` | Max wait time (default: 300, 0 = wait forever) |

**Exit codes:**
- 0 = completed, failed, killed, or unknown (read JSON `status` field)
- 4 = timeout (waited too long, agent was killed)
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

Sends SIGTERM → waits 5s → sends SIGKILL if needed. `sam kill <unknown>` marks dead-PID unknown as `killed` (no signal if proc dead/recycled).

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

### `sam restart`

Restart a terminal agent with a fresh run directory.

```bash
sam restart <id-or-name>
```

Same name, same task, new process. Only works on terminal agents.
Agy restarts preserve the `conversation_id` pointer so the conversation
continues; pi restarts use a fresh `run-NNN/session.jsonl`.

### `sam prune` / `sam unprune`

Prune hides, never deletes. `sam prune` sets `archived=true` on terminal
agents + unknowns (unknown archived with reason stale; directories, logs, results stay intact); `sam unprune` restores.

```bash
sam prune [id|--all]   # no args = all terminal
sam unprune <id-or-name>
```

---

## Agent-to-Agent Contract

When an invoker (any agent/human/CLI) spawns a child sub-agent:

1. **Write a self-contained task file** — The sub-agent receives ONLY this task file. No inherited conversation context. Include goal, constraints, deliverables, and verification steps.

2. **Spawn the sub-agent (forget by default — no auto-wait):**
   ```bash
   sam spawn --name research-auth-n1 --task /tmp/task-refactor.md --cwd /home/user/project
   ```
   Keep working. Rendezvous with `sam wait` only when you need the output.

3. **Wait only on rendezvous:**
   ```bash
   sam wait research-auth-n1 --json
   ```
   Blocks until done. Returns result JSON with `status`, `exit_code`, and `result.output_path`. Prefer `wait` over polling `status`.

4. **Inspect the output, cheapest tier first:**
   ```bash
   sam result research-auth-n1
   sam logs research-auth-n1 -n 50
   ```
   `status` → `result` → `logs -n 50`. `result` is final-only;
   `logs` (default 50 lines) is for the full stream.

5. **Handle timeout:** If `sam wait` times out, run `sam kill` before retrying.

6. **Ignore sentinels:** Lines like `##PI_BEGIN_a1b2c3d4` and `##PI_END_a1b2c3d4` are SAM framing markers. They are stripped by default in `sam logs`. Use `--raw` to see them.

---

## Rules & Constraints

1. **Name format:** `^[a-zA-Z0-9_-]{1,64}$`. Slashes, spaces, and dots are not allowed.
2. **Name uniqueness:** Never reuse a non-terminal agent's name. Terminal names can be reused. Prefer long kebab-case `<area>-<task>-<n>` (e.g. `research-auth-n1`).
3. **Depth limit:** Max 4 levels. Top-level = 0. Attempting deeper returns an error.
4. **Task files must be self-contained.** The sub-agent has no access to the parent/invoking process's conversation history. Include all necessary context.
5. **Never edit `~/.sam/registry.json` directly.** Always use SAM commands.
6. **Spawn-and-forget by default.** Never auto-wait after spawn. `wait` only on rendezvous, when the child's output is actually needed.
7. **Read tiers: `status` → `result` → `logs -n 50`.** `result` is final-only and cheapest after `status`; `logs` (default 50 lines) is the last resort for the full stream.
7. **Pass `--model` only when overriding the default.** Children inherit `SAM_MODEL` automatically.
8. **Default model failure:** the hardcoded default is the only supported model. If a spawn fails with model errors (`FreeTierError`, `Model unavailable`, `403`, `not found for provider`), do NOT retry with other models. Ask the user whether it is time to update the default or the failure is just rate limits.

---

## Exit Codes

| Code | Meaning | Commands |
|------|---------|----------|
| 0 | OK | All |
| 1 | Error (general) | All |
| 2 | Name exists (non-terminal) | spawn |
| 3 | Not found | kill, logs, restart |
| 4 | Timeout | wait |
| 5 | Not found | wait |
| 6 | Not terminal | restart |
| 7 | Max restarts reached | restart |
| 8 | Lock timeout | spawn, restart |
| 130 | KeyboardInterrupt (Ctrl+C) | All |

---

## Limitations (v0.1)

- **Linux only.** SAM uses `/proc/<pid>/stat` and `fcntl.flock`.
- **No daemon.** If the parent/invoking process exits, orphaned sub-agents may continue running. SAM tracks PIDs in the registry so you can find them later.
- **No automatic recovery.** If the CLI crashes during `sam spawn`, an agent may be stuck in `spawning` state. After 30 seconds, use `sam restart` or `sam kill` to recover.
- **Full environment passthrough.** Sub-agents inherit the invoker's environment variables, including API keys. This is a known v0.1 limitation.
- **Registry is a single JSON file.** No concurrent modification protection beyond file locking. Do not edit it manually.
