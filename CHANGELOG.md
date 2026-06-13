# Changelog

## Unreleased

- One-click launch page (`scripts/launch_ui.py`): a local web form that drives the full
  QUICKSTART flow — bootstrap render, MASTER_PLAN goal/first-slice injection, config
  registration, sidecar load, detached `ao start`, optional first-slice seed poke. Every
  step is fail-closed with a visible rollback anchor. Boundaries: config.yaml is
  AUTO-CREATE ONLY (any existing content falls back to manual paste); MASTER_PLAN is
  written once at creation from the operator's own words and never touched again;
  localhost-only with Host/Origin checks, per-process CSRF token, and no-store responses.

## v0.3.0

Engine forward-sync: ports the full downstream anti-stall / integrity commit series, bringing
the public engine to symbol-and-behaviour parity with the production engine (verified by the
armed AST-parity and state shadow-replay gates with zero temporary allowlist entries).

Operator and observability surfaces:

- First-class operator pause: new `pause` / `resume` CLI subcommands set a state-visible
  `operator_pause` record (orchestrator-caller-proof gated). While paused, projection and
  side-effectful commands — including the preflight-bypassing canonical writers — refuse with
  an explicit `operator_paused` envelope instead of silently proceeding; a malformed pause
  record fails closed.
- Dispatch-stall escalation: new read-only `dispatch-stall-check` subcommand detects a ready
  obligation that persisted across N consecutive checks with no state-revision advance (the
  "detector but no dispatcher" stall). The watermark is caller-owned; the engine never writes.
- Dead-session GC: new `retire-dead-sessions` subcommand (with the `session_reaper` module)
  retires provably-dead worker sessions by writing their terminal lifecycle — fail-closed
  predicates, staleness floor, PR protection, read-only tmux snapshot, dry-run by default.

Review-routing and receipt-freshness invariants:

- Kind/scope routing guards: mis-kinded `evidence_pending` and mis-scoped codex_cc receipts
  are rejected at the source before any state mutation or stored-kind corruption.
- Receipt freshness: a content review receipt is only live while its target sits in the
  scope's active review state; anything else is `stale_review_receipt_for_inactive_round`.
  Closed and repair-exhausted targets are terminal against late receipts; a stale pass can no
  longer suppress a re-review watchdog timeout or downgrade an environment-unavailable parking.
- Escalated-review gate-deadlock fix and dispatch-lease accounting (in-flight lease counting).

Concurrency and dispatch integrity:

- The single-flight state lock is now an fcntl.flock advisory lock (kernel-released on kill);
  the manual stale-reclaim and its TOCTOU steal window are deleted whole.
- apply() appends the ledger BEFORE writing state, and persists target hints in
  `proposal_results`, so a crash between the two no longer strands an accepted obligation
  invisibly (crash-split orphans self-heal or are recovered exactly).
- Per-action dispatch-lease orphan floors: long-running review/actuator leases use a floor
  tracking the watchdog hard-timeout contract, so a live external review bridge can no longer
  be reclaimed mid-run and double-submitted.
- The consume-once `spawned` dispatch record persists BEFORE baseline verification, so a
  baseline-time crash cannot double-spawn a worker.
- The review-job actuator gate is state-aware: a post-timeout or crash-reclaimed actuate is
  refused before the external bridge runs, not after.

Multi-project deployment:

- Machine-global escalated-review actuator mutex: one external review bridge per machine.
  A second project's actuate attempt releases its own dispatch lease and reports
  `escalated_review_actuator_busy` (exit 0, in-flight semantics) instead of double-driving
  the shared bridge; the flock is kernel-released on holder death.
- Per-project sidecar isolation: the liveness sidecar's pidfile and log default to
  per-project names, and the plist template passes LOG_FILE explicitly, so a second
  project's sidecar no longer mistakes the first project's live sidecar for itself.
- `docs/MULTI_PROJECT.md` (+ zh-CN): single-daemon multiplexing topology, the per-project
  three-piece deployment set, the machine-global resource table, and the bring-up flow for
  project N+1. Two-project parallel smoke coverage (bootstrap rendering + concurrent engine
  writes, zero cross-contamination).

Sidecar hardening:

- Detached revive: the runtime launcher (`ao start`) runs as a foreground daemon that never
  exits on success, so the old synchronous revive permanently parked the patrol loop inside
  its first successful revive — one revive, then silence. The revive now launches detached,
  confirms success by the orchestrator session appearing (bounded wait), actually delivers
  the post-revival reconcile poke (previously dead code), and patrols on.

## v0.2.0

Reliability fixes ported from downstream production use, scrubbed to the public core.

- Verdict alias normalization: a reviewer's non-canonical `pass_with_advisory` token is now
  normalized to `advisory` before routing, so closure fires instead of recording a null next
  action (which would freeze the orchestrator). Recognized by both the writer apply path and
  the escalated review actuator classifier.
- Deterministic orphaned-lease janitor: new `reconcile-leases` CLI subcommand and
  `StateWriter.reclaim_orphaned_pending_leases`. When an orchestrator is killed between claim
  and confirm, its `pending` dispatch lease is orphaned; this lets a non-LLM caller
  (sidecar/cron) clear only the expired fast auto-spawn orphans, sparing fresh leases (race
  guard, re-checked under the single-flight lock) and external review/actuator leases (which
  legitimately hold `pending` far longer). Dry-run by default; pass `--apply` to delete.
- Phantom-obligation fail-closed: an accepted `proposal_results` entry that is missing from an
  existing ledger now resolves to not-current (was: current), so a stale/corrupt audit entry
  can no longer shadow healthy sibling obligations at the preflight chokepoint. An absent
  ledger file stays back-compat current.

Not ported (kept out of the minimal public core by design): downstream convergence-review cap
accounting and dispatch-kind audit stamping (the cap machinery is not part of the public core),
the read-time effective-exhaustion projection (depends on a decision field outside the public
core), and the governance-dirty git-status preflight (downstream-specific).

## v0.1.0

Initial public GitHub release of AO Open Orchestrator.

- Public-safe `ao-state-writer` reference implementation with append-only
  state transition evidence and consume-once dispatch records.
- Fail-closed compatibility checks for root identity, state schema version,
  contract version, and action vocabulary.
- Receipt-bound external review contracts for Codex cc and escalated review profile
  adapters, including caller identity, package hash, nonce, and artifact hash
  validation.
- Shared preflight/reconcile behavior for unsupported live obligations and
  governance-dirty blockers.
- Brand-neutral reference review actuator that shells out to a profile-owned
  reviewer command (no product-specific bridge bundled).
- Public-safety scan and regression tests for the core safety boundaries.
