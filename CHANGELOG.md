# Changelog

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
