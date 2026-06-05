# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`ao-open-orchestrator` is the **public-safe core** of a fail-closed, repair-capable
orchestration loop for agentic project execution. It is a reference implementation,
not a runnable product: it expects an external AO-like runtime to wake/tick it, and
deployment-specific transport lives in a private profile/contract. Pure Python 3.10+,
**zero runtime dependencies** (`pytest` is the only dev dependency).

See [README.en.md](README.en.md) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the
conceptual model. The package ships one console script: `ao-state-writer`.

## Commands

```bash
# Dev setup (zero runtime deps; pytest is the only dev dep)
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -e ".[dev]"

# Verify (all three are the gate before any public commit)
python -m pytest -q                       # full suite
python -m pytest tests/test_public_core.py::test_name -q   # single test
python scripts/public_safety_scan.py      # MUST pass before publishing — see below
git diff --check                          # whitespace
```

`pyproject.toml` sets `pythonpath = ["src"]`, so tests import `ao_state_writer`
without an editable install, but the `ao-state-writer` console script does need one.

## Architecture

Three responsibilities are deliberately separated (see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)):

1. **Runtime wakeup** — an external supervisor decides *when* to call the CLI. The core
   does not keep a process alive; its guarantee is scoped only to ticks it receives.
2. **Canonical state** — [`StateWriter.apply()`](src/ao_state_writer/writer.py) is the
   *single* writer for all state transitions and review receipts.
3. **Profile transport** — local CLIs, browser, or desktop adapters do external work and
   return bounded, typed artifacts. The bundled GPT Pro browser/CDP bridge
   (`src/ao_state_writer/gpt_pro_desktop_bridge.py`, `desktop_bridge/`) is a *reference
   adapter*, not safe on every machine.

Module map under `src/ao_state_writer/`:

- [`writer.py`](src/ao_state_writer/writer.py) — `StateWriter`: proposal → accept/reject
  decision, append-only ledger, single-flight file lock, consume-once dispatch ledger,
  orchestrator-authorization API.
- [`cli.py`](src/ao_state_writer/cli.py) — argparse entrypoint (`main`). Subcommands:
  `apply`, `continue`, `dispatch`, `list-ready`, `list-gated`, `reconcile-once`,
  `review-job`, `gpt-pro-actuate`, `authorize`, `preflight`, `repair-todo`, `watchdog`.
  Also `preflight_reconcile` (the shared read-only fail-closed guard) and GPT Pro actuation.
- [`continuation.py`](src/ao_state_writer/continuation.py) — the action vocabulary
  (`AUTO_SPAWN_ACTIONS` / `GATED_ACTIONS` / `NON_EXECUTABLE_ACTIONS`), prompt rendering,
  and the actual `ao spawn` subprocess.
- [`compat.py`](src/ao_state_writer/compat.py) — schema/contract version + action-vocabulary
  + root-identity checks; reads `DIRECT_PROJECT_CONTRACT.toml`.
- [`preflight.py`](src/ao_state_writer/preflight.py) — governance-file dirty check.
- [`watchdog.py`](src/ao_state_writer/watchdog.py) — review-timeout thresholds → typed blocker proposals.
- [`todo.py`](src/ao_state_writer/todo.py) — `## Current Execution State` compact-block render/validate/repair.

Canonical state lives at `<root>/.omx/state/ao-state-writer/{state.json,state-transitions.jsonl}`
(`schema_version = 1`). Per-project policy lives in `DIRECT_PROJECT_CONTRACT.toml` at the root
(see [examples/DIRECT_PROJECT_CONTRACT.example.toml](examples/DIRECT_PROJECT_CONTRACT.example.toml)).

## Critical conventions (non-obvious, fail-loudly)

- **`StateWriter.apply()` is the only canonical writer.** Never mutate `state.json` or the
  ledger directly; every transition is a `StateTransitionProposal`. A proposal is accepted
  exactly once (replayed by `proposal_id`).
- **Fail closed, never silently skip.** Unknown schema version, contract version ≠ 1,
  action-vocabulary mismatch, non-canonical/missing root, missing receipts, and unauthenticated
  callers become typed JSON blockers — they are not skipped. This is the central design rule;
  preserve it in any change.
- **Caller identity is environment-injected, never self-asserted.** Orchestrator
  authorization requires `AO_CALLER_TYPE=orchestrator` *and* a session matching the contract's
  `continuation_policy.orchestrator_session`; review receipts require the scope's expected
  caller type (`codex_cc`, `gpt_pro_review_actuator`, `watchdog`). See `CALLER_TYPE_ENV` /
  `SESSION_ID_ENV` in `compat.py`.
- **The action-vocabulary tuples in `continuation.py` are a compatibility contract.** If you
  change `AUTO_SPAWN_ACTIONS` / `GATED_ACTIONS` / `NON_EXECUTABLE_ACTIONS`, you must also update
  `[continuation_policy]` in `examples/DIRECT_PROJECT_CONTRACT.example.toml` and the tests — a
  divergence is rejected as `contract_action_vocabulary_mismatch`.
- **`proposal_results` is append-only audit history, not a queue.** Live obligations are
  recomputed (latest accepted revision per target, live GPT Pro gate pointer); do not treat it
  as a worklist.
- **GPT Pro receipts are cryptographically bound** to package sha256, submission nonce, artifact
  sha256, gate proposal id, and caller identity; artifacts must resolve under `<root>/reports/`
  (path traversal is rejected). Major closure independently re-validates the receipt verdict
  (`_gpt_pro_closure_receipt_guard` in `cli.py`).
- **CLI exit codes are part of the contract:** `0` success/no-op, `2` rejected proposal/receipt,
  `3` fail-closed blocker (compat/preflight/authorization), `4` unknown/ambiguous proposal id.
- **Keep `dependencies = []` in `pyproject.toml`.** This core intentionally has no third-party
  runtime deps.

## Public-safety boundary

This repo is the publishable extraction — no private runtime state, local receipts, transcripts,
or machine-specific paths. `scripts/public_safety_scan.py` enforces forbidden patterns (absolute
home paths, private project identifiers, secret tokens, etc.). **Run it before any commit intended
to be public**; a finding fails the publish gate.

## Docs are bilingual — keep pairs in sync

Documentation ships as English + Simplified-Chinese pairs: `README.md` (zh-CN, primary) /
`README.en.md`, and `docs/*.md` (en) / `docs/*.zh-CN.md`. `.github/` templates are
Chinese-primary with English support text. When you change one language, update its counterpart.
