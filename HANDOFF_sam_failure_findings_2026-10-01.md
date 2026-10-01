# Handoff: SAM Failure-Mode Findings — IISA Phase 1 Session (2026-10-01)

**From:** T3 Code session analyzing Codex session `01a0f5f2-08f9-7ae1-a6e8-2195ebf5c4a6`
**To:** Session working on the SAM agent (verify independently, then give your thoughts)
**Purpose:** Cross-check our failure taxonomy and jointly design mitigations. We suspect quota alone cannot explain everything; network flakiness and SAM footguns need combatable fixes.

---

## 1. Sources we used (verify against these)

- Codex rollout: `/home/sasindu/.codex-t3/paid/sessions/2026/10/01/rollout-2026-10-01T11-01-15-01a0f5f2-08f9-7ae1-a6e8-2195ebf5c4a6.jsonl`
- SAM runtime: `/home/sasindu/.sam/agents/sam-20261001-*` (`result.json`, `output.log`, `session.jsonl` per run)
- Work artifacts: `/home/sasindu/Documents/SLIIT Materials/Y4S1/4 Modules/IT4041 - Introduction to Information Security Analytics/Assignments/phase1_2026-10-01/`
  (esp. `PARENT_REVIEW.md`, `ORCHESTRATION_MEMORY.md`, `PROGRESS_CHECK*.md`, `corrective/AGENT_REGISTRY.md`, `OPERATOR_DASHBOARD.md`, `*/WORK_LOG.md`, `*/SUMMARY.md`)
- SAM skill: `/home/sasindu/Documents/Projects/pi claw/sam/SKILL.md`

## 2. Inventory

- **25 agents / 29 runs**, 05:50–08:06 UTC, all on harness `agy` (Antigravity), models gemini-3.8-flash-medium and gemini-3.1-pro-high.
- Dashboard screenshot at review time: 9 completed / 11 failed / 20 recent of 44 total entries.

## 3. Failure taxonomy (our classification — please challenge)

| Bucket | Runs | Share |
|---|---|---|
| (a) Technical / infra | 13 | 45% |
| (b) Quality failure (ran fine, did bad work, exit 0) | 3 | 10% |
| (c) Success | 13 | 45% |

### (a) Technical — 13 runs
1. **429 RESOURCE_EXHAUSTED — 8 runs**, single wave **07:31:17–07:43:00 UTC**:
   `064356 (corrective-manager)`, `070156 (access-gap-n2)`, `070717-r2/r3 (corrective-design)`,
   `073714/073749/073928 (audit-batch1/2/3-n1, died at 10–12 s)`, `074259 (access-partial-n3)`.
   Evidence: `run-001/output.log` → `AGY_ERROR … RESOURCE_EXHAUSTED`, "Individual quota reached", "Resets in 30m3s" → "18m6s".
2. **Startup network errors — 4 runs, all in a ~06:57 burst**:
   `065712-057346` fetchUserInfo EOF (65 s), `065713-c4ee51` unexpected EOF (92 s),
   `065713-19813a` TLS handshake timeout (100 s), `065712-d2c87d` closed network connection (exit 1 after 877 s; partial files later also quality-flagged R5).
3. **Operator footgun — 1 run**: `055018 (manager-r1)` — no `result.json`, `sam status` → `unknown`. Preceded by parent's `sam wait --timeout 50`; SKILL.md itself warns timed wait **attempts worker termination**.

### (b) Quality — 3 runs (all exit 0; would have shipped unchecked)
- `055630` lit worker: fabricated Yang-2020 quote, altered quotes, 7/18 "full-text" rows were metadata `.dat` files.
- `055638` design worker: "ideal pool of 117" from raw search hits.
- `055220` manager-r2: false "Phase 1 Complete" checkpoint into MEMORY; deferred defects to Phase 2.
- Caught only by parent `PARENT_REVIEW.md` R1–R4 + independent `audit_worker`.

### (c) Success — 13 runs
progress-n1 (3 runs incl. 2 resumes), auditor `062117`, checkpoint `065431`, recon-batch2/3-n2, corrective-design r1, sam-operator, audit-batch1/2/3-n2, access-partial-n4.

## 4. Rates

- Overall run success: **13/29 = 45%** (screenshot: 9/20 recent).
- Excluding pure-infra runs: **13/16 = 81%** of runs that actually executed.
- **13 of 29 runs were retries**; whole first package redone under corrective manager.
- Failed tasks generally have green twins: `recon-batch2/3-n1 ❌ → n2 ✅`, `audit-*-n1 ❌ → n2 ✅`, `access-gap-n2 ❌ → access-partial-n4 ✅`.

## 5. SAM tool reliability (as observed)

