# Future: runtime analytics (planning note — not implemented)

Goal: estimate the earliest useful CHECK time for a spawned worker without
repeated token-cost polling (`status --detail` parses session tails; `wait`
blocks the turn). Spawn-and-forget stays the default; future human
notifications are not a priority.

## Currently recorded telemetry (per run, no new engine needed)

Registry entry (`registry.json`, per agent, mutated across runs — keep
`created_at` for history, `run_started_at` for the current run):

- `harness`, `model`, `thinking`, `effort` (requested reasoning; stored on
  spawn/resume/restart; stale side cleared on harness switch)
- `created_at`, `run_started_at`, `updated_at`
- `run_id`, `run_count`, `restart_count`
- `exit_code`, `exit_signal`, `duration_ms` (write-back from result.json),
  `killed_reason`

Per-run files (`agents/<id>/run-NNN/`, retained across subsequent runs):

- `result.json`: `harness`, `exit_code`, `exit_signal`, `final_state_hint`,
  `duration_ms`, `started_at` (epoch), `ended_at` (epoch), `conversation_id`
  (agy), `session_path`, `result` (final text or null), `error`
- `task.md`: exact prompt for that run; `output.log`: full stream

Only task snapshots are enforced write-once today. Logs grow during execution;
results are written atomically at completion. Model, requested reasoning, and
task category are not stored in the result schema, so historical cohorts cannot
be reliably reconstructed from the mutable registry. A future immutable per-run
record must capture those launch settings, run ID, and completion outcome.

## Missing for distributions

- Task category (no label exists; cheapest path is an opt-in `--tag`
  recorded per run, not classifiers).
- Per-harness token cost (pi usage exists in session.jsonl but is not
  aggregated per run; agy exposes no usage stream at all).
- Failure taxonomy beyond `final_state_hint`/`error` strings.
- Persistent historical queue/launch latency (current registry `run_started_at`
  and result `started_at` permit current-run calculation, not complete history).

## Quantile guidance (when data exists)

- Lower-tail completion percentiles (**p1/p10**) estimate the earliest
  useful CHECK — the point where the first runs of a cohort start
  finishing.
- **p90/p99** describe the slow tail (do not use them to schedule the
  first check; that reverses the percentiles).
- Slice by (harness, model, reasoning, task category); show sample counts
  per cell.
- Separate completed successes, observed failures, explicit timeouts, and
  unfinished/lost observations. Failures are outcomes, not automatically
  censored success samples; timeouts/unfinished runs may be right-censored.
  Never average these into successful completion latency.
- No check schedule can be inferred before data exists; until then keep
  spawn-and-forget + rendezvous-on-need.

## Non-goals

No telemetry engine, no auto-wait, no notification daemon in this note.
