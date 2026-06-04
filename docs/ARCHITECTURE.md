# Architecture

AO Open Orchestrator separates three responsibilities:

1. Runtime wakeup: an AO-like runtime decides when to call the CLI.
2. Canonical state: `StateWriter.apply()` is the only writer for state
   transitions and review receipts.
3. Profile transport: local CLIs, browser automation, or desktop adapters do
   external work and return bounded artifacts. This repository includes a
   reference GPT Pro browser/CDP adapter, but deployment-specific transport
   choice still belongs in a private profile/contract.

The core does not assume that any transport is always available. A failed
adapter must return a typed failure or submit a blocker proposal through the
state writer. It must not invent a pass.

## State

Canonical state lives under:

```text
.omx/state/ao-state-writer/state.json
.omx/state/ao-state-writer/state-transitions.jsonl
```

The JSON state carries `schema_version = 1`. Legacy v1 state with
`state_revision`, `targets`, and `proposal_results` is accepted and backfilled
on the next write.

## Actions

Actions are split into:

- `AUTO_SPAWN_ACTIONS`: safe tier-1 actions that can dispatch after preflight;
- `GATED_ACTIONS`: actions that require owner/orchestrator authorization;
- `NON_EXECUTABLE_ACTIONS`: convergence states that must not dispatch.

Unknown current obligations are reported as `unsupported_current_obligation`
with a repair envelope. They are never silently skipped.

## Runtime Boundary

The core does not keep a process alive by itself. If the host process exits,
the computer sleeps, or an external service disappears, a supervisor outside
this package must wake the loop again. The core guarantee is scoped to the
ticks it actually receives.

## Gate Semantics

Not every gate is human-only. GPT Pro review uses an orchestrator/owner-proxy
authorization path. Human-owner intervention is reserved for actions the
project contract names as human-only, such as master-plan edits or destructive
operations.
