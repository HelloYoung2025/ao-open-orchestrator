from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar
import fcntl
import hashlib
import json
import os
import re
import time
import uuid

from .compat import (
    CALLER_TYPE_ENV,
    SESSION_ID_ENV,
    UnsupportedStateSchemaVersion,
    empty_state,
    normalize_state,
    read_contract_active_root,
    read_contract_project_id,
    read_contract_string,
)


ACCEPTED_ADVISORY_VERDICTS = {"pass", "pass_with_nits", "advisory"}
REVIEW_VERDICTS_WITH_BLOCKER = ACCEPTED_ADVISORY_VERDICTS | {"blocker"}
# Canonical review-verdict vocabulary. Reserved: the verdict-rejection gate that fail-closes a
# non-canonical verdict is added in a later writer.py slice; this token set is dormant until then.
CANONICAL_REVIEW_VERDICTS = REVIEW_VERDICTS_WITH_BLOCKER
# Exact, narrow aliases for non-canonical "pass-with-a-note" tokens a reviewer may emit. They are
# normalized to the canonical token BEFORE validation/routing so closure routing fires. Without this,
# a non-canonical verdict matches neither the advisory branches nor the blocker branch, so the
# obligation records next_required_action=None and dispatch cannot route a null action (the
# orchestrator freezes). Keep this map exact and minimal: never alias a token that could carry
# blocker semantics.
_REVIEW_VERDICT_ALIASES = {"pass_with_advisory": "advisory"}
REVIEW_SCOPE_ACTORS = {
    "codex_cc": "codex_cc",
    "escalated_review": "escalated_review",
}
REVIEW_SCOPE_CALLER_TYPES = {
    "codex_cc": "codex_cc",
    "escalated_review": "escalated_review_actuator",
}
WATCHDOG_CALLER_TYPE = "watchdog"
CODEX_CC_MODEL = "gpt-5.5"
CODEX_CC_REASONING_EFFORT = "xhigh"
CODEX_CC_TRANSCRIPT_ARTIFACT_REF_PREFIX = "artifact:reports/codex-cc-receipts/"
CODEX_CC_TIMEOUT_BLOCKER_CODE = "codex_cc_review_timeout"
ESCALATED_REVIEW_WATCHDOG_TIMEOUT_BLOCKER_CODES = {
    "escalated_review_uncertain",
    "escalated_review_hard_timeout",
}
ESCALATED_REVIEW_ACTUATOR_FAILURE_BLOCKER_CODE = "escalated_review_actuator_failure"
# Blocker codes that record an ENVIRONMENT/actuator/review-timeout fault rather than a real CONTENT
# verdict: the actuator never reached the reviewer, or the review timed out / came back uncertain, so
# no content judgement was produced. _accept routes these through a dedicated environment lane that is
# EXCLUDED from the per-target content-exhaustion budget (so a run of transient env faults can never
# inflate a target into a false "exhaustion"); past the cumulative env bound it escalates to the
# owner-visible review_environment_unavailable obligation. The final-convergence guard re-excludes this
# set as defense in depth, so even a legacy/migrated env-inflated exhaustion cannot push a
# still-converging target into recorded convergence.
ENVIRONMENTAL_BLOCKER_CODES = ESCALATED_REVIEW_WATCHDOG_TIMEOUT_BLOCKER_CODES | {
    CODEX_CC_TIMEOUT_BLOCKER_CODE,
    ESCALATED_REVIEW_ACTUATOR_FAILURE_BLOCKER_CODE,
}
# The --outcome token a record-final-convergence call must carry. It is an orchestrator-decision
# label on an exhausted repair ladder, NOT a continuation action token (it is never matched by
# compat.validate_contract_compat, which only checks the three continuation_policy action arrays).
FINAL_CONVERGENCE_OUTCOME = "repair_ladder_exhausted"
# A spawn_session_id attestation parsed from the engine's SESSION=<id> line must match this shape.
# Used by reconcile-spawn-attestation to validate an orchestrator-supplied session id.
AO_SPAWN_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
# dispatched_proposals statuses that mean a proposal_id has already been CONSUMED — the preflight
# loop skips it (is_dispatched True) and claim_dispatch refuses to re-claim it. "spawned" is the
# ordinary successful-dispatch terminal; "spawned_unattested" is a dispatch whose SESSION= line was
# missing and is awaiting reconcile-spawn-attestation; "final_convergence_recorded" is the
# owner-proxy convergence decision layered on an exhausted repair token by record-final-convergence.
# "spawned_base_mismatch" / "spawned_base_unverified" are spawn-baseline leases written by
# record_spawn_baseline_issue: a spawned worker whose worktree cannot prove the required source
# baseline is consumed here (never re-spawned on a known-bad baseline) and surfaced as an
# owner-visible obligation by _current_obligation_issue. reconcile_spawned_dispatch RELEASES such a
# lease under --release-if-terminal + terminal proof (b3a). NOTE: the DETECTION call site (the spawn
# flow that writes these leases) and the CLI ao-session kill readback that produces the live
# terminal_readback are wired in a later slice; the writer release path itself is live.
CONSUMED_DISPATCH_STATUSES = frozenset(
    {
        "spawned",
        "spawned_unattested",
        "final_convergence_recorded",
        "spawned_base_mismatch",
        "spawned_base_unverified",
    }
)
# Reserved: consumed by the convergence-review / spawned-dispatch reconciliation added in later
# writer.py + cli.py slices. dispatch_kind is an INDEPENDENT audit field, never a status value.
CONVERGENCE_REVIEW_DISPATCH_STATUSES = {"spawned", "spawned_unattested"}
DISPATCH_KIND_CONVERGENCE_REVIEW = "convergence_review"
DISPATCH_KIND_NON_CONVERGENCE = "non_convergence_spawn"
DISPATCH_KINDS = frozenset({DISPATCH_KIND_CONVERGENCE_REVIEW, DISPATCH_KIND_NON_CONVERGENCE})
SPAWNED_DISPATCH_RECONCILE_ACTIONS = {
    "dispatch_next_slice_plan_mode",
    "repair_active",
    "state_writer_closure",
    "major_closure_candidate",
    "repair_attempts_exhausted",
}
# A normal "spawned" dispatch lease older than this is treated as overdue/stalled and may be
# released by reconcile-spawned-dispatch --release-if-terminal. Same floor as the orphan-lease
# reclaim age (STALE_PENDING_DISPATCH_SECONDS, defined below) — both bound a stuck dispatch lease.
SPAWNED_DISPATCH_STALL_SECONDS = 600.0
# Bounded auto-repair: at most this many repair dispatches per (target, blocker_code) before the
# next blocker escalates to a human instead of looping repair <-> review indefinitely.
MAX_REPAIR_ATTEMPTS = 2
# Per-target ceiling across ALL blocker_codes, so an agent varying the blocker_code cannot dodge escalation.
MAX_REPAIR_ATTEMPTS_TOTAL = 4
# Cumulative environment-fault ceiling. _accept's environment lane escalates to the owner-visible
# review_environment_unavailable obligation once a target's cumulative env faults exceed this bound.
MAX_ENVIRONMENT_ATTEMPTS = 5
# Per-target convergence-review caps. Reserved: the convergence-cap subsystem that consumes them
# is added in a later writer.py slice.
MAX_CONVERGENCE_REVIEWS_PER_TARGET_BLOCKER = 1
MAX_CONVERGENCE_REVIEWS_PER_TARGET_TOTAL = 4
# C-FIX-7: a genuine CONTENT review receipt for a given scope is only "live" while its target sits in
# that scope's active review state. Mirrors watchdog.py's _ACTIVE_REVIEW_STATE_BY_SCOPE (kept as a
# separate writer-local copy to avoid a writer<->watchdog import cycle).
_ACTIVE_REVIEW_STATE_BY_SCOPE = {"codex_cc": "evidence_pending", "escalated_review": "escalated_review_pending"}
# Bounded backoff for the single-flight state lock so brief contention retries instead of failing hard.
LOCK_TIMEOUT_SECONDS = 10.0
LOCK_INITIAL_BACKOFF_SECONDS = 0.05
LOCK_MAX_BACKOFF_SECONDS = 0.5
# A "pending" dispatch claim older than this is treated as an orphaned lease (the
# claimer was killed between claim_dispatch and confirm/release) and may be reclaimed,
# so a hard SIGKILL in that window can never deadlock a slice forever. Comfortably
# larger than the ~120s `ao spawn` timeout.
STALE_PENDING_DISPATCH_SECONDS = 600.0
# C-FIX-10: an actuator/review lease (the escalated-review bridge, a codex_cc review) legitimately
# holds a 'pending' claim FAR longer than the 600s fast-spawn floor, so claim_dispatch must use a
# longer orphan floor for these — otherwise a reconcile re-dispatch in the 600s..bridge-timeout window
# reclaims the LIVE lease (is_dispatched() does not count 'pending') and launches a SECOND external
# actuation (double-submit; the external side effect is not consume-once). This set is exactly the
# one the reconcile-leases janitor already excludes via cli._is_spawned_dispatch_reconcile_action.
LONG_RUNNING_DISPATCH_LEASE_ACTIONS = frozenset({"codex_cc_review", "escalated_review"})
# The floor tracks the WATCHDOG hard-timeout contract (ESCALATED_REVIEW_HARD_TIMEOUT_MINUTES = 120 =
# 7200s), which is the real liveness bound on an escalated-review obligation: at 7200s the watchdog
# emits a timeout blocker that moves the target out of escalated_review_pending, so the gate stops
# being live and no autonomous re-dispatch occurs past it. A lease still 'pending' beyond this floor
# is therefore provably orphaned and safe to reclaim. (It deliberately does NOT track a per-invocation
# `--bridge-timeout-seconds` override: the watchdog, not the subprocess timeout, bounds a LEGITIMATE
# obligation, and an operator override cannot extend that contract.)
LONG_RUNNING_PENDING_DISPATCH_SECONDS = 7800.0


@dataclass(frozen=True)
class StateTransitionProposal:
    proposal_id: str
    target_kind: str
    target_id: str
    base_state_revision: int
    requested_state: str
    actor_role: str
    evidence_refs: list[str] | None = None
    summary: str = ""
    review_scope: str = "none"
    verdict: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    review_mode: str | None = None
    blocker_code: str | None = None
    blocker_detail: str | None = None
    package_path: str | None = None
    prompt_path: str | None = None
    package_sha256: str | None = None
    non_retryable: bool = False
    codex_cc_transcript_sha256: str | None = None
    codex_cc_transcript_artifact_ref: str | None = None
    external_review_receipt_sha256: str | None = None
    external_review_submission_nonce: str | None = None
    external_review_artifact_ref: str | None = None
    external_review_gate_proposal_id: str | None = None


@dataclass(frozen=True)
class StateTransitionDecision:
    decision: str
    proposal_id: str
    reason: str
    state_revision: int
    new_state: str | None = None
    next_required_action: str | None = None
    blocker_code: str | None = None
    non_retryable: bool = False
    replayed: bool = False


# C-FIX-9: keys persisted ALONGSIDE a decision inside a proposal_results entry that are NOT
# StateTransitionDecision fields. apply() stores the obligation's target here so a crash-split orphan
# (state.json advanced, ledger append lost to a kill) can be resolved to its target WITHOUT the
# ledger. Single source of truth: EVERY site that reconstructs a StateTransitionDecision from a stored
# proposal_results entry (writer._decision_from_stored AND cli._projected_decision) MUST strip these,
# or the **splat raises "unexpected keyword argument". `replayed` is also stripped at reconstruction.
DECISION_STORED_HINT_KEYS = ("target_id", "target_kind")


def _dispatch_is_convergence_review(record: Any, stored: Any) -> bool:
    """True iff a dispatched_proposals record represents a bounded convergence-review spawn — the
    unit the convergence caps count.

    For records stamped at dispatch time the persisted ``dispatch_kind`` is authoritative: a
    ``convergence_review`` stamp counts even if the obligation's raw stored action later projects to
    ``repair_active``, and a ``non_convergence_spawn`` stamp never counts even if its raw stored
    action later crosses the content threshold. Legacy records (written before dispatch_kind) fall
    back to the historical raw-join (stored ``next_required_action == repair_attempts_exhausted``),
    but explicitly exclude any ENVIRONMENTAL_BLOCKER_CODES-coded record so an env fault never consumes
    a target's convergence budget. (Caveat: for an env-inflated CONTENT legacy record the fallback
    still over-counts; only NEW dispatches are stamp-corrected.)
    """
    kind = record.get("dispatch_kind") if isinstance(record, dict) else None
    if kind is not None:
        return kind == DISPATCH_KIND_CONVERGENCE_REVIEW
    if not (isinstance(stored, dict) and stored.get("next_required_action") == "repair_attempts_exhausted"):
        return False
    if stored.get("blocker_code") in ENVIRONMENTAL_BLOCKER_CODES:
        return False
    return True


