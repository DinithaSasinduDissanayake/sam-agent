# SKILL.md — SAM: Sub-Agent Manager

## What is SAM?

SAM is a CLI tool that lets you (a `pi` agent) spawn background sub-agents,
wait for them to finish, read their output, and kill or restart them.

Each sub-agent is an independent `pi` process running a task you define in a
markdown file. SAM tracks process state, captures output, and handles cleanup.

**Platform:** Linux only. Python 3.9+. No external dependencies.

---

## Quick Start (3 commands)

```bash
# 1. Spawn a sub-agent
sam spawn --name my-task --task ./tasks/refactor-auth.md --cwd /home/user/project

# 2. Block until it finishes
sam wait my-task --json

# 3. Read its output
sam logs my-task -n 200
```

---

## The 3-Command Contract

The core workflow is always the same three steps:

1. **`sam spawn`** — Creates a sub-agent process, assigns it a stable name, copies your task file, launches it in the background.
2. **`sam wait`** — Blocks until the agent reaches a terminal state (completed/failed/killed). Returns a JSON object with status, exit code, and paths to output files.
3. **`sam logs`** — Shows the agent's stdout/stderr output. Sentinels like `##PI_BEGIN_...` are stripped by default.

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
sam spawn --name <name> --task <path> [--cwd <dir>] [--model <model>]
```

| Flag | Required | Description |
|------|----------|-------------|
| `--name` | Yes | Unique name for this agent `^[a-zA-Z0-9_-]{1,64}$` |
| `--task` | Yes | Path to task markdown file |
| `--cwd` | No | Working directory (default: task file's parent directory) |
| `--model` | No | Model to use (default: from config or SAM_MODEL env) |
| `--thinking` | No | Thinking/reasoning level for model (`off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`) |

**Output:** Agent ID like `sam-20260716-103042-a1b2c3`.

### `sam status`

Check agent state.

```bash
sam status [<id-or-name>] [--json]
```

Without arguments, lists all agents. With an ID or name, shows that agent.

States: `spawning`, `running`, `completed`, `failed`, `killed`. Resolved dynamically.

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

Sends SIGTERM → waits 5s → sends SIGKILL if needed.

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

---

## Agent-to-Agent Contract

When a `pi` agent spawns a child sub-agent:

1. **Write a self-contained task file** — The sub-agent receives ONLY this task file. No inherited conversation context. Include goal, constraints, deliverables, and verification steps.

2. **Spawn the sub-agent:**
   ```bash
   sam spawn --name research-auth --task /tmp/task-refactor.md --cwd /home/user/project
   ```

3. **Wait for completion:**
   ```bash
   sam wait research-auth --json
   ```
   Blocks until done. Returns result JSON with `status`, `exit_code`, and `result.output_path`.

4. **Inspect the output:**
   ```bash
   sam logs research-auth -n 200
   ```

5. **Handle timeout:** If `sam wait` times out, run `sam kill` before retrying.

6. **Ignore sentinels:** Lines like `##PI_BEGIN_a1b2c3d4` and `##PI_END_a1b2c3d4` are SAM framing markers. They are stripped by default in `sam logs`. Use `--raw` to see them.

---

## Rules & Constraints

1. **Name format:** `^[a-zA-Z0-9_-]{1,64}$`. Slashes, spaces, and dots are not allowed.
2. **Name uniqueness:** Never reuse a non-terminal agent's name. Terminal names can be reused.
3. **Depth limit:** Max 4 levels. Top-level = 0. Attempting deeper returns an error.
4. **Task files must be self-contained.** The sub-agent has no access to the parent's conversation history. Include all necessary context.
5. **Never edit `~/.sam/registry.json` directly.** Always use SAM commands.
6. **Prefer `sam wait` over polling `sam status`.** Wait handles state reconciliation automatically.
7. **Pass `--model` only when overriding the default.** Children inherit `SAM_MODEL` automatically.

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
- **No daemon.** If the parent `pi` process exits, orphaned sub-agents may continue running. SAM tracks PIDs in the registry so you can find them later.
- **No automatic recovery.** If the CLI crashes during `sam spawn`, an agent may be stuck in `spawning` state. After 30 seconds, use `sam restart` or `sam kill` to recover.
- **Full environment passthrough.** Sub-agents inherit the parent's environment variables, including API keys. This is a known v0.1 limitation.
- **Registry is a single JSON file.** No concurrent modification protection beyond file locking. Do not edit it manually.