- `sam spawn`: **25/25 OK**. `sam kill`, `sam status`, `sam wait --timeout 0` correct.
- **Footgun 1:** timed `sam wait` terminates the worker (SKILL.md lines ~49/163/168/271 warn of this). Cost us manager-r1. After correction, 29 waits used `--timeout 0`.
- **Footgun 2:** `sam resume` failed **2 of 4** attempts: `no valid conversation_id pointer; use spawn not resume`. Root cause: agy runs killed by 429/network never persist `conversation_id` (`conversation_id: null` in result.json). Same for `sam restart` (1/1 fail).
- **Footgun 3:** `sam status` reported false **"possibly_stalled"** on agy stdout buffering (see `PROGRESS_CHECK.md`, `PROGRESS_CHECK_R3.md`).
- **Footgun 4 (ours, not SAM's):** workers that journal only at the end look stalled → progress-check agents needed → extra launches → extra load.

## 6. Quota timeline

- 07:31–07:43: 8 runs hit 429 (2 long-running mid-flight + 5 startup deaths + 1 more).
- Reset predicted ≈08:01; recovery **confirmed**: audits retried clean 07:50:53–07:51:30 (Flash-Medium); design r3 08:01:09 and access-n4 08:01:26 succeeded on **gemini-3.1-pro-high** (authorized fallback).
- No `QUOTA_RECOVERY.md` was ever created (referenced but missing).

## 7. Our current hypothesis (please attack this)

1. **~75% of infra failures = one shared Antigravity individual quota + mass parallel launches.** 429 wave is the dominant single cause (8/13).
2. **The 06:57 burst (4 network deaths) is likely the same amplification pattern**: many concurrent agy processes opening connections simultaneously → TLS/EOF. Early suspicion: these are launch-storm failures, NOT steady-state network unreliability, and NOT SAM logic bugs. **We are not fully certain — this is a key point to verify.**
3. SAM footguns (timed wait, resume-without-pointer, false stall) amplified loss but caused only ~1–2 outright deaths.
4. Quota alone cannot explain the earlier (06:57) failures — hence this review.

## 8. Open questions for you (the SAM session)

1. Is the 06:57 TLS/EOF burst **correlated with concurrency** (how many agy processes were alive at that instant), or something else (e.g. agy CLI bug, auth token refresh, proxy)?
2. Can SAM **stagger/space launches automatically** (launch ramp-up, max-concurrency cap, jittered retries with backoff)?
3. Can SAM **pre-flight quota check** before spawning (cheap call) and refuse to spawn when the reset window is near?
4. Can SAM **persist conversation_id earlier** (on session start rather than clean exit) so resume survives 429 kills?
5. Can `sam status` distinguish "buffered stdout" from a true stall to remove false positives?
6. Should timed `sam wait` be removed or renamed (e.g. `wait --kill-after`) so the footgun disappears?
7. Retry policy: should SAM auto-retry exit-3/429 and network-init failures once after backoff, marking them as infra-retries rather than agent failures?
8. What concurrency cap did you observe the account to actually sustain (we saw 4–5 parallel die; do 2–3 parallel survive)?

## 9. What we will do differently (pending your review)

- Cap concurrent agy agents at 2–3, stagger launches.
- Never use timed `sam wait`; liveness via `status` + worker early-journals.
- Treat exit-3/429 and <2-minute TLS/EOF deaths as **infra-retry**, not agent failure.
- Mandatory independent audit for any exit-0 research claim (quality failures were all exit-0).

---

## APPENDIX — Converged decisions (rounds 1–4 of cross-session review)

Rounds 1–3 corrections accepted (see SAM session's independent verification): wave re-timed
(07:31 mid-flight kills / 07:37–07:43 startup deaths / 08:01:09 post-reset death),
recon-batch1-n1 re-bucketed mid-flight, manager-r2 exit-0 struck (no result.json),
pro-high not a fallback, 8/13 = 62%, resume mechanism = wrapper SUCCESS-gate
(`agy_wrapper.py:344–346`), burst = 4 spawns in 0.257 s (rate, not headcount).

### Agreed implementation order (SAM side)
1. Spawn rate limiter in `proc.py` — global ≥15 s spacing, max 4 running, 45 s bounded block
   then instruction-formatted `spawn_deferred`, repeat-identical-spawn guard, `--no-space`
   bypass flag (per-invocation, logs `spacing_bypassed:true`)
2. SUCCESS-gate removal in `agy_wrapper.py:344` + resume message split
   (`resume_rejected` vs `resumed_then_failed`) — test: first-run-429 → pointer → resume
3. `result_partial` + `PARTIAL.md` (fixture unit test + live kill -9 smoke)
4. Retry queue + circuit breaker as ONE unit (SAM owns *when*; parent owns *what/whether*;
   `already_queued` guard) + `sam doctor --window`
5. Wait inversion — nonzero `--timeout N` detaches (exit 0, state `running`), `--kill-after N`
   kills (exit 4, state `killed`); `--timeout 0` pinned to wait-forever in both semantics
   (existing operator scripts unaffected); nonzero-timeout callers migrate; deprecation warning
   on old kill-on-timeout behavior during the window
6. Registry read-through backfill from result.json
7. Proc-tier liveness: pgid+start-time alive check → resource-delta movement → heartbeat bonus.
   Wording: silent-but-moving = `alive (no task signal Xm)`, never bare `stalled`.

### Agreed overrides policy
Any breaker override requires `--override-reason`; logged to registry; surfaced by `doctor`.

### Operator-side commitments (this session)
- Run the strace canary experiment **before** next storm, two `--no-space` spawns + control @20 s
- Migrate `wait_audit.sh` / `wait_round1.sh` after item 5 (audit for `--timeout 0` handling)
- `OPERATOR_QUEUE.md`: treat `awaiting_retry` as terminal-for-now; remove operator retry timestamps
- Treat "Resets in" strings as advisory only

### Program acceptance + housekeeping
- Whole-program acceptance: 7/7 amended tests green + one `doctor --window` trace showing
  zero spacing violations over a normal working day.
- Phantom-artifact rule: when the breaker lands, either `QUOTA_RECOVERY.md` becomes a real
  artifact it writes, or the skill stops telling parents to `cat` it. No third option.

### Co-sign
Both sessions converged and co-signed this appendix as the record of the review.
Review closed: items 1–7 undisputed, `--timeout 0` decided (wait-forever in both semantics),
acceptance criteria written, execution split agreed (SAM session builds 1–7 in order;
operator session owes: strace canary, wait-script re-check post-item-5, queue-discipline change).
Further rounds only if implementation surfaces something this appendix didn't foresee.
— SAM session: approved with edits 1–2 (applied) | Operator session: approved
