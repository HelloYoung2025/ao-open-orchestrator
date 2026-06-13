# Layer-3 Incident Protocol

How this system handles situations its vocabulary does not model — and how those
situations get repaired without corroding the gates.

## The three layers

| Layer | What it covers | Who acts |
| --- | --- | --- |
| **L1 — modeled vocabulary** | Typed states and actions the engine knows: dispatch, review receipts, blockers, repair loops, exhaustion, convergence, environment escalation | The engine, mechanically |
| **L2 — judgment within the contract** | Ambiguity, low confidence, residue, label disputes — anything resolvable with mechanisms that already exist (reconcile-from-state, per-pid dispatch, read-only codex_cc cross-validation, advisory absorption) | The orchestrator, autonomously |
| **L3 — out-of-vocabulary** | Situations that require a NEW mechanism, vocabulary entry, or wiring fix: the engine fails closed, or progress stalls with no modeled move available | The outer repair loop (below) — never the orchestrator alone |

The design intent: **L3 failure mode is "paused and visible", never "advances
wrongly"**. A stall is recoverable; silently forged progress is not.

## What counts as an L3 incident

Any of the following, observed on a live deployment:

1. **Fail-closed rejection not attributable to caller error AND carrying no
   modeled repair path** — e.g. `unsupported_requested_state_transition` on a
   transition the workflow genuinely needs, or `missing_ao_active_root` /
   contract-shape mismatches. A rejection that returns `allowed_repair_actions`
   (such as `unrecognized_governance_dirty` →
   `orchestrator_governance_dirty_review`) is L2 — run the modeled repair; it
   becomes L3 only when that modeled repair path itself fails.
2. **Stall signature** — live work demonstrably exists (a worker finished,
   a PR is open) but `list-ready` AND `list-gated` stay empty across
   consecutive sidecar sweeps.
3. **Perception gap** — runtime state the canonical layer cannot see
   (an agent TUI dialog awaiting input, a dead notifier leg, hook errors
   repeating on every tool call).
4. **Infra leg failure** — sidecar, notifier, or hook errors recurring across
   ticks (TCC denials, PATH resolution failures, dead-session deliveries).
5. **Any manual intervention during normal operation.** If an operator had to
   relay, poke, or hand-edit to keep a run moving, that intervention IS the
   incident — by definition something the machine should have owned.

## Role boundary (non-negotiable)

**The orchestrator detects and surfaces L3; it never repairs L3.**

On detecting an L3 signature, the orchestrator:

- emits a structured report through the needs-input notification path: what it
  attempted, the exact rejection reason or stall evidence, and current canonical
  state references (revision, target ids, proposal ids);
- continues every obligation NOT blocked by the incident;
- does **not** edit the engine, the contract, its own brain prompt, the
  canonical files outside its normal write lanes, and does not loosen, retry
  around, or reinterpret a fail-closed gate.

Why this boundary is the load-bearing wall: an agent that extends its own rule
system when the rules block it is the self-grading-gate anti-pattern — the
judge and the judged collapse into one process. The cross-review that caught a
caller-binding hole in this very protocol's first mechanism (`historical_closed`
without orchestrator-caller proof) exists only because the repairer was outside
the machine being repaired.

## The repair loop (mandatory order, no skipping)

1. **Root cause on live evidence.** Reproduce the failure verbatim (the exact
   rejection string, the exact missing path). No fix proposals before the root
   cause is demonstrated, not inferred.
2. **Written design proposal.** Established facts with evidence, the proposed
   mechanism, alternatives considered, and the explicit question set.
3. **Independent cross-review of the design** (codex or an equivalent reviewer
   that did not author the proposal). Findings are absorbed or explicitly
   rebutted — never silently dropped.
4. **Typed implementation.** A new capability enters as explicit vocabulary
   with its own fail-closed gates — never as a relaxation of an existing gate.
   If the new lane grants authority, it MUST bind the caller identity
   (the `AO_CALLER_TYPE` + contract-proof standard), not trust self-asserted
   fields. Regression tests must pin that every pre-existing gate still rejects
   what it rejected before.
5. **Independent cross-review of the implementation**, diff-based.
6. **Full test battery and the public safety scan, green.**
7. **Live verification** on the running deployment — the original failing
   operation now succeeds, and the surrounding loop demonstrably moves.
8. **Ledger entry** recording the whole chain: detection evidence, root cause,
   review verdicts, what shipped, what remains.

## Hard boundaries the loop must never cross

- Human-only gates stay human-only (plan-file modification, automation
  deletion). An L3 repair is never a justification to automate past them.
- Never bend a rule to fit the incident — change the mechanism or extend the
  vocabulary, and if a rule itself seems wrong, that is an owner decision, not
  a repair step.
- One incident, one typed fix. Bundled "while we're here" loosenings are how
  gates corrode.

## Convergence metric

The system is converging when **consecutive full cycles complete with zero
manual interventions** (seed → engine-rendered dispatch → worker apply →
review → closure → next obligation). Every manual touch during a cycle is
counted, attributed, and upstreamed as an L3 defect. A repair that reduces
incident count without closing the loop (a detector without an executor) is
not convergence — it is whack-a-mole, and this protocol exists to end that.
