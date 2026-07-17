# Changelog

## [0.1.1] - 2026-07-17
### Added
- `sam skill` command — prints SKILL.md for agent discovery
- `sam prune` command — removes terminal agents and cleans up disk
- `sam status --all` — show all agents including terminal (default: active only)
- Concurrency warning on `sam spawn` — stderr warning when 3+ agents on same model

## [0.1.0] - 2026-07-17
### Added
- Core commands: `init`, `spawn`, `status`, `kill`, `wait`, `logs`, `restart`
- Process lifecycle management with 6-state machine
- Atomic JSON registry with flock-based locking
- Named agents with collision detection and depth limits (max 4)
- Wrapper executable with streaming logs, sentinel markers, atomic result.json
- JSON output (`--json`) across all commands
- Test suite with `fake_pi.py` test double
- 8 exit codes for scriptable handling
