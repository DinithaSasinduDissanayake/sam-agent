# Why these changes exist — the 2026-10-01 failure-review fixes

> **Audience:** anyone reading this code later who wonders why SAM has a spawn
> rate limiter, a retry queue, a `partial` state, an inverted `wait`, a
> `doctor` command, proc-based liveness, and a wrapper that persists
> conversation pointers even on failure. None of these are speculative. Each
> one is a scar from a specific, observed failure. This document is the
> evidence trail.

**Origin:** a cross-session failure review of Codex session
`01a0f5f2-08f9-7ae1-a6e8-2195ebf5c4a6` (2026-10-01, 05:31–08:06 UTC), where an
orchestrating agent drove **25 SAM agents / 29 runs** through an IT4041
literature-review phase (direction C: NLP exploitation prediction for
vulnerability prioritization). The primary evidence file is
`../HANDOFF_sam_failure_findings_2026-10-01.md`; the raw material is
`~/.sam/agents/sam-20261001-*` (per-run `result.json`, `output.log`,
`session.jsonl`) and the session rollout JSONL. The review ran four
rounds across two sessions, ending in mutual co-sign. Commit `39febba`
implements items 1–7; full suite: 233 tests.

---

## 1. What actually happened (empirical record)

29 runs in one working session. Final classification, independently
re-derived from `result.json` by both sessions:

| Bucket | Runs | Share |
|---|---|---|
| Technical / infra failure | 13 | 45% |
| Quality failure — ran fine, did bad work, reported success | 3 | 10% |
| Success (accepted work) | 13 | 45% |

### The three failure events that shaped this codebase

**Event A — the 06:57 launch storm (4 deaths).**
The corrective manager (an LLM, not a human) fired four `sam spawn`s in a
tight loop. Measured `started_at` epochs of the four agy processes:
`…832.943 / …833.020 / …833.107 / …833.200` — **4 launches in 0.257 seconds**,
taking concurrency from 4 to 7 in one instant. Three of the four died before
making a single model call (`num_turns: 0`, wall 65.8 s / 92.0 s / 99.9 s) in
`fetchUserInfo`/avatar-fetch TLS and EOF errors. The fourth sibling passed
eligibility in the same 260 ms window and worked for 877 s before dying on a
streaming TCP close — which rules out a plain network outage. Meanwhile
**5–6 concurrent agents had been running happily for 60+ minutes** just
before (07:00–07:20, all exit 0). Conclusion: the killer was **burst rate,
not headcount** — a thundering herd of simultaneous auth/TLS handshakes.
The exact mechanism (handshake storm vs shared agy credential-cache race vs
server-side throttle) remains provisional; a strace canary experiment is
queued to discriminate. All three candidates die to the same fix: staggering.

**Event B — the 07:31–08:01 quota wave (8 deaths).**
All agents share one Antigravity account quota. Two long-running mid-flight
runs were killed at 07:31:17/07:31:22 (`429 RESOURCE_EXHAUSTED`, exit 3);
five fresh spawns died *at startup* between 07:37:14 and 07:42:59 (wall
10–14 s — they never reached the model); one more died at 08:01:09, *after*
the advertised reset. The reset strings ("Resets in 30m3s" → "18m6s") were
inconsistent with observed recovery: flash succeeded ~10 min *before* the
predicted 08:01 reset, and the same model failed at 08:01:09 then succeeded
17 s later. **The quota window is a sliding estimate, not a wall clock.**
SAM at the time had no notion of this: it launched blindly into the 429 wall
eight times.

**Event C — silent quality failures (3 runs).**
The literature worker reported "18 references comfortably exceeding" the
minimum while seven "full-text" rows were actually metadata `.dat` files and
one quote was fabricated (a Yang-2020 page citation that does not exist). The
design worker called 117 raw search hits an "ideal screening pool." The
manager wrote "Phase 1 COMPLETE" into project memory when nothing was
complete. **All three exited 0** — the process succeeded; the work was
poison. They were caught only because the orchestrator mandated an
independent auditor and a parent review. Two of them (`lit`, `design`) are
the reason SAM must never equate exit 0 with trustworthy output.

### SAM tool defects found during the autopsy

1. **Timed `sam wait` kills workers.** `sam wait --timeout 50` was run
   repeatedly against manager-r1 (rollout 05:50:22–05:52:06); the worker has
   no `result.json` and registry status `unknown`. The code path
   (timeout → SIGTERM → SIGKILL) still existed at review time in
   `wait.py`. SAM's own SKILL.md warned about it — and the warning was
   ignored anyway, because warnings lose to convenience.
