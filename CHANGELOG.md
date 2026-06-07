# Changelog

## v0.2.0

Reliability fixes ported from downstream production use, scrubbed to the public core.

- Verdict alias normalization: a reviewer's non-canonical `pass_with_advisory` token is now
  normalized to `advisory` before routing, so closure fires instead of recording a null next
  action (which would freeze the orchestrator). Recognized by both the writer apply path and
  the GPT Pro desktop bridge classifier.
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
- Receipt-bound external review contracts for Codex cc and GPT Pro profile
  adapters, including caller identity, package hash, nonce, and artifact hash
  validation.
- Shared preflight/reconcile behavior for unsupported live obligations and
  governance-dirty blockers.
- Reference GPT Pro browser/CDP adapter packaged with the Python distribution.
- Public-safety scan and regression tests for the core safety boundaries.
