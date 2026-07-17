# Changelog

## [0.1.0] - 2026-07-17

### Added
- Core commands: `init`, `spawn`, `status`, `kill`, `wait`, `logs`, `restart`
- Process lifecycle management with 6-state machine (spawning → running → completed/failed/killed/unknown)
- Atomic JSON registry with flock-based locking
- Named agents with collision detection and depth limits (max 4)
- Wrapper executable (`pi-wrapper`) with streaming logs, sentinel markers (`##PI_BEGIN_`/`##PI_END_`), and atomic `result.json`
- JSON output (`--json`) across all commands
- Configurable SAM_HOME via env var or `--sam-home` flag
- Agent-to-agent contract documentation (`SKILL.md`)
- Test suite with `fake_pi.py` test double (no real pi needed)
- 8 exit codes for scriptable handling