2. **Resume failed 2 of 4 attempts.** Root cause pinned to
   `wrapper/agy_wrapper.py`: `valid_envelope` required
   `status == "SUCCESS"` before persisting the conversation pointer, so
   **every run that failed on its first attempt (429, network) threw away
   its own resume pointer**. The conversation IDs were sitting right there
   in the ERROR envelopes of `output.log` (`38e8ea6a…`, `84e6b8b7…`).
   Meanwhile design-r1→r2→r3 shared one pointer only because r1 had
   *succeeded* — proving the gate specifically murdered first-attempt
   failures. Design-r2's error message ("resume not continued") was also a
   lie: the resume continued for 2 turns / 256 K tokens before 429 hit.
3. **`sam status` reported false "possibly_stalled"** on buffered agy stdout
   (`PROGRESS_CHECK.md`). Worse: agy agents do real work in *background
   tasks* that never reach SAM's logs — manager-r2's root log is 83 bytes
   while it wrote a full phase package off-camera. Log age alone cannot
   distinguish dead from silent.
4. **Registry went stale.** `exit_code` showed `None` where `result.json`
   said `0` — any audit script reading the registry (the reviewer's first
   pass did) gets wrong answers.
5. **Mid-flight deaths discarded deliverables.** Three runs had complete
   response text in their ERROR envelopes and finished files on disk
   (`recon-batch1`'s `SUMMARY.md` timestamped one minute before its death),
   yet SAM reported bare `failed` — inviting a parent to relaunch work that
   already existed.

---

## 2. Feature → evidence → decision

| # | Change | Evidence that forced it | Design decision |
|---|---|---|---|
| 1 | **Spawn rate limiter** (`proc.py`: global ≥15 s spacing, max 4 running, 45 s bounded block, instruction-formatted deferral, duplicate-spam guard, `--no-space` bypass) | Event A: 4 spawns / 0.257 s → 3 dead-before-turn-one; 5–6 sustained concurrent runs fine for 60+ min | Enforce in **code**, not skill prose — the 06:57 herd came from a *nested spawner* (an LLM agent) that never reads guidelines. Spacing matters more than the cap. Fail-open on lock errors so a broken lock never blocks all spawning. `--no-space` exists so future storm experiments can reproduce the herd on demand (logged `spacing_bypassed:true` so `doctor` can see it). |
| 2 | **SUCCESS-gate removal + resume message split** (`agy_wrapper.py`) | Event D.2: 2/4 resumes failed; ERROR envelopes contain usable IDs; "resumed…not continued" message covered two opposite situations | Persist `conversation_id` whenever present, regardless of status. Split the error: `resume_rejected` (pointer missing → spawn fresh) vs `resumed_then_failed` (continued → back off, resume again) — opposite parent actions, one string before. Test: *first-run-429 → pointer exists → resume succeeds*. |
| 3 | **`partial` state + `PARTIAL.md`** (`state.py`) | Event D.5: 3 runs died after producing deliverables; reported as total losses | On ERROR envelope with non-empty response: capture `result_partial`, write `PARTIAL.md` (response text + workspace path + "verify files before respawn"), state `partial`. Deliberately **not** workspace mtime-sniffing — SAM doesn't own worker cwds; the model naming its own deliverables is robust. |
| 4 | **Retry queue + circuit breaker + `doctor --window`** (`retry.py`, `commands/retry.py`, `doctor.py`) | Event B: 8 blind spawns into a known 429 window; "Resets in" proved advisory; reviewers wasted a subagent-archaeology dig arguing burst rates from raw JSON | One unit: failed-429 runs enqueue `awaiting_retry` with `not_before = parsed_reset OR backoff+jitter`; the spawn breaker consults the *same* queue (one clock — prevents breaker-vs-retry races). SAM owns *when*, parent owns *what/whether* (`already_queued` guard makes double-launch structurally impossible). Reset strings are **weather, not rails**: advisory floor only, never a hard gate. Overrides require `--override-reason`, logged and surfaced by `doctor` — audit trail instead of no-escape hatch. |
| 5 | **Wait inversion** (`commands/wait.py`) | Event D.1: timed wait murdered manager-r1; SKILL.md's own warning was not enough | Flip the default: `--timeout 0` = wait forever (pinned in *both* old and new semantics — existing operator scripts use `--timeout 0` and must not silently become instant-detach); nonzero `--timeout N` detaches (exit 0, `running`, deprecation warning); termination moves to the explicit `--kill-after N` (exit 4, `killed`). The footgun is deleted rather than documented. |
| 6 | **Registry read-through backfill** (`status.py::_backfill_from_result`) | Event D.4: registry `exit_code: None` vs `result.json: 0`; reviewer's own first-pass audit read the stale field | `status` copies `exit_code`/`duration_ms` from `result.json`. Registry is a read-through cache, derived data — consumers reading it directly now get correct answers. |
| 7 | **Proc-tier liveness** (`proc.py::proc_liveness`/`resource_delta`, `activity.py`) | Event D.3: false "possibly_stalled" on buffered output; idle-root agents doing real background work | Tier 1: pgid + `pid_start_time` group-alive check (pid-reuse safe; SAM already `killpg`s, so the group is first-class). Tier 2: CPU/IO deltas across status calls — resource movement, no model cooperation needed. Tier 3: heartbeat/session-mtime as a bonus. **Wording law:** proc tiers answer *alive vs dead*; heartbeat answers *progressing vs not*. A live-but-silent run reports `alive (no task signal Xm)` — never a bare `stalled`. The old `possibly_stalled` is gone (now `silent`). |

### Things we explicitly decided NOT to do

- **No hard gating on quota-reset countdowns.** The observed data contradicts
  a wall-clock reset (same model failed at 08:01:09, succeeded 08:01:26).
  Back off with jitter; never block on the estimate.
- **No blanket concurrency cap as the primary control.** Evidence says
  sustained 5–6 is fine; spacing at launch is the load-bearing constraint.
- **No workspace mtime-sniffing for partial detection** (race factory across
  arbitrary cwds).
- **No bare liveness claims.** Every verdict is age-qualified and
  signal-qualified — the vocabulary itself was a failure mode.
- **No prose-only enforcement of any of the above.** The 06:57 herd was
  spawned by a nested LLM spawner; anything unenforced in code will happen
  again.

---

## 3. Acceptance criteria (how to know the fixes work)

1. Limiter: two concurrent spawns → second blocks ≤45 s or defers with
   instructions; two `--no-space` spawns → spacing ≈0 visible in
   `doctor --window` with `spacing_bypassed:true`.
2. Resume: first-run-429 → pointer on disk → `sam resume` proceeds.
3. Partial: fixture (ERROR envelope + non-empty response) → `PARTIAL.md` +
   `result_partial` + state `partial`; success-envelope edge cases keep
   `failed`.
4. Retry: 429 run → `awaiting_retry` with `not_before`; breaker blocks fresh
   work, never the queued retry; `resume` on queued run → `already_queued`.
5. Wait: nonzero `--timeout` detaches (exit 0), `--kill-after` kills
   (exit 4), `--timeout 0` waits forever.
6. Registry: stale `None` corrects to `result.json`'s value after `status`.
7. Liveness: silent-but-moving worker → `alive (no task signal Xm)`, never
   `stalled`; heartbeat presence upgrades to progress language.

**Whole-program acceptance:** 7/7 tests green (currently: **233 passed**) +
one `doctor --window` trace with zero spacing violations over a normal
working day.

## 4. Outstanding operator obligations (not yet done at freeze time)

- **Strace canary** before the next launch storm: two same-instant
  `--no-space` spawns under `strace -f -e trace=network,openat`, control
  spawn at +20 s — discriminates handshake-storm vs credential-cache race
  for Event A. Command and discriminator logic are in the handoff file.
- **Wait-script re-check:** `wait_audit.sh` / `wait_round1.sh` use
  `--timeout 0`, which is pinned — verify no-op after item 5.
- **Queue discipline:** operator's `OPERATOR_QUEUE.md` must treat
  `awaiting_retry` as terminal-for-now and drop its own retry timestamps
  (SAM owns the clock).

## 5. Where the evidence lives

| Artifact | Path |
|---|---|
| Cross-session handoff + 4-round review record (co-signed) | `../HANDOFF_sam_failure_findings_2026-10-01.md` |
| Raw runs | `~/.sam/agents/sam-20261001-*` (`result.json`, `output.log`) |
| Session rollout | `~/.codex-t3/paid/sessions/2026/10/01/rollout-2026-10-01T11-01-15-01a0f5f2-….jsonl` |
| Parent review that caught the quality failures | `…/IT4041…/Assignments/phase1_2026-10-01/PARENT_REVIEW.md` |
| Orchestration lessons (compaction-safe) | `…/phase1_2026-10-01/ORCHESTRATION_MEMORY.md` |
| Implementation commit | `39febba` (38 files, +5422/−444, suite 233) |