class StateWriter:
    """Single authority for AO clone canonical state-transition decisions."""

    _STATE_ROOT_PARTS: ClassVar[tuple[str, str, str]] = (".omx", "state", "ao-state-writer")

    def __init__(self, *, state_path: Path, ledger_path: Path):
        self.state_path = state_path
        self.ledger_path = ledger_path

    @staticmethod
    def _decision_from_stored(
        stored: dict, *, replayed: bool = False
    ) -> StateTransitionDecision:
        """Reconstruct a StateTransitionDecision from a proposal_results entry.

        C-FIX-9 persists two hint keys (target_id/target_kind) ALONGSIDE the decision in
        proposal_results so a crash-split orphan (state.json advanced but the ledger append did not
        run) can still be resolved to its target by the dispatch chokepoint WITHOUT the ledger. Those
        hints are NOT StateTransitionDecision fields, so strip them (and any stored `replayed`)
        before constructing the frozen dataclass, which keeps both the dataclass and the ledger's
        decision record byte-identical to the pre-C-FIX-9 shape.
        """
        fields = {
            k: v
            for k, v in stored.items()
            if k not in DECISION_STORED_HINT_KEYS and k != "replayed"
        }
        return StateTransitionDecision(**fields, replayed=replayed)

    def apply(self, proposal: StateTransitionProposal) -> StateTransitionDecision:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with self._single_flight():
            try:
                state = self._read_state()
            except UnsupportedStateSchemaVersion:
                return StateTransitionDecision(
                    decision="rejected",
                    proposal_id=proposal.proposal_id,
                    reason="unsupported_state_schema_version",
                    state_revision=proposal.base_state_revision,
                )
            replay = state["proposal_results"].get(proposal.proposal_id)
            if replay is not None:
                return self._decision_from_stored(replay, replayed=True)

            # Normalize an exact non-canonical review-verdict alias (e.g. pass_with_advisory)
            # to its canonical token BEFORE validation and routing, so closure routing and the
            # caller/proof gates all see the canonical verdict. The proposal is frozen, so rebind
            # to a replaced copy.
            if (
                proposal.review_scope in REVIEW_SCOPE_ACTORS
                and proposal.verdict in _REVIEW_VERDICT_ALIASES
            ):
                proposal = replace(
                    proposal, verdict=_REVIEW_VERDICT_ALIASES[proposal.verdict]
                )

            rejection = self._rejection(proposal, state)
            if rejection is not None:
                return rejection

            target = state["targets"].setdefault(
                proposal.target_id,
                {"kind": proposal.target_kind, "state": "planned", "review_receipts": []},
            )
            decision = self._accept(proposal, state, target)
            if decision.next_required_action is None:
                # Invariant: an accepted transition MUST carry a routable next_required_action.
                # A fall-through in _accept (an unmodeled requested_state/verdict/review_scope/
                # target_kind tuple) would otherwise be stored as an accepted obligation with a
                # null action, which the dispatch chokepoint treats as unsupported_current_obligation
                # and freezes the whole loop on the non-executable orchestrator_vocab_review repair
                # (a verdict-vocab stall via the requested_state axis). Fail closed here, before any
                # state mutation/persist, exactly as _rejection does. This only prevents NEW frozen
                # obligations; it does not repair historical null-action records already persisted in
                # state.json.
                return self._reject(proposal, state, "unsupported_requested_state_transition")
            target["state"] = decision.new_state
            target["kind"] = proposal.target_kind
            target.setdefault("evidence_refs", [])
            target["evidence_refs"].extend(proposal.evidence_refs or [])
            target["evidence_refs"] = sorted(set(target["evidence_refs"]))
            if proposal.review_scope != "none" or proposal.verdict is not None:
                target.setdefault("review_receipts", []).append(self._review_receipt(proposal))
            if proposal.requested_state == "escalated_review_pending" and (
                proposal.package_path or proposal.prompt_path or proposal.package_sha256
            ):
                target["escalated_review_package"] = {
                    "package_path": proposal.package_path,
                    "prompt_path": proposal.prompt_path,
                    "package_sha256": proposal.package_sha256,
                }
                target["active_escalated_review_gate_proposal_id"] = proposal.proposal_id
                state.setdefault("escalated_review_gates", {})[proposal.proposal_id] = {
                    "proposal_id": proposal.proposal_id,
                    "target_id": proposal.target_id,
                    "target_kind": proposal.target_kind,
                    "package_path": proposal.package_path,
                    "prompt_path": proposal.prompt_path,
                    "package_sha256": proposal.package_sha256,
                }

            state["state_revision"] += 1
            stored = asdict(decision)
            stored["state_revision"] = state["state_revision"]
            stored.pop("replayed", None)
            # C-FIX-9 Part B: persist the obligation's target alongside the decision so a crash-split
            # orphan (state.json written, ledger append not yet run) can still be resolved to its
            # target by the dispatch chokepoint WITHOUT the ledger. These hint keys live only in
            # proposal_results; _decision_from_stored strips them before rebuilding the frozen
            # StateTransitionDecision, so neither the dataclass nor the ledger decision record changes.
            stored["target_id"] = proposal.target_id
            stored["target_kind"] = proposal.target_kind
            state["proposal_results"][proposal.proposal_id] = stored
            final_decision = self._decision_from_stored(stored)
            # C-FIX-9 Part A: append the ledger BEFORE persisting state.json. A hard kill between the
            # two now leaves the ledger AHEAD of state (proposal in the ledger, NOT in proposal_results);
            # enumeration iterates proposal_results so it ignores the orphan ledger line, and a worker
            # re-submit finds no proposal_results entry -> no replay short-circuit -> re-applies cleanly
            # (a harmless duplicate ledger line; last-wins for every ledger consumer). The reverse order
            # (state-then-ledger) instead stranded a LEGITIMATE accepted obligation invisibly with NO
            # self-heal (replay short-circuits the re-apply) -> permanent unattended stall.
            self._append_ledger(proposal, final_decision)
            self._write_state(state)
            return final_decision

    def _rejection(
        self, proposal: StateTransitionProposal, state: dict[str, Any]
    ) -> StateTransitionDecision | None:
        if proposal.base_state_revision != state["state_revision"]:
            return self._reject(proposal, state, "stale_revision")
        expected_actor = REVIEW_SCOPE_ACTORS.get(proposal.review_scope)
        if expected_actor is not None and proposal.actor_role != expected_actor:
            return self._reject(proposal, state, "invalid_review_actor")
        # Fail closed on an unrecognized review verdict (after alias normalization in apply).
        # An accepted-but-unrouted verdict would record next_required_action=None and freeze
        # the orchestrator on orchestrator_vocab_review, so reject it here instead.
        if (
            proposal.review_scope in REVIEW_SCOPE_ACTORS
            and proposal.verdict is not None
            and proposal.verdict not in CANONICAL_REVIEW_VERDICTS
        ):
            return self._reject(proposal, state, "unsupported_review_verdict")
        # A blocker verdict drives the repair-attempt accounting in _accept (repair_attempts /
        # repair_attempts_total -> the repair_attempts_exhausted escalation), and apply() records a
        # review receipt whose scope is copied from proposal.review_scope. But the review-actor,
        # canonical-verdict, codex_cc model/effort, caller-type, and transcript gates are ALL keyed on a
        # review_scope in REVIEW_SCOPE_ACTORS. An unscoped (or forged-scope) blocker would bypass every
        # one of those authentication gates yet still manufacture repair attempts and a forged
        # scope review receipt. Only a genuine review actor may emit a blocker; fail closed otherwise.
        if proposal.verdict == "blocker" and proposal.review_scope not in REVIEW_SCOPE_ACTORS:
            return self._reject(proposal, state, "unscoped_blocker_verdict")
        if not proposal.evidence_refs:
            return self._reject(proposal, state, "missing_evidence")
        # Reserve the environment-fault blocker codes to their legitimate producers. _accept routes by
        # blocker_code ALONE into the environment lane (excluded from content accounting, eventually
        # escalating to review_environment_unavailable). Without this gate a normal or forged reviewer
        # could stamp a reserved env code on an ordinary proposal to dodge the content give-up budget and
        # silently enter the env lane / falsely escalate. Fail closed on a (scope, mode) that is not the
        # code's legitimate producer.
        if (
            proposal.blocker_code in ENVIRONMENTAL_BLOCKER_CODES
            and not self._is_legitimate_environment_blocker(proposal)
        ):
            return self._reject(proposal, state, "reserved_environment_blocker_code_mismatch")
        if proposal.review_scope == "codex_cc" and proposal.blocker_code != CODEX_CC_TIMEOUT_BLOCKER_CODE:
            if proposal.model != CODEX_CC_MODEL:
                return self._reject(proposal, state, "invalid_codex_cc_model")
            if proposal.reasoning_effort != CODEX_CC_REASONING_EFFORT:
                return self._reject(proposal, state, "invalid_codex_cc_reasoning_effort")
        if proposal.review_scope == "codex_cc" or proposal.review_mode == "watchdog_timeout":
            caller_rejection = self._review_caller_rejection(proposal)
            if caller_rejection is not None:
                return self._reject(proposal, state, caller_rejection)
        if proposal.review_scope == "codex_cc" and proposal.verdict in ACCEPTED_ADVISORY_VERDICTS:
            if not proposal.codex_cc_transcript_sha256:
                return self._reject(proposal, state, "missing_codex_cc_transcript")
            if not proposal.codex_cc_transcript_artifact_ref:
                return self._reject(proposal, state, "missing_codex_cc_transcript_artifact")
            artifact_rejection = self._codex_cc_transcript_artifact_rejection(proposal)
            if artifact_rejection is not None:
                return self._reject(proposal, state, artifact_rejection)
        if proposal.review_scope == "escalated_review" and proposal.verdict in ACCEPTED_ADVISORY_VERDICTS.union(
            {"blocker"}
        ):
            if self._is_escalated_review_watchdog_timeout(proposal):
                pass
            elif self._is_escalated_review_actuator_failure(proposal):
                if not (proposal.package_sha256 and proposal.external_review_gate_proposal_id):
                    return self._reject(proposal, state, "missing_escalated_review_actuator_failure_proof")
                caller_rejection = self._review_caller_rejection(proposal)
                if caller_rejection is not None:
                    return self._reject(proposal, state, caller_rejection)
                authorization_rejection = self._escalated_review_authorization_rejection(proposal, state)
                if authorization_rejection is not None:
                    return self._reject(proposal, state, authorization_rejection)
            elif not (
                proposal.package_sha256
                and proposal.external_review_receipt_sha256
                and proposal.external_review_submission_nonce
                and proposal.external_review_artifact_ref
                and proposal.external_review_gate_proposal_id
            ):
                return self._reject(proposal, state, "missing_escalated_review_receipt_proof")
            else:
                caller_rejection = self._review_caller_rejection(proposal)
                if caller_rejection is not None:
                    return self._reject(proposal, state, caller_rejection)
                authorization_rejection = self._escalated_review_authorization_rejection(proposal, state)
                if authorization_rejection is not None:
                    return self._reject(proposal, state, authorization_rejection)
                artifact_rejection = self._external_review_artifact_rejection(proposal)
                if artifact_rejection is not None:
                    return self._reject(proposal, state, artifact_rejection)
        if (
            proposal.target_kind == "small_chapter"
            and proposal.requested_state == "closed"
            and not self._has_codex_cc_receipt(state, proposal.target_id)
        ):
            return self._reject(proposal, state, "missing_codex_cc_receipt")
        if (
            proposal.target_kind == "major_chapter"
            and proposal.requested_state == "closed"
            and not self._has_escalated_review_receipt(state, proposal.target_id)
        ):
            return self._reject(proposal, state, "missing_escalated_review_receipt")
        if proposal.requested_state == "escalated_review_pending":
            # escalated_review_pending is the MAJOR-chapter external-review tier ONLY (small chapters
            # use evidence_pending + codex_cc — see continuation's small-chapter closure path). A
            # non-major request is a mis-routing: _accept() would otherwise route ANY kind to
            # escalated_review and set target.state=escalated_review_pending; the inferred-live-gate
            # logic would then mark it a live gate that suppresses ALL sibling work, yet it can never
            # be cleared (no package -> record_authorization rejects missing_escalated_review_package)
            # => permanent dispatch-loop deadlock. Reject at the source so one mis-routed proposal
            # cannot brick the loop.
            if proposal.target_kind != "major_chapter":
                return self._reject(proposal, state, "escalated_review_requires_major_chapter")
            if not (proposal.package_path and proposal.prompt_path and proposal.package_sha256):
                return self._reject(proposal, state, "missing_escalated_review_package_receipt")
        # Same-class kind/scope routing guards (mirror siblings of escalated_review_requires_major_chapter
        # above). _accept() routes evidence_pending and the codex_cc/escalated-review advisory-pass
        # receipts to closure actions by requested_state / review_scope ALONE — kind-agnostically — and
        # apply() then unconditionally overwrites target["kind"] from the proposal. Without these guards a
        # mis-kinded proposal is accepted into the WRONG review lane (and can silently corrupt an existing
        # target's recorded kind), yielding a closure action that can never be satisfied (e.g. a major
        # routed to the small-chapter state_writer_closure can never clear the major close's
        # missing_escalated_review_receipt, and a major mislabeled small bypasses escalated review
        # entirely). Reject every mis-kinded routing at the source, before any state mutation.
        # (a) A proposal must never contradict an existing target's recorded kind. kind is immutable once
        #     set; apply() would otherwise overwrite it -> silent kind corruption + cross-lane misroute.
        existing_target = state.get("targets", {}).get(proposal.target_id)
        if existing_target is not None:
            # (a0) PARKED states are terminal for REVIEW receipts. Two target states park a chapter
            #      awaiting either owner action or a fresh WORK re-open: `closed` (C-FIX-5) and
            #      `repair_attempts_exhausted` (C-FIX-6 Finding 1 — advanced ONLY via the
            #      record-final-convergence CLI, never an apply() review receipt). Each MAY still be
            #      legitimately re-opened by a fresh WORK proposal: a worker evidence_pending re-open is
            #      the designed obligation-supersession path (reconcile then files the prior obligation
            #      under historical_dispatch_records / current_obligation=false). But a stale/adversarial
            #      REVIEW receipt (anything carrying a review_scope or a verdict) must NEVER reopen a
            #      parked target: without this guard a late codex_cc receipt or a late escalated-review
            #      blocker reusing a consumed gate routes through _accept's blocker/closure branches and
            #      apply() overwrites target["state"], dragging a done/parked chapter back into
            #      review_blocked/repair_active or closure_candidate and silently erasing the
            #      owner-visible parking (final-convergence / closed). apply()'s replay short-circuit
            #      (keyed on proposal_id) already returns the cached decision for a legitimately-replayed
            #      transition BEFORE _rejection runs, so this never blocks idempotent retries. A review
            #      receipt only ever sees a parked state when it is stale (a fresh review round always
            #      follows a worker re-open, observing evidence_pending first).
            #      "Carries a review receipt" uses the SAME predicate apply() uses to decide whether to
            #      append a review_receipt (review_scope != "none" or verdict is not None) — review_scope
            #      defaults to the string "none", NOT Python None, so a bare worker re-open is allowed
            #      through.
            existing_state = existing_target.get("state")
            if existing_state in ("closed", "repair_attempts_exhausted") and (
                proposal.review_scope != "none" or proposal.verdict is not None
            ):
                return self._reject(
                    proposal,
                    state,
                    "target_already_closed"
                    if existing_state == "closed"
                    else "target_already_repair_attempts_exhausted",
                )
            stored_kind = existing_target.get("kind")
            if isinstance(stored_kind, str) and stored_kind and stored_kind != proposal.target_kind:
                return self._reject(proposal, state, "target_kind_mismatch")
        # (b) evidence_pending is the SMALL-chapter codex_cc tier ONLY (major repairs submit
        #     escalated_review_pending, NOT evidence_pending). _accept() routes ANY kind's
        #     evidence_pending to codex_cc_review; a major chapter so routed bypasses escalated review and
        #     can never close. ((b) catches a fresh-target mislabel; (a) catches an existing major target.)
        if proposal.requested_state == "evidence_pending" and proposal.target_kind != "small_chapter":
            return self._reject(proposal, state, "evidence_pending_requires_small_chapter")
        # (c) the codex_cc review tier is SMALL-chapter only (small repairs re-enter codex_cc, major
        #     repairs re-enter escalated review). VERDICT-AGNOSTIC (C-FIX-3/F2): an advisory-pass
        #     codex_cc receipt for a major routes to the small-chapter state_writer_closure, AND a
        #     codex_cc BLOCKER for a major routes through _accept's blocker branch to
        #     review_blocked/repair_active, knocking the major out of its legitimate
        #     escalated_review_pending gate (its inferred live gate then vanishes). Reject ANY codex_cc
        #     receipt for a non-small target. (The codex_cc watchdog-timeout blocker defaults
        #     target_kind to small, so this never rejects a legit timeout.)
        if proposal.review_scope == "codex_cc" and proposal.target_kind != "small_chapter":
            return self._reject(proposal, state, "codex_cc_receipt_requires_small_chapter")
        # C-FIX-7 (4th recurrence of the "stale review receipt drives a target out of a resting/parked
        # obligation" class — centralized into one freshness invariant): a genuine CONTENT review receipt
        # is only "live" while the target sits in the ACTIVE review state for its scope (codex_cc ->
        # evidence_pending, escalated_review -> escalated_review_pending —
        # _ACTIVE_REVIEW_STATE_BY_SCOPE). Arriving in any other state it is stale: a fresh review round
        # ALWAYS re-opens the target to its active state first (a worker evidence_pending re-open for
        # codex_cc; a fresh gate authorization -> escalated_review_pending for escalated review).
        # codex_cc has no gate (unlike the escalated-review authorization rejection), so this state-based
        # check is its freshness guard — the codex_cc analog of C-FIX-6 Finding 2 (the
        # review_environment_unavailable hole). Without it a late codex_cc PASS from an earlier timed-out
        # round downgrades an owner-visible review_environment_unavailable (review_blocked) outage to
        # closure_candidate. Placed AFTER the (a)/(b)/(c) kind-scope guards on purpose, so a mis-KINDED
        # codex_cc receipt keeps its more-specific reason (codex_cc_receipt_requires_small_chapter /
        # target_kind_mismatch) and this guard only catches a correctly-kinded receipt that is merely
        # stale. EXEMPTIONS: watchdog-timeout blockers (they legitimately fire repeatedly at
        # review_blocked during env escalation), escalated-review actuator-failure blockers (the
        # env-retry lane), and a content BLOCKER contradicting a just-recorded pass at closure_candidate.
        # A bare worker re-open has review_scope "none" (not in the map) and is unaffected.
        # escalated-review stale receipts are already rejected EARLIER by the authorization rejection
        # (its specific stale_escalated_review_gate reason); every escalated-review receipt that reaches
        # here is therefore one of the exempt cases or already at escalated_review_pending — so this
        # guard changes no escalated-review reason and is the codex_cc freshness check in practice.
        if existing_target is not None:
            existing_state = existing_target.get("state")
            active_review_state = _ACTIVE_REVIEW_STATE_BY_SCOPE.get(proposal.review_scope)
            if (
                active_review_state is not None
                and existing_state != active_review_state
                and proposal.review_mode != "watchdog_timeout"
                and not self._is_escalated_review_actuator_failure(proposal)
                and not (proposal.verdict == "blocker" and existing_state == "closure_candidate")
            ):
                return self._reject(proposal, state, "stale_review_receipt_for_inactive_round")
        return None

    def _accept(
        self,
        proposal: StateTransitionProposal,
        state: dict[str, Any],
        target: dict[str, Any],
    ) -> StateTransitionDecision:
        requested = proposal.requested_state
        next_required_action: str | None = None
        blocker_code = proposal.blocker_code

        if proposal.verdict == "blocker":
            attempts = target.setdefault("repair_attempts", {})
            code = proposal.blocker_code or "unspecified"
            attempts[code] = int(attempts.get(code, 0)) + 1
            target["repair_attempts_total"] = int(target.get("repair_attempts_total", 0)) + 1
            # An ENVIRONMENT fault (actuator unreachable / "Service is busy", OR the review process
            # hard-timed-out / returned uncertain — the review never produced a content verdict) is not
            # a content verdict, so it is handled FIRST and in its own lane: it must NEVER trip content
            # exhaustion or convergence (excluded from _content_repair_total AND the per-code cap), and
            # non_retryable does NOT apply to it. Below the env bound it retries (repair_active — the
            # review may recover); past MAX_ENVIRONMENT_ATTEMPTS cumulative env faults it escalates to the
            # owner-visible review_environment_unavailable obligation so a persistently-broken review
            # environment stops looping forever. Env faults are still recorded in repair_attempts/_total
            # for audit (e.g. 2 env + 3 content faults stays repair_active because content-only 3 < the
            # content-total cap). new_state stays review_blocked (reused); the action token distinguishes
            # the obligation.
            if code in ENVIRONMENTAL_BLOCKER_CODES:
                # An env fault never CAUSES exhaustion and non_retryable does not apply to it, but it
                # must not UN-stick a target that is ALREADY genuinely content-exhausted (real content
                # blockers overflowed the per-code cap OR content_total, from which the env set is
                # excluded). Preserve that genuine exhaustion; otherwise route to the env lane: retry
                # below the bound, escalate to the owner-visible review_environment_unavailable above it.
                if self._content_exhausted(attempts):
                    requested = "repair_attempts_exhausted"
                    next_required_action = "repair_attempts_exhausted"
                elif self._environment_attempt_total(attempts) > MAX_ENVIRONMENT_ATTEMPTS:
                    requested = "review_blocked"
                    next_required_action = "review_environment_unavailable"
                else:
                    requested = "review_blocked"
                    next_required_action = "repair_active"
            elif (
                proposal.non_retryable
                or attempts[code] > MAX_REPAIR_ATTEMPTS
                or self._content_repair_total(attempts) > MAX_REPAIR_ATTEMPTS_TOTAL
            ):
                requested = "repair_attempts_exhausted"
                next_required_action = "repair_attempts_exhausted"
            else:
                requested = "review_blocked"
                next_required_action = "repair_active"
        elif proposal.review_scope == "codex_cc" and proposal.verdict in ACCEPTED_ADVISORY_VERDICTS:
            requested = "closure_candidate"
            next_required_action = "state_writer_closure"
        elif proposal.review_scope == "escalated_review" and proposal.verdict in ACCEPTED_ADVISORY_VERDICTS:
            requested = "closure_candidate"
            next_required_action = "major_closure_candidate"
        elif requested == "closed" and proposal.target_kind in ("small_chapter", "major_chapter"):
            next_required_action = "dispatch_next_slice_plan_mode"
        elif requested == "escalated_review_pending":
            next_required_action = "escalated_review"
        elif requested == "evidence_pending":
            next_required_action = "codex_cc_review"

        return StateTransitionDecision(
            decision="accepted",
            proposal_id=proposal.proposal_id,
            reason="accepted",
            state_revision=state["state_revision"],
            new_state=requested,
            next_required_action=next_required_action,
            blocker_code=blocker_code,
            non_retryable=proposal.non_retryable,
        )

    def _reject(
        self, proposal: StateTransitionProposal, state: dict[str, Any], reason: str
    ) -> StateTransitionDecision:
        return StateTransitionDecision(
            decision="rejected",
            proposal_id=proposal.proposal_id,
            reason=reason,
            state_revision=state["state_revision"],
        )

    @staticmethod
    def _content_repair_total(attempts: dict[str, Any]) -> int:
        """Per-target repair budget counting CONTENT blockers only.

        An ENVIRONMENTAL_BLOCKER_CODES fault (actuator unreachable / review hard-timeout / review
        uncertain) never produced a content verdict, so it is excluded from the exhaustion budget;
        otherwise transient env hiccups would prematurely "exhaust" a target whose content is still
        converging. Env-coded counts are kept in repair_attempts for audit but not counted here.
        """
        return sum(
            int(count)
            for blocker_code, count in attempts.items()
            if blocker_code not in ENVIRONMENTAL_BLOCKER_CODES
        )

    @staticmethod
    def _environment_attempt_total(attempts: dict[str, Any]) -> int:
        """Cumulative count of ENVIRONMENTAL_BLOCKER_CODES faults for a target (the env bound).

        Mirror of _content_repair_total but for the env set: counts exactly the codes EXCLUDED from
        content accounting. When this exceeds MAX_ENVIRONMENT_ATTEMPTS, _accept's environment lane
        surfaces the owner-visible review_environment_unavailable obligation instead of retrying review.
        """
        return sum(
            int(count)
            for blocker_code, count in attempts.items()
            if blocker_code in ENVIRONMENTAL_BLOCKER_CODES
        )

    @staticmethod
    def _content_exhausted(attempts: dict[str, Any]) -> bool:
        """True iff a target is GENUINELY content-exhausted: a real content blocker overflowed EITHER
        the per-code cap (MAX_REPAIR_ATTEMPTS) OR the per-target content total (MAX_REPAIR_ATTEMPTS_TOTAL).
        Mirrors the give-up predicate used by the env-lane/projection slices, evaluated across ALL
        content codes (the env set is excluded from both). Used so an ENVIRONMENT fault never UN-sticks
        a content-exhausted target — including the per-code case (e.g. one content code at count 3 > 2
        with content-total still <= 4), which a content-total-only check would miss.
        """
        if StateWriter._content_repair_total(attempts) > MAX_REPAIR_ATTEMPTS_TOTAL:
            return True
        return any(
            blocker_code not in ENVIRONMENTAL_BLOCKER_CODES and int(count) > MAX_REPAIR_ATTEMPTS
            for blocker_code, count in attempts.items()
        )

    @staticmethod
    def project_effective_action(
        stored: dict[str, Any], target: dict[str, Any] | None
    ) -> str | None:
        """Read-time effective-exhaustion projection (the un-stick for env-inflated false exhaustion).

        A stored ``repair_attempts_exhausted`` obligation that is exhausted ONLY because
        environment-only review faults (actuator failure or a review timeout/uncertain) inflated the
        per-target content budget is projected back to ``repair_active`` — the env fault never
        consumed content budget, so the target is still converging and must get its remaining real
        repair rounds (e.g. 2 env faults + 3 content repairs = repair_attempts_total 5 > 4, but
        content-only 3 <= 4).

        Only ``next_required_action`` is projected; ``new_state``/target ``state`` are left intact so
        the actionable-obligation check (which requires target.state == stored.new_state) still
        recognizes the obligation as live. The blocker_code is preserved, so the projected repair
        targets the real remaining content blocker.

        FAIL-CLOSED: a non-exhausted action, a recorded non_retryable exhaustion, a missing/malformed
        target, a malformed or negative count, or a CONTENT per-code / content-total cap genuinely
        exceeded all keep the RAW stored action. An ENVIRONMENTAL_BLOCKER_CODES per-code count is an
        environment fault, so it is excluded from the per-code clause and never keeps the raw
        exhaustion. We only RELAX exhaustion when we can positively prove the env-inflation; we never
        tighten or fabricate it. (Symmetric with the write-time give-up gate in _accept.)
        """
        raw = stored.get("next_required_action")
        if raw != "repair_attempts_exhausted":
            return raw
        if bool(stored.get("non_retryable", False)):
            return raw
        if not isinstance(target, dict):
            return raw
        attempts = target.get("repair_attempts")
        if not isinstance(attempts, dict):
            return raw
        try:
            counts = {str(code): int(count) for code, count in attempts.items()}
        except (TypeError, ValueError):
            return raw
        if any(count < 0 for count in counts.values()):
            return raw
        code = stored.get("blocker_code") or "unspecified"
        per_code = counts.get(code, 0)
        content_total = sum(
            count
            for blocker_code, count in counts.items()
            if blocker_code not in ENVIRONMENTAL_BLOCKER_CODES
        )
        per_code_exhausted = (
            code not in ENVIRONMENTAL_BLOCKER_CODES and per_code > MAX_REPAIR_ATTEMPTS
        )
        genuinely_exhausted = (
            per_code_exhausted or content_total > MAX_REPAIR_ATTEMPTS_TOTAL
        )
        return raw if genuinely_exhausted else "repair_active"

    def _review_caller_rejection(self, proposal: StateTransitionProposal) -> str | None:
        expected = self._expected_review_caller_type(proposal)
        if expected is None:
            return None
        if os.environ.get(CALLER_TYPE_ENV) != expected:
            return "unauthorized_review_receipt_actor"
        return None

    @staticmethod
    def _expected_review_caller_type(proposal: StateTransitionProposal) -> str | None:
        if proposal.verdict == "blocker" and proposal.review_mode == "watchdog_timeout":
            if (
                proposal.review_scope == "codex_cc"
                and proposal.blocker_code == CODEX_CC_TIMEOUT_BLOCKER_CODE
            ) or (
                proposal.review_scope == "escalated_review"
                and proposal.blocker_code in ESCALATED_REVIEW_WATCHDOG_TIMEOUT_BLOCKER_CODES
            ):
                return WATCHDOG_CALLER_TYPE
        if proposal.review_scope == "codex_cc" and proposal.verdict in ACCEPTED_ADVISORY_VERDICTS:
            return REVIEW_SCOPE_CALLER_TYPES["codex_cc"]
        if proposal.review_scope == "escalated_review" and proposal.verdict in ACCEPTED_ADVISORY_VERDICTS.union({"blocker"}):
            return REVIEW_SCOPE_CALLER_TYPES["escalated_review"]
        return None

    def _has_codex_cc_receipt(self, state: dict[str, Any], target_id: str) -> bool:
        target = state["targets"].get(target_id, {})
        return self._latest_review_receipt_is_pass_family(target, "codex_cc")

    def _has_escalated_review_receipt(self, state: dict[str, Any], target_id: str) -> bool:
        target = state["targets"].get(target_id, {})
        return self._latest_review_receipt_is_pass_family(target, "escalated_review")

    @staticmethod
    def _latest_review_receipt_is_pass_family(target: dict[str, Any], scope: str) -> bool:
        # review_receipts is append-only through apply(); the current close gate
        # deliberately accepts only the newest scoped receipt for a target that is
        # still in the closure_candidate state produced by that receipt. New
        # receipts carry the proposal base_state_revision, which is monotonic per
        # accepted apply; all-legacy receipts fall back to append order, while
        # malformed or post-revision legacy entries fail closed.
        if target.get("state") != "closure_candidate":
            return False
        receipts = target.get("review_receipts", [])
        if not isinstance(receipts, list) or any(not isinstance(receipt, dict) for receipt in receipts):
            return False
        scoped_receipts = [receipt for receipt in receipts if receipt.get("scope") == scope]
        if not scoped_receipts:
            return False
        saw_revisioned_receipt = False
        for receipt in scoped_receipts:
            if isinstance(receipt.get("review_state_revision"), int):
                saw_revisioned_receipt = True
            elif saw_revisioned_receipt:
                return False
        latest = max(
            enumerate(scoped_receipts),
            key=lambda item: (
                item[1].get("review_state_revision")
                if isinstance(item[1].get("review_state_revision"), int)
                else -1,
                item[0],
            ),
        )[1]
        if latest.get("verdict") not in ACCEPTED_ADVISORY_VERDICTS:
            return False
        if scope == "escalated_review":
            package = target.get("escalated_review_package")
            if not isinstance(package, dict) or not package.get("package_sha256"):
                return False
            if latest.get("package_sha256") != package.get("package_sha256"):
                return False
        return True

    @staticmethod
    def _is_escalated_review_watchdog_timeout(proposal: StateTransitionProposal) -> bool:
        return (
            proposal.review_scope == "escalated_review"
            and proposal.review_mode == "watchdog_timeout"
            and proposal.blocker_code in ESCALATED_REVIEW_WATCHDOG_TIMEOUT_BLOCKER_CODES
        )

    @staticmethod
    def _is_escalated_review_actuator_failure(proposal: StateTransitionProposal) -> bool:
        return (
            proposal.review_scope == "escalated_review"
            and proposal.verdict == "blocker"
            and proposal.review_mode == "actuator_failure"
            and proposal.blocker_code == ESCALATED_REVIEW_ACTUATOR_FAILURE_BLOCKER_CODE
        )

    @staticmethod
    def _is_legitimate_environment_blocker(proposal: StateTransitionProposal) -> bool:
        """True iff an ENVIRONMENTAL_BLOCKER_CODES blocker_code is paired with its LEGITIMATE producer.

        The environment lane routes by blocker_code alone — no content counting, bounded
        env-escalation. Each env code is RESERVED to one producer:
        escalated_review_actuator_failure to the escalated review actuator-failure path, the escalated review watchdog-timeout
        codes to a escalated review watchdog timeout, and codex_cc_review_timeout to a codex_cc watchdog
        timeout. Any other (scope, mode) carrying a reserved code is a malformed/forged tuple that
        must NOT enter the env lane.
        """
        code = proposal.blocker_code
        if code == ESCALATED_REVIEW_ACTUATOR_FAILURE_BLOCKER_CODE:
            return StateWriter._is_escalated_review_actuator_failure(proposal)
        if code in ESCALATED_REVIEW_WATCHDOG_TIMEOUT_BLOCKER_CODES:
            return StateWriter._is_escalated_review_watchdog_timeout(proposal)
        if code == CODEX_CC_TIMEOUT_BLOCKER_CODE:
            return proposal.review_scope == "codex_cc" and proposal.review_mode == "watchdog_timeout"
        return False

    def _escalated_review_authorization_rejection(
        self,
        proposal: StateTransitionProposal,
        state: dict[str, Any],
    ) -> str | None:
        gate_id = proposal.external_review_gate_proposal_id
        if not gate_id:
            return "missing_escalated_review_authorization"
        grant = self._fresh_authorization_record(state, gate_id)
        if grant is None:
            return "missing_escalated_review_authorization"
        gate = grant.get("escalated_review_gate")
        if not isinstance(gate, dict) or not gate.get("package_sha256"):
            return "missing_escalated_review_package"
        if gate.get("target_id") != proposal.target_id:
            return "escalated_review_authorization_target_mismatch"
        current_gate = self._current_escalated_review_gate_id_for_target(state, proposal.target_id)
        if current_gate is not None and current_gate != gate_id:
            return "stale_escalated_review_gate"
        # C-FIX-3/F1: _current_escalated_review_gate_id_for_target returns a gate ONLY while the target
        # is in escalated_review_pending. If it is None the gate was already consumed (a prior pass moved
        # the target to closure_candidate) or superseded (a blocker moved it to review_blocked). A later
        # PASS-FAMILY receipt reusing that gate id is stale — accepting it would un-block the target back
        # to closure_candidate and suppress the live repair_active obligation. Gate on pass-family ONLY
        # so a legitimate later BLOCKER on the same gate (which arrives with current_gate already None)
        # is NOT rejected; a fresh post-repair pass uses a NEW gate and so finds current_gate == its own
        # gate id.
        if current_gate is None and proposal.verdict in ACCEPTED_ADVISORY_VERDICTS:
            return "stale_escalated_review_gate"
        # C-FIX-6/Finding-2: a consumed gate (current_gate is None) is reusable for a CONTENT blocker
        # ONLY while the target rests at closure_candidate — the path where a blocker legitimately
        # contradicts a just-recorded pass within the same review round. Once the target has moved on
        # (e.g. review_blocked carrying the owner-visible review_environment_unavailable obligation), a
        # late content blocker reusing the consumed gate is stale: accepting it routes to repair_active
        # and downgrades the parked env-outage obligation, so the orchestrator repairs content while the
        # review environment is actually down. Environment-fault blockers are EXEMPT and stay in their
        # own non-content lane: watchdog-timeout blockers never reach this function (they bypass
        # authorization at the review-scope dispatch), and actuator-failure blockers are intentionally
        # accepted repeatedly after the target is already review_blocked (the env-retry lane). Both are
        # excluded via the same helpers the nonce gate below uses. (Pass-family stays rejected for ANY
        # None-gate state above — only the blocker side gets the closure_candidate carve-out, so a
        # stale/duplicate pass at closure_candidate is still rejected.)
        if (
            current_gate is None
            and proposal.verdict == "blocker"
            and not self._is_escalated_review_watchdog_timeout(proposal)
            and not self._is_escalated_review_actuator_failure(proposal)
        ):
            existing_target = state.get("targets", {}).get(proposal.target_id)
            existing_state = existing_target.get("state") if isinstance(existing_target, dict) else None
            if existing_state != "closure_candidate":
                return "stale_escalated_review_gate"
        if gate.get("package_sha256") != proposal.package_sha256:
            return "escalated_review_package_sha_mismatch"
        expected_nonce = gate.get("external_review_submission_nonce")
        if (
            isinstance(expected_nonce, str)
            and expected_nonce
            and not self._is_escalated_review_watchdog_timeout(proposal)
            and not self._is_escalated_review_actuator_failure(proposal)
        ):
            if proposal.external_review_submission_nonce != expected_nonce:
                return "external_review_submission_nonce_mismatch"
        return None

    def _external_review_artifact_rejection(self, proposal: StateTransitionProposal) -> str | None:
        path = self._external_review_artifact_path(proposal.external_review_artifact_ref)
        if path is None:
            return "invalid_external_review_artifact_ref"
        if not path.exists() or not path.is_file():
            return "missing_external_review_artifact"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != proposal.external_review_receipt_sha256:
            return "external_review_artifact_sha_mismatch"
        return None

    def _codex_cc_transcript_artifact_rejection(self, proposal: StateTransitionProposal) -> str | None:
        path = self._codex_cc_transcript_artifact_path(proposal.codex_cc_transcript_artifact_ref)
        if path is None:
            return "invalid_codex_cc_transcript_artifact_ref"
        if not path.exists() or not path.is_file():
            return "missing_codex_cc_transcript_artifact"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != proposal.codex_cc_transcript_sha256:
            return "codex_cc_transcript_sha_mismatch"
        return None

    def _codex_cc_transcript_artifact_path(self, artifact_ref: str | None) -> Path | None:
        if not isinstance(artifact_ref, str) or not artifact_ref.startswith(CODEX_CC_TRANSCRIPT_ARTIFACT_REF_PREFIX):
            return None
        relative = artifact_ref[len("artifact:") :]
        rel_path = Path(relative)
        if (
            rel_path.is_absolute()
            or ".." in rel_path.parts
            or rel_path.parts[:2] != ("reports", "codex-cc-receipts")
        ):
            return None
        root = self._repo_root()
        full_path = (root / rel_path).resolve()
        try:
            full_path.relative_to(root.resolve())
        except ValueError:
            return None
        return full_path

    def _external_review_artifact_path(self, artifact_ref: str | None) -> Path | None:
        if not isinstance(artifact_ref, str) or not artifact_ref.startswith("artifact:reports/"):
            return None
        relative = artifact_ref[len("artifact:") :]
        rel_path = Path(relative)
        if rel_path.is_absolute() or ".." in rel_path.parts or rel_path.parts[:1] != ("reports",):
            return None
        root = self._repo_root()
        full_path = (root / rel_path).resolve()
        try:
            full_path.relative_to(root.resolve())
        except ValueError:
            return None
        return full_path

    def _repo_root(self) -> Path:
        parts = self.state_path.parts
        for index in range(0, len(parts) - len(self._STATE_ROOT_PARTS) + 1):
            if parts[index : index + len(self._STATE_ROOT_PARTS)] == self._STATE_ROOT_PARTS:
                return Path(*parts[:index])
        return self.state_path.parent.parent.parent.parent

    def _review_receipt(self, proposal: StateTransitionProposal) -> dict[str, Any]:
        return {
            "scope": proposal.review_scope,
            "actor_role": proposal.actor_role,
            "caller_type": os.environ.get(CALLER_TYPE_ENV),
            "verdict": proposal.verdict,
            "model": proposal.model,
            "reasoning_effort": proposal.reasoning_effort,
            "review_mode": proposal.review_mode,
            "review_state_revision": proposal.base_state_revision,
            "blocker_code": proposal.blocker_code,
            "blocker_detail": proposal.blocker_detail,
            "codex_cc_transcript_sha256": proposal.codex_cc_transcript_sha256,
            "codex_cc_transcript_artifact_ref": proposal.codex_cc_transcript_artifact_ref,
            "package_sha256": proposal.package_sha256,
            "external_review_receipt_sha256": proposal.external_review_receipt_sha256,
            "external_review_submission_nonce": proposal.external_review_submission_nonce,
            "external_review_artifact_ref": proposal.external_review_artifact_ref,
            "external_review_gate_proposal_id": proposal.external_review_gate_proposal_id,
            "evidence_refs": proposal.evidence_refs or [],
        }

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return empty_state()
        last_error: json.JSONDecodeError | None = None
        for attempt in range(3):
            try:
                return normalize_state(json.loads(self.state_path.read_text(encoding="utf-8")))
            except json.JSONDecodeError as exc:
                last_error = exc
                time.sleep(0.02 * (attempt + 1))
        assert last_error is not None
        raise last_error

    def _write_state(self, state: dict[str, Any]) -> None:
        state = normalize_state(state)
        state["schema_version"] = 1
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
        tmp_path = self.state_path.with_name(f".{self.state_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.state_path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def _append_ledger(
        self, proposal: StateTransitionProposal, decision: StateTransitionDecision
    ) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "id": str(uuid.uuid4()),
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "proposal": asdict(proposal),
            "decision": asdict(decision),
        }
        with self.ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    def _single_flight(self):
        return _FileLock(self.state_path.with_suffix(self.state_path.suffix + ".lock"))

    # ---------------------------------------------------------------------------
    # Consume-once dispatch ledger API
    # state["dispatched_proposals"] maps proposal_id -> {"status": "pending"|"spawned", "time": <iso8601>}
    # Spawned records may also carry spawn_session_id as audit proof parsed from AO's SESSION=<id>
    # stdout attestation; root resolution still comes from live AO session metadata, not this ledger.
    # ---------------------------------------------------------------------------

    def is_dispatched(self, proposal_id: str) -> bool:
        """Return True iff proposal_id has already been consumed.

        Consumed means any of CONSUMED_DISPATCH_STATUSES: an ordinary "spawned" dispatch, a
        "spawned_unattested" dispatch awaiting reconcile-spawn-attestation, a
        "final_convergence_recorded" owner-proxy convergence decision, or a "spawned_base_mismatch" /
        "spawned_base_unverified" spawn-baseline lease. This membership is the seam that makes the
        global preflight (_current_obligation_issue) SKIP a converged repair_attempts_exhausted
        obligation instead of re-emitting owner_proxy_convergence_required; for a baseline lease the
        dedicated spawned_base branch ABOVE the skip surfaces it as an obligation first.
        """
        state = self._read_state()
        state.setdefault("dispatched_proposals", {})
        record = state["dispatched_proposals"].get(proposal_id)
        return record is not None and record.get("status") in CONSUMED_DISPATCH_STATUSES

    def dispatch_record(self, proposal_id: str) -> dict[str, Any] | None:
        """Return a copy of the consume-once dispatch record for proposal_id, or None."""
        state = self._read_state()
        record = state.get("dispatched_proposals", {}).get(proposal_id)
        return dict(record) if isinstance(record, dict) else None

    def claim_dispatch(self, proposal_id: str) -> bool:
        """Atomically claim dispatch for proposal_id.

        Returns True (writing a fresh "pending" lease) when no live claim exists.
        Returns False when the proposal is already "spawned" (consumed) or a "pending"
        lease is still live (a peer is mid-dispatch). A "pending" lease older than
        STALE_PENDING_DISPATCH_SECONDS — or one with an unparseable timestamp — is an
        orphaned claim (the claimer was SIGKILLed between claim and confirm/release) and
        is reclaimed here, so that window can never deadlock a slice forever.
        """
        with self._single_flight():
            state = self._read_state()
            state.setdefault("dispatched_proposals", {})
            record = state["dispatched_proposals"].get(proposal_id)
            if record is not None:
                if record.get("status") in CONSUMED_DISPATCH_STATUSES:
                    return False
                if record.get("status") == "pending" and not self._pending_lease_expired(
                    record, ttl_seconds=self._dispatch_lease_ttl_seconds(state, proposal_id)
                ):
                    return False
                # A stale/malformed pending lease falls through and is reclaimed below.
            state["dispatched_proposals"][proposal_id] = {
                "status": "pending",
                "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            self._write_state(state)
            return True

    @staticmethod
    def _dispatch_lease_ttl_seconds(state: dict[str, Any], proposal_id: str) -> float:
        """The orphan floor for this proposal's 'pending' lease, by its action. A long-running
        actuator/review lease (escalated_review / codex_cc_review) uses the longer
        LONG_RUNNING_PENDING_DISPATCH_SECONDS floor so a reconcile re-dispatch cannot reclaim a LIVE
        bridge and double-submit; every other (fast AO-spawn) lease keeps STALE_PENDING_DISPATCH_SECONDS.
        Read from the stored RAW next_required_action: for these two actions it equals the read-time
        effective action (the only projection rewrites repair_attempts_exhausted -> repair_active,
        neither of which is long-running), so no cli._effective_action import (and its cycle) is needed."""
        proposal_results = state.get("proposal_results", {})
        stored = proposal_results.get(proposal_id) if isinstance(proposal_results, dict) else None
        action = stored.get("next_required_action") if isinstance(stored, dict) else None
        if action in LONG_RUNNING_DISPATCH_LEASE_ACTIONS:
            return LONG_RUNNING_PENDING_DISPATCH_SECONDS
        return STALE_PENDING_DISPATCH_SECONDS

    @staticmethod
    def _pending_lease_expired(
        record: dict[str, Any], ttl_seconds: float = STALE_PENDING_DISPATCH_SECONDS
    ) -> bool:
        """True if a 'pending' dispatch lease is older than ttl_seconds (or carries an unparseable
        timestamp) — i.e. an orphaned claim safe to reclaim. ttl_seconds defaults to the 600s
        fast-spawn floor, correct for the action-pre-filtered reclaim_orphaned_pending_leases and
        cmd_reconcile_leases callers; claim_dispatch passes a per-action floor (C-FIX-10) so a live
        long-running actuator/review lease is not reclaimed mid-bridge."""
        raw = record.get("time")
        if not isinstance(raw, str):
            return True
        try:
            claimed = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            return True
        return (datetime.now(timezone.utc) - claimed).total_seconds() > ttl_seconds

    def confirm_dispatch(
        self,
        proposal_id: str,
        authorized_by: str = "orchestrator_policy",
        spawn_session_id: str | None = None,
        spawn_attestation: str | None = None,
    ) -> None:
        """Mark a previously claimed proposal_id as successfully spawned.

        Creates the record if missing (e.g. state file was replaced), and updates
        its time to now.  ``authorized_by`` is recorded in the spawned record so
        callers can distinguish policy-pre-authorized dispatches from those that
        required a live orchestrator authorization.
        """
        with self._single_flight():
            state = self._read_state()
            state.setdefault("dispatched_proposals", {})
            record = {
                # A confirm with spawn_attestation="missing" (the spawn succeeded but no SESSION=
                # line was parsed) records the consume-once token as "spawned_unattested" so a later
                # reconcile-spawn-attestation can promote it to "spawned" or release it. Any other
                # confirm is an ordinary fully-attested "spawned".
                "status": "spawned_unattested" if spawn_attestation == "missing" else "spawned",
                "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "authorized_by": authorized_by,
            }
            if spawn_session_id:
                record["spawn_session_id"] = spawn_session_id
                record["spawn_attestation"] = "session"
            elif spawn_attestation:
                record["spawn_attestation"] = spawn_attestation
            state["dispatched_proposals"][proposal_id] = record
            self._write_state(state)

    # ---------------------------------------------------------------------------
    # Orchestrator-authorization API
    # state["orchestrator_authorizations"] maps proposal_id ->
    #   {"actor_role": str, "evidence_refs": list[str], "scope": str|None, "time": <iso8601>}
    # ---------------------------------------------------------------------------

    def record_authorization(
        self,
        *,
        proposal_id: str,
        evidence_refs: list[str],
        scope: str | None = None,
    ) -> dict:
        """Record a live orchestrator authorization for an already-accepted proposal.

        The caller identity is taken from the AO-injected ``AO_CALLER_TYPE`` env var
        (set per session by the AO runtime), NOT from a self-asserted argument: only a
        real orchestrator session (``AO_CALLER_TYPE == "orchestrator"``) may authorize,
        and a missing/other value fails closed (``non_orchestrator_caller``). This binds
        the authorization to the orchestrator so a worker cannot mint one. Evidence and
        proposal acceptance are also validated. Returns a rejection dict (without
        writing) on any failure, or a ``{"decision": "recorded", ...}`` dict on success.

        Note: whether the action is "gated" is intentionally NOT checked here to
        avoid a circular import with continuation.py — the CLI handles that guard.
        """
        caller_type = os.environ.get(CALLER_TYPE_ENV)
        with self._single_flight():
            state = self._read_state()
            state.setdefault("orchestrator_authorizations", {})
            state.setdefault("proposal_results", {})

            if caller_type != "orchestrator":
                return {
                    "decision": "rejected",
                    "reason": "non_orchestrator_caller",
                    "proposal_id": proposal_id,
                    "caller_type": caller_type,
                }
            orchestrator_proof = self._orchestrator_proof_metadata()
            if orchestrator_proof.get("reason"):
                return {
                    "decision": "rejected",
                    "reason": orchestrator_proof["reason"],
                    "proposal_id": proposal_id,
                    "caller_type": caller_type,
                }
            if not evidence_refs:
                return {
                    "decision": "rejected",
                    "reason": "missing_authorization_evidence",
                    "proposal_id": proposal_id,
                }

            stored = state["proposal_results"].get(proposal_id)
            if stored is None or stored.get("decision") != "accepted":
                return {
                    "decision": "rejected",
                    "reason": "unknown_or_unaccepted_proposal",
                    "proposal_id": proposal_id,
                }

            record: dict = {
                "actor_role": "orchestrator",
                "caller_type": caller_type,
                "session_id": orchestrator_proof.get("session_id"),
                "project_id": orchestrator_proof.get("project_id"),
                "active_root": orchestrator_proof.get("active_root"),
                "evidence_refs": list(evidence_refs),
                "scope": scope,
                "state_revision": stored.get("state_revision"),
                "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            if stored.get("next_required_action") == "escalated_review":
                gate = self._escalated_review_gate_metadata(state, proposal_id)
                if not isinstance(gate, dict) or not gate.get("package_sha256"):
                    return {
                        "decision": "rejected",
                        "reason": "missing_escalated_review_package",
                        "proposal_id": proposal_id,
                    }
                current_gate = self._current_escalated_review_gate_id_for_target(state, gate.get("target_id"))
                if current_gate is not None and current_gate != proposal_id:
                    return {
                        "decision": "rejected",
                        "reason": "stale_escalated_review_gate",
                        "proposal_id": proposal_id,
                    }
                gate.setdefault("external_review_submission_nonce", uuid.uuid4().hex)
                record["escalated_review_gate"] = dict(gate)
                state.setdefault("escalated_review_gates", {})[proposal_id] = dict(gate)
                target_id = gate.get("target_id")
                target = state.get("targets", {}).get(target_id) if isinstance(target_id, str) else None
                if isinstance(target, dict):
                    target["active_escalated_review_gate_proposal_id"] = proposal_id
                    target["escalated_review_package"] = {
                        "package_path": gate.get("package_path"),
                        "prompt_path": gate.get("prompt_path"),
                        "package_sha256": gate.get("package_sha256"),
                    }
            state["orchestrator_authorizations"][proposal_id] = record
            self._write_state(state)
            return {"decision": "recorded", "proposal_id": proposal_id, "authorization": record}

    def _escalated_review_gate_metadata(
        self,
        state: dict[str, Any],
        proposal_id: str,
    ) -> dict[str, Any] | None:
        gate = state.get("escalated_review_gates", {}).get(proposal_id)
        if isinstance(gate, dict) and gate.get("package_sha256"):
            return dict(gate)

        proposal = self._ledger_proposal(proposal_id)
        if not isinstance(proposal, dict):
            return None
        target_id = proposal.get("target_id")
        target_kind = proposal.get("target_kind")
        if not isinstance(target_id, str) or not isinstance(target_kind, str):
            return None
        target = state.get("targets", {}).get(target_id)
        if not isinstance(target, dict) or target.get("state") != "escalated_review_pending":
            return None

        package = target.get("escalated_review_package") if isinstance(target.get("escalated_review_package"), dict) else {}
        package_path = proposal.get("package_path") or package.get("package_path")
        prompt_path = proposal.get("prompt_path") or package.get("prompt_path")
        package_sha256 = proposal.get("package_sha256") or package.get("package_sha256")
        if not (
            isinstance(package_path, str)
            and isinstance(prompt_path, str)
            and isinstance(package_sha256, str)
            and package_sha256
        ):
            return None
        return {
            "proposal_id": proposal_id,
            "target_id": target_id,
            "target_kind": target_kind,
            "package_path": package_path,
            "prompt_path": prompt_path,
            "package_sha256": package_sha256,
            "external_review_submission_nonce": proposal.get("external_review_submission_nonce")
            or package.get("external_review_submission_nonce"),
        }

    def _current_escalated_review_gate_id_for_target(
        self,
        state: dict[str, Any],
        target_id: object,
    ) -> str | None:
        if not isinstance(target_id, str):
            return None
        target = state.get("targets", {}).get(target_id)
        if not isinstance(target, dict) or target.get("state") != "escalated_review_pending":
            return None
        proposal_results = state.get("proposal_results", {})
        if not isinstance(proposal_results, dict):
            return None
        latest: tuple[int, str] | None = None
        for pid, stored in proposal_results.items():
            if not isinstance(pid, str) or not isinstance(stored, dict):
                continue
            if stored.get("decision") != "accepted":
                continue
            if stored.get("next_required_action") != "escalated_review":
                continue
            revision = stored.get("state_revision")
            if not isinstance(revision, int):
                continue
            proposal = self._ledger_proposal(pid)
            if not isinstance(proposal, dict) or proposal.get("target_id") != target_id:
                continue
            if latest is None or revision > latest[0]:
                latest = (revision, pid)
        if latest is not None:
            return latest[1]
        pointer = target.get("active_escalated_review_gate_proposal_id")
        return pointer if isinstance(pointer, str) else None

    def _ledger_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        if not self.ledger_path.exists():
            return None
        for line in self.ledger_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            proposal = entry.get("proposal")
            if isinstance(proposal, dict) and proposal.get("proposal_id") == proposal_id:
                return dict(proposal)
        return None

    def _orchestrator_proof_metadata(self) -> dict[str, Any]:
        session_id = os.environ.get(SESSION_ID_ENV)
        if not isinstance(session_id, str) or not session_id.strip():
            return {"reason": "missing_or_stale_orchestrator_proof"}

        root = self._repo_root().resolve()
        session_id = session_id.strip()
        expected_session = read_contract_string(root, "continuation_policy", "orchestrator_session")
        if not expected_session or session_id != expected_session:
            return {"reason": "missing_or_stale_orchestrator_proof"}

        metadata: dict[str, Any] = {"session_id": session_id}
        active_root = read_contract_active_root(root)
        if active_root is None or active_root != root:
            return {"reason": "missing_or_stale_orchestrator_proof"}
        metadata["active_root"] = str(active_root)

        project_id = read_contract_project_id(root)
        if not project_id:
            return {"reason": "missing_or_stale_orchestrator_proof"}
        metadata["project_id"] = project_id
        return metadata

    def is_authorized(self, proposal_id: str) -> bool:
        """Return True iff a FRESH orchestrator authorization exists for proposal_id.

        Fresh requires ALL of:
          - a grant whose actor_role is "orchestrator" (a worker cannot mint one — see
            record_authorization's AO_CALLER_TYPE binding),
          - the proposal is STILL accepted in canonical state, and
          - the grant's state_revision equals the proposal's current accepted
            state_revision — i.e. the grant is bound to the exact accepted decision it
            was issued against.

        The revision binding scopes the grant to a single accepted decision: it cannot
        validate against, or outlive, a different/superseded decision, and a grant left
        behind by a tampered/corrupted state (proposal removed, or its revision changed)
        fails closed instead of silently clearing the gate. We deliberately do NOT delete
        the grant after a read ("consume by deletion"): `continue` for a gated action is
        idempotent (a daemon re-poke must keep returning a stable "authorized", not flap
        back to requires_orchestrator_authorization); the revision binding, not deletion,
        is what makes this a one-decision grant.

        Honest note on reachability: a proposal_id is accepted exactly once (apply()
        replays a duplicate proposal_id without bumping state — writer.py:84-86), so
        proposal_results[pid].state_revision is immutable and the revision check never
        diverges under normal operation. Its value is defense-in-depth: tamper/corruption
        resistance and an explicit, self-validating invariant if accept-once ever changes.
        """
        state = self._read_state()
        return self._fresh_authorization_record(state, proposal_id) is not None

    def authorization_record(self, proposal_id: str) -> dict[str, Any] | None:
        state = self._read_state()
        record = self._fresh_authorization_record(state, proposal_id)
        return dict(record) if record is not None else None

    @staticmethod
    def _fresh_authorization_record(state: dict[str, Any], proposal_id: str) -> dict[str, Any] | None:
        grant = state.get("orchestrator_authorizations", {}).get(proposal_id)
        if not isinstance(grant, dict) or grant.get("actor_role") != "orchestrator":
            return None
        accepted = state.get("proposal_results", {}).get(proposal_id)
        if not isinstance(accepted, dict) or accepted.get("decision") != "accepted":
            return None
        if grant.get("state_revision") != accepted.get("state_revision"):
            return None
        return grant

    def release_dispatch(self, proposal_id: str) -> None:
        """Release a *pending* dispatch claim so a failed/aborted dispatch can be retried.

        Only a "pending" record is removed; a "spawned" record (a confirmed successful
        dispatch) is never deleted, preserving the consume-once invariant even if
        release is called out of order.
        """
        with self._single_flight():
            state = self._read_state()
            state.setdefault("dispatched_proposals", {})
            record = state["dispatched_proposals"].get(proposal_id)
            if record is not None and record.get("status") == "pending":
                del state["dispatched_proposals"][proposal_id]
                self._write_state(state)

    def set_operator_pause(
        self, *, reason: str | None = None, set_by: str | None = None
    ) -> dict[str, Any]:
        """First-class operator pause (P5): set a state-visible ``operator_pause`` record.

        Orthogonal metadata, NOT a transition: this does NOT bump state_revision and does NOT touch
        the obligation chain. The CLI projection guard (``_operator_pause_issue``) short-circuits
        list-ready/list-gated/continue/reconcile-once and the side-effectful canonical-write commands
        to an ``operator_paused`` envelope while this is set, so the sidecar's pokes become explicit
        no-ops the state machine can reason about — vs. an out-of-band "ignore pokes" LLM instruction
        that is invisible to state.json. Caller-type proof is enforced at the CLI layer. Distinct
        from the final-convergence parked terminal (``final_convergence_recorded``):
        operator-invokable, not content-exhaustion-gated.
        """
        with self._single_flight():
            state = self._read_state()
            record = {
                "paused": True,
                "reason": reason or "",
                "set_by": set_by or "",
                "set_at_revision": state.get("state_revision"),
                "set_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            state["operator_pause"] = record
            self._write_state(state)
            return dict(record)

    def clear_operator_pause(self) -> dict[str, Any]:
        """Resume (P5): clear the ``operator_pause`` record. No state_revision bump.

        Resume restores normal projection from the CURRENT state (not ``set_at_revision``); any state
        advance that happened while paused is reflected immediately on the next list-ready.
        """
        with self._single_flight():
            state = self._read_state()
            prior = state.get("operator_pause")
            was_paused = isinstance(prior, dict) and prior.get("paused") is True
            state.pop("operator_pause", None)
            self._write_state(state)
            return {
                "was_paused": bool(was_paused),
                "prior": dict(prior) if isinstance(prior, dict) else None,
            }

    def reclaim_orphaned_pending_leases(
        self, proposal_ids: list[str], *, apply: bool
    ) -> dict[str, list[str]]:
        """Delete provably-orphaned 'pending' dispatch leases for the given proposal_ids.

        A lease is reclaimed ONLY IF, re-read under the single-flight lock, it is still
        ``status == "pending"`` AND ``_pending_lease_expired`` (older than
        STALE_PENDING_DISPATCH_SECONDS, or an unparseable timestamp). The under-lock expiry
        recheck is the race guard: a lease a concurrent claim refreshed (e.g. a just-started
        external review actuator bridge) is younger than the floor and is left untouched, so a
        reclaim can never delete a live lease out from under an in-flight dispatch.

        The CALLER owns the action-class invariant: ``proposal_ids`` must already be filtered to
        ordinary fast auto-spawn continuation leases — never external review actuator/review leases
        (e.g. an escalated review), which legitimately hold "pending" far longer than the orphan
        floor. This method enforces only the orphan-age invariant, all that can be verified from the
        lease record alone. It mirrors ``release_dispatch`` (pending-only delete) with the orphan-age
        gate added, and never deletes a ``spawned`` record.

        With ``apply=False`` this is a pure dry-run: it reports what WOULD be reclaimed and writes
        nothing.
        """
        reclaimed: list[str] = []
        skipped: list[str] = []
        with self._single_flight():
            state = self._read_state()
            dispatched = state.setdefault("dispatched_proposals", {})
            changed = False
            for proposal_id in proposal_ids:
                record = dispatched.get(proposal_id)
                if (
                    isinstance(record, dict)
                    and record.get("status") == "pending"
                    and self._pending_lease_expired(record)
                ):
                    reclaimed.append(proposal_id)
                    if apply:
                        del dispatched[proposal_id]
                        changed = True
                else:
                    skipped.append(proposal_id)
            if changed:
                self._write_state(state)
        return {"reclaimed": reclaimed, "skipped": skipped}

    # ---------------------------------------------------------------------------
    # Owner-proxy convergence / dispatch-record reconcile API
    #
    # These consumers resolve obligations the ordinary auto-spawn loop deliberately CANNOT execute:
    #   * record_final_convergence    — consume an exhausted repair token as an audited orchestrator
    #                                    decision (the converged/closed terminal), without spawning,
    #                                    resetting counters, or mutating the target state.
    #   * reconcile_spawn_attestation — promote a spawned_unattested lease to spawned (session found)
    #                                    or release it (session proven absent).
    #   * reconcile_spawned_dispatch  — refresh, or release-if-overdue, a normal spawned lease.
    #
    # All three are orchestrator-only (AO_CALLER_TYPE=orchestrator + a fresh orchestrator proof that
    # binds AO_SESSION_ID to the contract's continuation_policy.orchestrator_session and the active
    # root). They never edit action vocabulary, so the contract action arrays stay in lock-step.
    # ---------------------------------------------------------------------------

    def record_spawn_baseline_issue(
        self,
        proposal_id: str,
        *,
        spawn_session_id: str,
        reason: str,
        required_source_commit: str | None = None,
        worker_worktree: str | None = None,
        worker_head: str | None = None,
        spawn_session_termination: dict[str, Any] | None = None,
        ao_project_id: str | None = None,
        authorized_by: str = "orchestrator_policy",
    ) -> None:
        """Consume a spawned dispatch whose worktree cannot prove the required source baseline.

        Record-only: it writes a "spawned_base_mismatch" / "spawned_base_unverified" lease into
        dispatched_proposals (so the target is never re-spawned on a known-bad baseline) and leaves
        it for _current_obligation_issue to surface as an owner-visible obligation.
        reconcile_spawned_dispatch RELEASES the lease under --release-if-terminal + terminal proof.
        The DETECTION call site (the spawn flow that invokes THIS method) is wired in a later slice.
        """
        status = "spawned_base_mismatch" if reason == "spawn_base_commit_mismatch" else "spawned_base_unverified"
        with self._single_flight():
            state = self._read_state()
            state.setdefault("dispatched_proposals", {})
            record: dict[str, Any] = {
                "status": status,
                "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "authorized_by": authorized_by,
                "spawn_session_id": spawn_session_id,
                "spawn_attestation": "session",
                "baseline_attestation_result": reason,
                "reason": reason,
            }
            if required_source_commit:
                record["required_source_commit"] = required_source_commit
            if worker_worktree:
                record["worker_worktree"] = worker_worktree
            if worker_head:
                record["worker_head"] = worker_head
            if ao_project_id:
                record["ao_project_id"] = ao_project_id
            if spawn_session_termination is not None:
                record["spawn_session_termination"] = spawn_session_termination
            state["dispatched_proposals"][proposal_id] = record
            self._write_state(state)

    def record_final_convergence(
        self,
        *,
        proposal_id: str,
        evidence_refs: list[str],
        outcome: str,
    ) -> dict:
        """Record an owner-proxy final convergence decision for an exhausted repair token.

        This consumes the ``repair_attempts_exhausted`` obligation WITHOUT spawning another worker,
        resetting repair counters, mutating the target's state, or changing action vocabulary. It
        exists so "repair budget exhausted" becomes an auditable orchestrator decision instead of an
        unstructured prompt to the human Owner. Only the live AO orchestrator caller may record it.

        Atomicity: both state keys it writes (``owner_proxy_final_convergence_records`` and
        ``dispatched_proposals``) are written under a single ``_single_flight`` lock and one
        ``_write_state`` so a crash can never leave a ``final_convergence_recorded`` dispatch with no
        audit record (or vice versa).
        """
        caller_type = os.environ.get(CALLER_TYPE_ENV)
        with self._single_flight():
            state = self._read_state()
            state.setdefault("proposal_results", {})
            state.setdefault("owner_proxy_final_convergence_records", {})
            state.setdefault("dispatched_proposals", {})

            if caller_type != "orchestrator":
                return {
                    "decision": "rejected",
                    "reason": "non_orchestrator_caller",
                    "proposal_id": proposal_id,
                    "caller_type": caller_type,
                }
            orchestrator_proof = self._orchestrator_proof_metadata()
            if orchestrator_proof.get("reason"):
                return {
                    "decision": "rejected",
                    "reason": orchestrator_proof["reason"],
                    "proposal_id": proposal_id,
                    "caller_type": caller_type,
                }
            if not evidence_refs:
                return {
                    "decision": "rejected",
                    "reason": "missing_final_convergence_evidence",
                    "proposal_id": proposal_id,
                }
            if outcome != FINAL_CONVERGENCE_OUTCOME:
                return {
                    "decision": "rejected",
                    "reason": "unsupported_final_convergence_outcome",
                    "proposal_id": proposal_id,
                    "outcome": outcome,
                }

            stored = state["proposal_results"].get(proposal_id)
            if not isinstance(stored, dict) or stored.get("decision") != "accepted":
                return {
                    "decision": "rejected",
                    "reason": "unknown_or_unaccepted_proposal",
                    "proposal_id": proposal_id,
                }
            if stored.get("next_required_action") != "repair_attempts_exhausted":
                return {
                    "decision": "rejected",
                    "reason": "not_final_convergence_obligation",
                    "proposal_id": proposal_id,
                    "next_required_action": stored.get("next_required_action"),
                }
            # Env-inflation convergence guard (LIVE parity). Refuse to record final convergence for an
            # env-INFLATED exhaustion: if excluding the transient escalated-review actuator failures
            # leaves the target still within its content-repair budget, the exhaustion is false (the
            # review never ran) and the target has real repair rounds left — converging it would
            # prematurely consume a recoverable obligation; it must instead flow through its projected
            # repair_active. This reuses the same read-time projection `continue` applies
            # (project_effective_action), so the convergence guard and the continuation path agree.
            # project_effective_action fails closed on a missing/malformed target or counts by keeping the
            # RAW exhausted action, so such state records convergence rather than rejecting — matching LIVE
            # (no separate PUBLIC-only shape guard; the directive keeps PUBLIC == LIVE here).
            convergence_target_id = self._proposal_targets_from_ledger().get(proposal_id)
            convergence_target = (
                state.get("targets", {}).get(convergence_target_id)
                if convergence_target_id
                else None
            )
            if (
                self.project_effective_action(stored, convergence_target)
                != "repair_attempts_exhausted"
            ):
                return {
                    "decision": "rejected",
                    "reason": "not_genuinely_exhausted_excluding_actuator",
                    "proposal_id": proposal_id,
                }
            existing = state["owner_proxy_final_convergence_records"].get(proposal_id)
            if isinstance(existing, dict):
                return {
                    "decision": "recorded",
                    "proposal_id": proposal_id,
                    "final_convergence": dict(existing),
                    "replayed": True,
                }

            record: dict[str, Any] = {
                "actor_role": "orchestrator",
                "caller_type": caller_type,
                "session_id": orchestrator_proof.get("session_id"),
                "project_id": orchestrator_proof.get("project_id"),
                "active_root": orchestrator_proof.get("active_root"),
                "evidence_refs": list(evidence_refs),
                "outcome": outcome,
                "state_revision": stored.get("state_revision"),
                "blocker_code": stored.get("blocker_code"),
                "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            state["owner_proxy_final_convergence_records"][proposal_id] = record
            state["dispatched_proposals"][proposal_id] = {
                "status": "final_convergence_recorded",
                "time": record["time"],
                "authorized_by": "orchestrator",
            }
            self._write_state(state)
            return {"decision": "recorded", "proposal_id": proposal_id, "final_convergence": record}

    def reconcile_spawn_attestation(
        self,
        *,
        proposal_id: str,
        evidence_refs: list[str],
        spawn_session_id: str | None = None,
        release_if_absent: bool = False,
    ) -> dict:
        """Resolve a ``spawned_unattested`` dispatch record after an orchestrator evidence check.

        If a real engine session is found, the orchestrator records its session id and promotes the
        dispatch to a normal ``spawned``. If the orchestrator proves no session exists, it releases
        the consumed token so dispatch can retry once from a known-absent state. Exactly one of
        ``spawn_session_id`` / ``release_if_absent`` must be supplied.
        """
        caller_type = os.environ.get(CALLER_TYPE_ENV)
        with self._single_flight():
            state = self._read_state()
            state.setdefault("dispatched_proposals", {})

            if caller_type != "orchestrator":
                return {
                    "decision": "rejected",
                    "reason": "non_orchestrator_caller",
                    "proposal_id": proposal_id,
                    "caller_type": caller_type,
                }
            orchestrator_proof = self._orchestrator_proof_metadata()
            if orchestrator_proof.get("reason"):
                return {
                    "decision": "rejected",
                    "reason": orchestrator_proof["reason"],
                    "proposal_id": proposal_id,
                    "caller_type": caller_type,
                }
            if not evidence_refs:
                return {
                    "decision": "rejected",
                    "reason": "missing_spawn_attestation_evidence",
                    "proposal_id": proposal_id,
                }
            if bool(spawn_session_id) == bool(release_if_absent):
                return {
                    "decision": "rejected",
                    "reason": "ambiguous_spawn_attestation_reconcile",
                    "proposal_id": proposal_id,
                }

            record = state["dispatched_proposals"].get(proposal_id)
            if not isinstance(record, dict) or record.get("status") != "spawned_unattested":
                return {
                    "decision": "rejected",
                    "reason": "not_spawned_unattested",
                    "proposal_id": proposal_id,
                }

            if spawn_session_id:
                if AO_SPAWN_SESSION_ID_RE.fullmatch(spawn_session_id) is None:
                    return {
                        "decision": "rejected",
                        "reason": "invalid_spawn_session_id",
                        "proposal_id": proposal_id,
                    }
                record.update(
                    {
                        "status": "spawned",
                        "spawn_session_id": spawn_session_id,
                        "spawn_attestation": "session",
                        "attestation_evidence_refs": list(evidence_refs),
                        "attested_by": "orchestrator",
                        "attested_time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    }
                )
                state["dispatched_proposals"][proposal_id] = record
                self._write_state(state)
                return {
                    "decision": "recorded",
                    "proposal_id": proposal_id,
                    "spawn_session_id": spawn_session_id,
                }

            del state["dispatched_proposals"][proposal_id]
            self._write_state(state)
            return {
                "decision": "released",
                "proposal_id": proposal_id,
                "reason": "spawn_session_absent",
            }

    def _proposal_targets_from_ledger(self) -> dict[str, str]:
        """Map proposal_id -> target_id from the transition ledger (brand-neutral; reads only
        self.ledger_path). Used by reconcile_spawned_dispatch to resolve a spawned proposal's target so
        project_effective_action can compute the effective action for the base-lease release gate."""
        targets: dict[str, str] = {}
        if not self.ledger_path.exists():
            return targets
        for line in self.ledger_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            proposal = entry.get("proposal")
            if not isinstance(proposal, dict):
                continue
            proposal_id = proposal.get("proposal_id")
            target_id = proposal.get("target_id")
            if isinstance(proposal_id, str) and isinstance(target_id, str):
                targets[proposal_id] = target_id
        return targets

    def reconcile_spawned_dispatch(
        self,
        *,
        proposal_id: str,
        evidence_refs: list[str],
        release_if_terminal: bool = False,
        refresh_time: bool = False,
        terminal_readback: dict[str, Any] | None = None,
    ) -> dict:
        """Resolve an overdue normal ``spawned`` dispatch, or release a spawn-baseline lease, after
        orchestrator review.

        Normal ``spawned`` lease: with ``--refresh-time`` it stamps the lease time to now (the worker
        is confirmed still progressing). With ``--release-if-terminal`` it deletes the lease IFF it is
        older than ``SPAWNED_DISPATCH_STALL_SECONDS`` (overdue) AND its stored action is safely
        releasable (see the action/revision guards below), so dispatch can retry. Exactly one of the
        two must be supplied.

        Spawn-baseline leases (``spawned_base_mismatch`` / ``spawned_base_unverified``, written by
        record_spawn_baseline_issue) ARE now reconciled here: under ``--release-if-terminal`` plus
        terminal proof (a stored terminated session whose kill readback is ``absent``, or a live
        ``terminal_readback`` whose result is ``absent``) the lease is released so dispatch can retry on
        a correct baseline; otherwise it fails closed (mismatch -> requires termination repair;
        unverified -> requires a terminal readback). ALL three statuses (normal ``spawned`` + both
        spawn-baseline leases) are gated through ONE projected-action allowlist
        (``SPAWNED_DISPATCH_RECONCILE_ACTIONS``), matching LIVE: ``project_effective_action`` is resolved
        once up front, so an env-inflated exhaustion reconciles as the repair it actually is and the
        review/actuator class (``codex_cc_review`` / ``escalated_review``) is rejected by absence from the
        5-set. The ``terminal_readback`` is produced by the CLI ao-session kill readback in a later slice;
        this writer method only consumes it.

        The destructive ``--release-if-terminal`` path is action/revision guarded: a spawned record
        carries no action or revision of its own, so they are resolved up front from the ledger proposal
        + stored proposal_result. A review/actuator obligation (``codex_cc_review`` / ``escalated_review``)
        is NEVER released — it is not in ``SPAWNED_DISPATCH_RECONCILE_ACTIONS``, the same review/actuator
        class ``cmd_reconcile_leases`` spares, because deleting one could let a second actuator
        double-submit. A ``dispatch_next_slice_plan_mode`` lease additionally requires the stored decision
        to still be the current accepted revision, so a stale/superseded global dispatch is not re-enabled.
        ``--refresh-time`` is non-destructive (it only re-stamps the lease); the stale-global-dispatch
        revision guard still applies to it, so a superseded global dispatch lease is neither released nor
        refreshed.
        """
        caller_type = os.environ.get(CALLER_TYPE_ENV)
        with self._single_flight():
            state = self._read_state()
            state.setdefault("dispatched_proposals", {})
            state.setdefault("spawned_dispatch_reconciliations", {})

            if caller_type != "orchestrator":
                return {
                    "decision": "rejected",
                    "reason": "non_orchestrator_caller",
                    "proposal_id": proposal_id,
                    "caller_type": caller_type,
                }
            orchestrator_proof = self._orchestrator_proof_metadata()
            if orchestrator_proof.get("reason"):
                return {
                    "decision": "rejected",
                    "reason": orchestrator_proof["reason"],
                    "proposal_id": proposal_id,
                    "caller_type": caller_type,
                }
            if not evidence_refs:
                return {
                    "decision": "rejected",
                    "reason": "missing_spawned_dispatch_reconcile_evidence",
                    "proposal_id": proposal_id,
                }
            if release_if_terminal == refresh_time:
                return {
                    "decision": "rejected",
                    "reason": "ambiguous_spawned_dispatch_reconcile",
                    "proposal_id": proposal_id,
                }

            record = state["dispatched_proposals"].get(proposal_id)
            if not isinstance(record, dict) or record.get("status") not in {
                "spawned",
                "spawned_base_mismatch",
                "spawned_base_unverified",
            }:
                return {
                    "decision": "rejected",
                    "reason": "not_spawned_dispatch",
                    "proposal_id": proposal_id,
                }

            # Resolve the stored decision + target ONCE and project the effective action, then gate ALL
            # three statuses (normal "spawned" + both spawn-baseline leases) through the unified
            # SPAWNED_DISPATCH_RECONCILE_ACTIONS allowlist (LIVE model). An env-inflated
            # repair_attempts_exhausted projects to repair_active so a spawned repair dispatch reconciles
            # as the repair it actually is, not its raw exhausted token; a genuinely exhausted lease
            # projects to itself (in the 5-set) and is releasable so the obligation re-surfaces for
            # deterministic convergence. codex_cc_review / escalated_review are rejected HERE because they
            # are not in the 5-set — the same review/actuator class the pending-lease janitor spares — so
            # no separate raw protected-action gate is needed on the normal path below.
            proposal_results = state.get("proposal_results", {})
            stored = proposal_results.get(proposal_id) if isinstance(proposal_results, dict) else None
            reconcile_target_id = self._proposal_targets_from_ledger().get(proposal_id)
            reconcile_target = (
                state.get("targets", {}).get(reconcile_target_id) if reconcile_target_id else None
            )
            action = (
                self.project_effective_action(stored, reconcile_target)
                if isinstance(stored, dict)
                else None
            )
            if action not in SPAWNED_DISPATCH_RECONCILE_ACTIONS:
                return {
                    "decision": "rejected",
                    "reason": "not_spawned_dispatch_reconcile_action",
                    "proposal_id": proposal_id,
                    "next_required_action": action,
                }

            # --- Spawn-baseline lease release (b3a). Scoped to the base statuses ONLY; each base branch
            # RETURNS, so a base lease never reaches the normal audit/refresh/elapsed-release block below
            # (the unified action gate above already covered them). ---
            if record.get("status") in {"spawned_base_mismatch", "spawned_base_unverified"}:
                if record.get("status") == "spawned_base_mismatch":
                    termination = record.get("spawn_session_termination")
                    readback = termination.get("readback") if isinstance(termination, dict) else None
                    stored_terminal = (
                        release_if_terminal
                        and isinstance(termination, dict)
                        and termination.get("result") == "terminated"
                        and isinstance(readback, dict)
                        and readback.get("result") == "absent"
                    )
                    live_terminal = (
                        release_if_terminal
                        and isinstance(terminal_readback, dict)
                        and terminal_readback.get("result") == "absent"
                    )
                    if stored_terminal or live_terminal:
                        now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                        outcome = (
                            "released_spawn_baseline_mismatch_terminated"
                            if stored_terminal
                            else "released_spawn_baseline_mismatch_terminal_readback"
                        )
                        audit: dict[str, Any] = {
                            "actor_role": "orchestrator",
                            "caller_type": caller_type,
                            "session_id": orchestrator_proof.get("session_id"),
                            "project_id": orchestrator_proof.get("project_id"),
                            "active_root": orchestrator_proof.get("active_root"),
                            "evidence_refs": list(evidence_refs),
                            "previous_dispatch_record": dict(record),
                            "time": now,
                            "outcome": outcome,
                        }
                        if isinstance(terminal_readback, dict):
                            audit["terminal_readback"] = dict(terminal_readback)
                        history = state["spawned_dispatch_reconciliations"].setdefault(proposal_id, [])
                        if not isinstance(history, list):
                            history = []
                            state["spawned_dispatch_reconciliations"][proposal_id] = history
                        history.append(audit)
                        del state["dispatched_proposals"][proposal_id]
                        self._write_state(state)
                        return {
                            "decision": "released",
                            "proposal_id": proposal_id,
                            "outcome": audit["outcome"],
                        }
                    return {
                        "decision": "rejected",
                        "reason": "spawn_baseline_mismatch_requires_termination_repair",
                        "proposal_id": proposal_id,
                        "next_required_action": action,
                    }
                if record.get("status") == "spawned_base_unverified":
                    if not release_if_terminal:
                        return {
                            "decision": "rejected",
                            "reason": "spawn_baseline_unverified_requires_release_repair",
                            "proposal_id": proposal_id,
                            "next_required_action": action,
                        }
                    if not isinstance(terminal_readback, dict) or terminal_readback.get("result") != "absent":
                        return {
                            "decision": "rejected",
                            "reason": "spawn_baseline_unverified_requires_terminal_readback",
                            "proposal_id": proposal_id,
                            "next_required_action": action,
                        }
                    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                    audit = {
                        "actor_role": "orchestrator",
                        "caller_type": caller_type,
                        "session_id": orchestrator_proof.get("session_id"),
                        "project_id": orchestrator_proof.get("project_id"),
                        "active_root": orchestrator_proof.get("active_root"),
                        "evidence_refs": list(evidence_refs),
                        "previous_dispatch_record": dict(record),
                        "terminal_readback": dict(terminal_readback),
                        "time": now,
                        "outcome": "released_spawn_baseline_unverified",
                    }
                    history = state["spawned_dispatch_reconciliations"].setdefault(proposal_id, [])
                    if not isinstance(history, list):
                        history = []
                        state["spawned_dispatch_reconciliations"][proposal_id] = history
                    history.append(audit)
                    del state["dispatched_proposals"][proposal_id]
                    self._write_state(state)
                    return {
                        "decision": "released",
                        "proposal_id": proposal_id,
                        "outcome": audit["outcome"],
                    }

            # GLOBAL next-slice revision guard (LIVE model). A dispatch_next_slice_plan_mode lease is
            # GLOBAL (it dispatches "whatever comes next" for the whole project, not a per-target
            # obligation): it is only current while its decision is STILL the canonical head. If canonical
            # state advanced past the revision it was accepted at, a newer accepted decision has superseded
            # it, so the lease is a STALE global dispatch and must be neither released NOR refreshed (err
            # toward NOT re-enabling an obsolete next-slice). stored missing state_revision => None != int
            # => rejects, the safe direction.
            if (
                action == "dispatch_next_slice_plan_mode"
                and isinstance(stored, dict)
                and stored.get("state_revision") != state.get("state_revision")
            ):
                return {
                    "decision": "rejected",
                    "reason": "not_current_spawned_dispatch_obligation",
                    "proposal_id": proposal_id,
                    "next_required_action": action,
                }
            elapsed_seconds = _spawned_dispatch_elapsed_seconds(record)
            if release_if_terminal and elapsed_seconds is None:
                return {
                    "decision": "rejected",
                    "reason": "spawned_dispatch_age_indeterminate",
                    "proposal_id": proposal_id,
                    "threshold_seconds": SPAWNED_DISPATCH_STALL_SECONDS,
                }
            if release_if_terminal and (
                elapsed_seconds is not None
                and elapsed_seconds < SPAWNED_DISPATCH_STALL_SECONDS
            ):
                return {
                    "decision": "rejected",
                    "reason": "spawned_dispatch_not_overdue",
                    "proposal_id": proposal_id,
                    "elapsed_seconds": elapsed_seconds,
                    "threshold_seconds": SPAWNED_DISPATCH_STALL_SECONDS,
                }

            now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            audit: dict[str, Any] = {
                "actor_role": "orchestrator",
                "caller_type": caller_type,
                "session_id": orchestrator_proof.get("session_id"),
                "project_id": orchestrator_proof.get("project_id"),
                "active_root": orchestrator_proof.get("active_root"),
                "evidence_refs": list(evidence_refs),
                "previous_dispatch_record": dict(record),
                "time": now,
            }
            history = state["spawned_dispatch_reconciliations"].setdefault(proposal_id, [])
            if not isinstance(history, list):
                history = []
                state["spawned_dispatch_reconciliations"][proposal_id] = history

            if release_if_terminal:
                audit["outcome"] = "released_terminal"
                history.append(audit)
                del state["dispatched_proposals"][proposal_id]
                self._write_state(state)
                return {
                    "decision": "released",
                    "proposal_id": proposal_id,
                    "outcome": audit["outcome"],
                }

            record.update(
                {
                    "time": now,
                    "time_reconciled_by": "orchestrator",
                    "time_reconcile_evidence_refs": list(evidence_refs),
                }
            )
            audit["outcome"] = "refreshed_time"
            audit["new_dispatch_record"] = dict(record)
            history.append(audit)
            state["dispatched_proposals"][proposal_id] = record
            self._write_state(state)
            return {
                "decision": "refreshed",
                "proposal_id": proposal_id,
                "outcome": audit["outcome"],
            }


def _spawned_dispatch_elapsed_seconds(record: dict[str, Any]) -> float | None:
    """Seconds since a spawned dispatch lease's ``time`` stamp, or None if it cannot be parsed
    or is in the future (clock skew)."""
    raw = record.get("time")
    if not isinstance(raw, str):
        return None
    try:
        claimed = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        try:
            claimed = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if claimed.tzinfo is None:
        claimed = claimed.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - claimed.astimezone(timezone.utc)).total_seconds()
    if elapsed < 0:
        return None
    return elapsed


class _FileLock:
    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> "_FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Whole-file advisory lock via fcntl.flock: the kernel grants it to exactly one open
        # file description at a time ACROSS processes (the state writer runs as separate
        # `ao-state-writer` CLI invocations) and releases it AUTOMATICALLY when the holder closes
        # the fd, exits, or is SIGKILLed. That makes a hard kill structurally unable to deadlock
        # the writer and removes the manual stale-reclaim entirely. The previous O_EXCL +
        # rename-by-path reclaim had a TOCTOU: a waiter that had classified
        # the lock stale would later rename self.path BY PATH, stealing a lock a SECOND waiter had
        # freshly created in the classify->rename window -> two writers in the critical section ->
        # state.json lost update. A held flock has no such window.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        delay = LOCK_INITIAL_BACKOFF_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                # Another holder owns the lock (EAGAIN/EWOULDBLOCK). Bounded-wait, then fail closed
                # with the SAME outward exception shape callers/tests expect on contention timeout.
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise FileExistsError(
                        f"state lock {self.path} not acquired within {LOCK_TIMEOUT_SECONDS}s"
                    )
                time.sleep(delay)
                delay = min(delay * 2, LOCK_MAX_BACKOFF_SECONDS)
            except OSError:
                os.close(fd)
                raise
        self.fd = fd
        # Forensics only (NOT used for liveness/reclaim anymore): record the current holder so a
        # human can see who holds the lock. Best-effort; the flock alone guarantees exclusion.
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, f"pid={os.getpid()} time={time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n".encode("utf-8"))
            os.fsync(fd)
        except OSError:
            pass
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(self.fd)
            self.fd = None
        # Intentionally do NOT unlink the lockfile: with flock the PATH is the stable lock
        # namespace. Unlinking it would let a concurrent acquirer create+lock a NEW inode at the
        # same path while a still-open fd holds the lock on the unlinked inode -> two holders. An
        # empty leftover lockfile is harmless (the next acquirer simply flocks it).
