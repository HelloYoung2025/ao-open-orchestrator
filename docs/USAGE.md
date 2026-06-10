# Usage Guide: Getting Productive and Day-to-Day Operation

[中文](USAGE.zh-CN.md)

Scope: [QUICKSTART.md](QUICKSTART.md) covers bootstrapping a project from zero; this guide covers
**how to use it once it exists** — a five-minute mental model, the operator command cookbook, how to
correctly wake the orchestrator, and a troubleshooting table. Everything here comes from real
production deployment experience.

## 0. Five-minute mental model

- All canonical state for a project lives under its ACTIVE_ROOT (`.omx/state/ao-state-writer/`),
  owned by a **single writer**. The engine has no daemon and no queue: every call is one command in,
  one JSON envelope out.
- The standard loop: a worker finishes → `apply`s a proposal (e.g. `evidence_pending`) → reports
  needs-input to the runtime → the runtime reaction pokes the orchestrator → the orchestrator
  reconciles (`list-ready` + `list-gated`) → `dispatch`es the next action → a new worker spawns.
  Every accepted proposal generates the next obligation; the loop drives itself.
- Two review tiers: small chapters go through codex review (tier-1, auto-authorized by the
  orchestrator's standing policy); major chapters go through escalated review (tier-2, the
  orchestrator must `authorize` then `dispatch`).
- Everything fails closed: unknown input, stale receipts, unauthorized callers, mismatched roots —
  all become **typed blockers** (machine-readable explicit refusals), never silent stalls. When you
  see a refusal envelope, read `reasons` first, then act.

## 1. Operator command cookbook

Every subcommand takes `--root <ACTIVE_ROOT>`. With the wheel installed use the `ao-state-writer`
console script, or the equivalent `python -m ao_state_writer.cli`.

**Read-only (run anytime, touches nothing):**

```bash
ao-state-writer list-ready --root <ROOT>        # which proposals are ready to dispatch
ao-state-writer list-gated --root <ROOT>        # which are waiting for tier-2 authorization
ao-state-writer reconcile-once --root <ROOT>    # full reconcile summary (incl. dispatch history)
ao-state-writer dispatch-stall-check --root <ROOT> \
  --last-seen-revision <N> --no-advance-count <K>   # stall detection; watermark is caller-owned
ao-state-writer watchdog --root <ROOT>          # review-timeout observation (dry-run evidence)
```

**State-writing (gated by orchestrator/operator identity):**

```bash
ao-state-writer dispatch --root <ROOT> --proposal-id <pid>     # dispatch one ready proposal
ao-state-writer authorize --root <ROOT> --proposal-id <pid> \
  --evidence <ref>                                             # tier-2 grant (dispatch in the same sweep)
ao-state-writer pause --root <ROOT>                            # state-visible global pause
ao-state-writer resume --root <ROOT>                           # resume
ao-state-writer reconcile-leases --root <ROOT> [--apply]       # orphaned-lease janitor (dry-run default)
ao-state-writer retire-dead-sessions --root <ROOT> [--apply]   # dead-session GC (dry-run default)
```

**Exit-code convention**: `0` OK; `2` malformed input; `3` fail-closed gate (blocked, authorization
required, etc. — the envelope carries `reasons` and `allowed_repair_actions`); `4` ambiguous (e.g.
bare `continue` with multiple ready proposals — use `dispatch --proposal-id` instead).
**`3` is not a crash** — it is the engine refusing by design.

## 2. Waking the orchestrator correctly (hard-earned lesson)

The orchestrator is event-driven: no poke, no movement. The **only reliable injection channel** is
the runtime's official send:

```bash
ao send <orchestrator-session> "<message>"
```

Field-tested caveats:

- Bare `tmux send-keys -t <session> ... Enter` (or `C-m`) does **not** work against an agent TUI —
  the text lands in the input box but the Enter never submits. Don't debug it; use `ao send`.
- `ao send` will **carry along any unsent text already sitting in the session's input box** and
  submit it together with your message. Usually harmless (they concatenate), but know it happens.
- When to poke manually: a ready queue has been sitting for hours with no dispatch (§3, first row);
  after the machine wakes from sleep; after a manual state repair; when a restored orchestrator is
  holding stale beliefs (explicitly void the outdated instruction inside your poke).

**Poke message template** (facts + request — never just "wake up"):

> Facts: worker X applied <state> at <time> (rev N); canonical now = …; list-ready currently = ….
> Request: reconcile from canonical state and dispatch per your standing policy.

## 3. Troubleshooting

| Symptom | Root cause | Action |
| --- | --- | --- |
| Ready proposal sits for hours, nothing dispatches | Wake chain broke: sidecar patrol dead / needs-input poke never delivered | Check the per-project sidecar log (`orchestrator-liveness.<project_id>.log`) → poke manually via `ao send` → long-term: use the v0.3.0 detached-revive sidecar |
| `blocked: unrecognized_governance_dirty:<file>` | A worker left governance files uncommitted | Orchestrator dispatches the governance-dirty review repair; or the operator verifies and commits |
| `requires_orchestrator_authorization` (exit 3) | Tier-2 action not yet granted | Orchestrator: `authorize` → `dispatch` in the same sweep |
| `ambiguous_proposal_id` (exit 4) | Bare `continue` with multiple ready proposals | Use `dispatch --proposal-id <pid>` per proposal |
| `stale_review_receipt_for_inactive_round` / `stale_escalated_review_gate` | A late or stale review receipt was refused | **No action needed** — the safety design working as intended; the current round's real receipt is unaffected |
| `escalated_review_actuator_busy` (exit 0) | Another project on this machine holds the external-review channel | Retry later; this project's lease was not consumed |
| Review worker timed out | The review exceeded its staged time budget | Don't re-run, don't forge a verdict: the worker stops and reports needs-input; the watchdog records a typed blocker and routes bounded repair |
| Sidecar revived the runtime, then the patrol went silent | Old synchronous revive (`ao start` is a foreground daemon that never returns, wedging the patrol loop) | Switch to the detached-revive `examples/orchestrator-liveness.sh` (v0.3.0) |
| Injected message gets no reaction | Bare tmux send-keys was used | See §2 — use `ao send` |

## 4. Multiple projects in parallel

One runtime daemon per machine multiplexes all registered projects; the external-review channel has
a machine-global mutex. The per-project deployment trio and the bring-up flow for project N+1 are in
[MULTI_PROJECT.md](MULTI_PROJECT.md).

## 5. Safety-boundary reminders

- The project's master plan file is the **only human-owner gate**: the orchestrator and all
  automation never edit it.
- The engine never deletes automation, including itself; deletions are always a hands-on operator
  action.
- Every `--apply` command defaults to dry-run — read the list before acting.
- Public publishing, merges, and production side effects are explicit operator/orchestrator-granted
  actions; the engine will not take them for you.
