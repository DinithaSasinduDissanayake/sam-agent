# SAM — Sub-Agent Manager

SAM is a CLI tool that lets AI agents spawn, track, kill, and restart background sub-agents.
Prevents orphaned processes, lost PIDs, and unreliable `nohup` + `&` workflows.

**Linux only. Python 3.9+ stdlib only. Zero dependencies.**

> **Are you an AI agent? Stop reading this and read [`SKILL.md`](SKILL.md) instead.**

---

## Install

```bash
pip install git+https://github.com/DinithaSasinduDissanayake/sam-agent.git
```

## Quick start

```bash
sam init
sam spawn --name worker --task task.md --json
sam wait worker --json
sam logs worker -n 50
```

## Commands

| Command | Purpose |
|---------|---------|
| `init` | Initialize SAM home directory |
| `spawn` | Spawn a sub-agent (accepts `--model`, `--thinking <level>`) |
| `status` | Show agent state |
| `kill` | Kill a running agent |
| `wait` | Wait for agent completion |
| `logs` | Show agent logs |
| `restart` | Restart a terminal agent |
| `resume` | Continue agent session with new task (accepts `--model`, `--thinking <level>`) |

All commands accept `--json` for machine-readable output and `--sam-home <path>` to override the data directory.

## How it works

- Agents are tracked in a JSON registry with flock-based locking
- Each agent gets its own directory with run-NNN/ history
- A wrapper script captures output, writes sentinel markers, and produces a structured result.json
- Eight exit codes for scriptable handling (0=ok, 1=error, 2=name exists, 3=not found, 4=timeout, 6=not terminal, 7=max restarts, 8=lock timeout)

## Limitations (v0.1)

- Linux only. Relies on `/proc/<pid>/stat`, `fcntl.flock`, `os.killpg`.
- No daemon mode. Agents run as background processes. Orphans possible if parent crashes.
- No automatic recovery. Manual `sam status` and `sam restart` required.
- Disk space grows with each agent run. Clean manually with `rm -rf ~/.sam/agents/*`.

## License

MIT