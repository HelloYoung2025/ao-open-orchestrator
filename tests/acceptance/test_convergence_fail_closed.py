"""Fail-closed regression harness for the two safety findings on the convergence/reconcile consumers.

WHY THIS EXISTS (the spec, not just a check)
--------------------------------------------
Two consumers in the L2 convergence-consumer port could fail OPEN:

  * record-final-convergence (writer.record_final_convergence) accepted ANY accepted
    ``repair_attempts_exhausted`` obligation. ``_accept`` now routes ENVIRONMENTAL faults (review
    timeouts / actuator failures that never produced a content verdict) through a dedicated lane that is
    excluded from the per-target content budget, so it no longer falsely exhausts a still-recoverable
    target. record-final-convergence keeps an independent guard as defense in depth: a legacy/migrated
    state.json written by a pre-env-lane engine could still carry an env-inflated exhaustion, and
    converging a still-recoverable target is a real loss — so the guard re-excludes the env set and
    rejects an env-inflated exhaustion.

  * reconcile-spawned-dispatch --release-if-terminal deleted ANY overdue ``spawned`` lease without
    checking what it was dispatched FOR. Age alone is not terminal proof: a long-running external
    review/actuator obligation legitimately holds its lease far past the orphan floor, and releasing
    one could let a second actuator double-submit; a stale GLOBAL next-slice dispatch could be
    re-enabled after newer work superseded it.

These tests are the executable proof that BOTH now fail CLOSED.

DISCIPLINE
----------
* BLACK-BOX against the public CLI / public package only; no private repo is read.
* The env-coded EXHAUSTION can no longer be produced through ``apply`` (the env lane prevents it at
  write time — see test_accept_env_lane_prevents_false_exhaustion). The F1 guard test therefore builds
  real ledger/target state through ``apply`` with a REAL public env blocker code
  (codex_cc_review_timeout watchdog-timeout), then DIRECTLY SEEDS the env-inflated exhaustion a
  pre-env-lane engine would have persisted — the legacy/migrated dead-end the guard must still reject.
* State is only edited directly to (a) AGE a lease past the stall floor and (b) advance the canonical
  head for the stale-dispatch case — both are time/revision conditions the harness cannot otherwise
  reach in-process within a single test run.
* The orchestrator identity each consumer requires is passed explicitly (conftest.cli_env strips AO_*).
"""

from __future__ import annotations

import hashlib
import json

from conftest import _state_paths

from ao_state_writer.writer import (
    CODEX_CC_MODEL,
    CODEX_CC_REASONING_EFFORT,
    CODEX_CC_TIMEOUT_BLOCKER_CODE,
    ESCALATED_REVIEW_ACTUATOR_FAILURE_BLOCKER_CODE,
    MAX_ENVIRONMENT_ATTEMPTS,
    MAX_REPAIR_ATTEMPTS_TOTAL,
    SPAWNED_DISPATCH_STALL_SECONDS,
    StateTransitionProposal,
)

ORCHESTRATOR_ENV = {
    "AO_CALLER_TYPE": "orchestrator",
    "AO_SESSION_ID": "example-orchestrator",
}

TARGET = "chapter-1"


def _proposal(**overrides) -> StateTransitionProposal:
    base = dict(
        proposal_id="p-default",
        target_kind="small_chapter",
        target_id=TARGET,
        base_state_revision=0,
        requested_state="evidence_pending",
        actor_role="implementer",
        evidence_refs=["evidence#1"],
    )
    base.update(overrides)
    return StateTransitionProposal(**base)


def _read_state(project) -> dict:
    state_path, _ = _state_paths(project.root)
    return json.loads(state_path.read_text(encoding="utf-8"))


def _write_state(project, state: dict) -> None:
    state_path, _ = _state_paths(project.root)
    state_path.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def _age_dispatch_lease(project, proposal_id: str) -> None:
    """Backdate a spawned lease's ``time`` well past SPAWNED_DISPATCH_STALL_SECONDS so the overdue
    age check passes and the new action/revision guards are the thing under test."""

    state = _read_state(project)
    record = state["dispatched_proposals"][proposal_id]
    assert record["status"] == "spawned", record
    # A fixed past timestamp comfortably older than the 600s stall floor (years, to be skew-proof).
    record["time"] = "2000-01-01T00:00:00+0000"
    _write_state(project, state)


def _drive_env_inflated_exhaustion(project, monkeypatch) -> None:
    """Seed an env-inflated repair_attempts_exhausted obligation — the legacy/migrated dead-end a
    PRE-env-lane engine could persist (env faults counted toward the per-target total). The current
    _accept env lane PREVENTS this at write time (see test_accept_env_lane_prevents_false_exhaustion),
    so it can no longer be produced in-process; it is seeded directly to prove record-final-convergence
    still fails CLOSED against such state — defense in depth for legacy/migrated state.json.

    The final blocker (p-env-blocker-4) is the convergence obligation; its ledger entry is
    non_retryable=False, so the F1 guard's non_retryable carve-out does NOT apply and the seeded
    exhaustion is purely env-inflated (repair_attempts == {codex_cc_review_timeout: N}, content_repair_total == 0).
    """

    writer = project.writer()

    seed = writer.apply(_proposal(proposal_id="p-seed-1", requested_state="evidence_pending"))
    assert seed.decision == "accepted", seed
    assert seed.next_required_action == "codex_cc_review"

    # Drive env-coded blockers through the documented path to build a REAL state + ledger (so the F1
    # guard's ledger->target resolution works). Post-env-lane these stay repair_active — the write path
    # correctly refuses to exhaust on env faults.
    revision = 1
    last = None
    blocker_count = MAX_REPAIR_ATTEMPTS_TOTAL + 1
    monkeypatch.setenv("AO_CALLER_TYPE", "watchdog")
    for index in range(blocker_count):
        last = writer.apply(
            _proposal(
                proposal_id=f"p-env-blocker-{index}",
                base_state_revision=revision,
                requested_state="review_blocked",
                actor_role="codex_cc",
                review_scope="codex_cc",
                verdict="blocker",
                review_mode="watchdog_timeout",
                blocker_code=CODEX_CC_TIMEOUT_BLOCKER_CODE,
                evidence_refs=[f"evidence#env-blocker-{index}"],
            )
        )
        assert last.decision == "accepted", last
        revision += 1
    monkeypatch.delenv("AO_CALLER_TYPE", raising=False)

    # blocker_count (5) == MAX_ENVIRONMENT_ATTEMPTS, NOT > it, so the env lane keeps the last obligation
    # RECOVERABLE (repair_active), never repair_attempts_exhausted. This is the B4 write-time fix proving
    # the env-inflated exhaustion is no longer reachable via apply.
    assert last is not None
    assert last.next_required_action == "repair_active", last

    # Directly seed the env-inflated EXHAUSTION a pre-env-lane engine would have written: flip the final
    # obligation + target to repair_attempts_exhausted while repair_attempts stay ALL env-coded. This is
    # the legacy/migrated dead-end record-final-convergence must still reject.
    state = _read_state(project)
    target = state["targets"][TARGET]
    target["state"] = "repair_attempts_exhausted"
    obligation = state["proposal_results"]["p-env-blocker-4"]
    obligation["new_state"] = "repair_attempts_exhausted"
    obligation["next_required_action"] = "repair_attempts_exhausted"
    _write_state(project, state)

    attempts = target["repair_attempts"]
    assert attempts == {CODEX_CC_TIMEOUT_BLOCKER_CODE: blocker_count}, attempts
    assert target["repair_attempts_total"] == blocker_count


def _drive_to_genuine_content_exhaustion_by_count(project, monkeypatch) -> str:
    """Drive chapter-1 to repair_attempts_exhausted using RETRYABLE CONTENT blockers (no non_retryable),
    so exhaustion is reached purely by the content count overflowing MAX_REPAIR_ATTEMPTS_TOTAL. Returns
    the proposal_id of the exhausting obligation. This is the genuine-content path the F1 guard must
    still ACCEPT (proving the guard is conservative, not blanket-rejecting)."""

    writer = project.writer()

    seed = writer.apply(_proposal(proposal_id="p-seed-1", requested_state="evidence_pending"))
    assert seed.decision == "accepted", seed

    # Retryable content blockers under varied codes so neither the per-code cap (MAX_REPAIR_ATTEMPTS)
    # nor non_retryable trips first — only the per-target CONTENT total (MAX_REPAIR_ATTEMPTS_TOTAL).
    revision = 1
    last = None
    last_id = ""
    blocker_count = MAX_REPAIR_ATTEMPTS_TOTAL + 1
    # A blocker must carry a real reviewer scope (unscoped blockers are refused). These are CONTENT
    # blockers from a codex_cc review; AO_CALLER_TYPE mirrors that caller.
    monkeypatch.setenv("AO_CALLER_TYPE", "codex_cc")
    for index in range(blocker_count):
        # Real small-chapter repair workflow: each codex_cc round re-enters via a worker
        # evidence_pending re-open (the seed covers round 0). The C-FIX-7 freshness guard requires
        # this — a codex_cc receipt is only live at evidence_pending — and repair_attempts/_total
        # PERSIST across the re-open (no verdict), so exhaustion still lands at the same count.
        if index > 0:
            reopen = writer.apply(
                _proposal(
                    proposal_id=f"p-content-reopen-{index}",
                    base_state_revision=revision,
                    requested_state="evidence_pending",
                    actor_role="worker",
                    evidence_refs=[f"session-log#content-reopen-{index}"],
                )
            )
            assert reopen.decision == "accepted", reopen
            revision += 1
        last_id = f"p-content-blocker-{index}"
        last = writer.apply(
            _proposal(
                proposal_id=last_id,
                base_state_revision=revision,
                requested_state="review_blocked",
                actor_role="codex_cc",
                review_scope="codex_cc",
                evidence_refs=[f"evidence#content-blocker-{index}"],
                verdict="blocker",
                blocker_code=f"content_fail_{index}",
                model=CODEX_CC_MODEL,
                reasoning_effort=CODEX_CC_REASONING_EFFORT,
                non_retryable=False,
            )
        )
        assert last.decision == "accepted", last
        revision += 1
    monkeypatch.delenv("AO_CALLER_TYPE", raising=False)

    assert last is not None
    assert last.next_required_action == "repair_attempts_exhausted", last
    state = _read_state(project)
    attempts = state["targets"][TARGET]["repair_attempts"]
    # All content-coded, distinct codes, each count 1; content_repair_total == blocker_count > cap.
    assert sum(attempts.values()) == blocker_count, attempts
    assert all(not code.startswith("codex_cc") for code in attempts), attempts
    return last_id


def test_f1_env_inflated_exhaustion_is_rejected(lifecycle_project, monkeypatch):
    """F1: record-final-convergence FAILS CLOSED for an env-inflated exhaustion.

    The target's repair_attempts are ALL env-coded (codex_cc_review_timeout); excluding the env set
    leaves content_repair_total == 0, so the exhaustion is false. record-final-convergence must reject
    ``not_genuinely_exhausted_excluding_actuator`` (exit 2) and MUST NOT consume the obligation. The
    guard is LIVE parity: it reuses the SAME read-time projection ``continue`` applies
    (``project_effective_action``) — the env-inflated exhaustion projects to repair_active, so it is not
    genuinely exhausted and convergence is refused. Both halves of the env-inflation defense are then
    exercised: the writer guard REFUSES to falsely converge, and the cli read-time effective-action
    projection (E2) ACTIVELY RECOVERS the still-converging target — ``continue`` projects the false
    exhaustion to the repair_active it really is and re-dispatches it, instead of dead-ending at
    owner_proxy_convergence_required.
    """

    project = lifecycle_project
    _drive_env_inflated_exhaustion(project, monkeypatch)

    converged = project.cli(
        "record-final-convergence",
        "--root",
        str(project.root),
        "--proposal-id",
        "p-env-blocker-4",
        "--evidence",
        "owner-proxy:final-convergence",
        **ORCHESTRATOR_ENV,
    )
    assert converged.returncode == 2, (converged.returncode, converged.stdout, converged.stderr)
    payload = json.loads(converged.stdout)
    assert payload["result"] == "owner_proxy_final_convergence_rejected", payload
    assert payload["reason"] == "not_genuinely_exhausted_excluding_actuator", payload

    # Obligation NOT converged (the guard refused) AND actively recovered: with the E2 read-time
    # effective-action projection, `continue` sees the env-inflated false exhaustion as the
    # repair_active it really is and re-dispatches it (result "spawned", exit 0), instead of
    # dead-ending at owner_proxy_convergence_required. A genuine content exhaustion would project to
    # itself and still route to convergence — proven by the genuine-exhaustion tests below.
    cont = project.cli("continue", "--root", str(project.root))
    assert cont.returncode == 0, (cont.returncode, cont.stdout, cont.stderr)
    assert json.loads(cont.stdout)["result"] == "spawned"


def test_f1_genuine_content_exhaustion_by_count_still_records(lifecycle_project, monkeypatch):
    """F1 conservatism: a GENUINE content exhaustion reached by COUNT (no non_retryable) still records.

    This proves the guard does not blanket-reject: a target whose CONTENT blockers overflowed the
    per-target content cap is genuinely exhausted (content_repair_total > MAX_REPAIR_ATTEMPTS_TOTAL),
    so record-final-convergence ACCEPTS it (exit 0) and the loop reaches the converged terminal. The
    happy-path harness covers the non_retryable branch; this covers the count-overflow branch.
    """

    project = lifecycle_project
    obligation_id = _drive_to_genuine_content_exhaustion_by_count(project, monkeypatch)

    converged = project.cli(
        "record-final-convergence",
        "--root",
        str(project.root),
        "--proposal-id",
        obligation_id,
        "--evidence",
        "owner-proxy:final-convergence",
        **ORCHESTRATOR_ENV,
    )
    assert converged.returncode == 0, (converged.returncode, converged.stdout, converged.stderr)
    payload = json.loads(converged.stdout)
    assert payload["result"] == "owner_proxy_final_convergence_recorded", payload

    cont = project.cli("continue", "--root", str(project.root))
    assert cont.returncode == 0, (cont.returncode, cont.stdout, cont.stderr)
    assert json.loads(cont.stdout)["result"] == "nothing_to_continue"


def test_accept_env_lane_prevents_false_exhaustion(lifecycle_project, monkeypatch):
    """Write-time prevention: env-coded faults NEVER reach repair_attempts_exhausted via _accept.

    Pre-env-lane, _accept counted EVERY blocker (incl. env review timeouts) toward the per-target repair
    total, so a run of transient env faults could falsely exhaust a still-recoverable target. The env
    lane excludes env faults from content exhaustion: up to MAX_ENVIRONMENT_ATTEMPTS they stay
    recoverable (repair_active), and once cumulative env faults cross the bound the target escalates to
    the owner-visible review_environment_unavailable obligation — never the false repair_attempts_exhausted
    dead-end. This is the write-time layer that makes the record-final-convergence guard rarely reachable.
    """

    project = lifecycle_project
    writer = project.writer()

    seed = writer.apply(_proposal(proposal_id="p-seed-1", requested_state="evidence_pending"))
    assert seed.decision == "accepted", seed

    revision = 1
    monkeypatch.setenv("AO_CALLER_TYPE", "watchdog")
    # Up to AND including MAX_ENVIRONMENT_ATTEMPTS env faults stay repair_active — even though the RAW
    # repair_attempts_total crosses MAX_REPAIR_ATTEMPTS_TOTAL (4) at fault #5, which the pre-env-lane
    # code would have falsely exhausted on.
    for index in range(MAX_ENVIRONMENT_ATTEMPTS):
        last = writer.apply(
            _proposal(
                proposal_id=f"p-env-{index}",
                base_state_revision=revision,
                requested_state="review_blocked",
                actor_role="codex_cc",
                review_scope="codex_cc",
                verdict="blocker",
                review_mode="watchdog_timeout",
                blocker_code=CODEX_CC_TIMEOUT_BLOCKER_CODE,
                evidence_refs=[f"evidence#env-{index}"],
            )
        )
        assert last.decision == "accepted", last
        assert last.next_required_action == "repair_active", (index, last)
        revision += 1

    # One more env fault crosses the bound -> owner-visible escalation, still NOT exhausted.
    crossing = writer.apply(
        _proposal(
            proposal_id="p-env-cross",
            base_state_revision=revision,
            requested_state="review_blocked",
            actor_role="codex_cc",
            review_scope="codex_cc",
            verdict="blocker",
            review_mode="watchdog_timeout",
            blocker_code=CODEX_CC_TIMEOUT_BLOCKER_CODE,
            evidence_refs=["evidence#env-cross"],
        )
    )
    monkeypatch.delenv("AO_CALLER_TYPE", raising=False)
    assert crossing.decision == "accepted", crossing
    assert crossing.next_required_action == "review_environment_unavailable", crossing

    state = _read_state(project)
    target = state["targets"][TARGET]
    assert target["repair_attempts"] == {CODEX_CC_TIMEOUT_BLOCKER_CODE: MAX_ENVIRONMENT_ATTEMPTS + 1}, target
    # Raw total (6) exceeds MAX_REPAIR_ATTEMPTS_TOTAL (4): env faults did NOT inflate content exhaustion.
    assert target["repair_attempts_total"] == MAX_ENVIRONMENT_ATTEMPTS + 1


def test_apply_rejects_forged_env_code_without_legitimate_mode(lifecycle_project):
    """Reserved-code gate: a proposal stamping a reserved ENV blocker code WITHOUT its legitimate
    (scope, mode) producer is rejected at apply() — it must NOT enter the env lane by code alone, which
    would dodge the content give-up budget and could falsely escalate to review_environment_unavailable.
    """

    project = lifecycle_project
    writer = project.writer()
    seed = writer.apply(_proposal(proposal_id="p-seed-1", requested_state="evidence_pending"))
    assert seed.decision == "accepted", seed

    forged = writer.apply(
        _proposal(
            proposal_id="p-forged-env",
            base_state_revision=1,
            requested_state="review_blocked",
            actor_role="codex_cc",
            review_scope="codex_cc",
            verdict="blocker",
            review_mode=None,  # the codex_cc env code REQUIRES watchdog_timeout — this is a forged tuple
            blocker_code=CODEX_CC_TIMEOUT_BLOCKER_CODE,
            evidence_refs=["evidence#forged-env"],
        )
    )
    assert forged.decision == "rejected", forged
    assert forged.reason == "reserved_environment_blocker_code_mismatch", forged

    # The forged tuple recorded NO env attempt — it never reached the env lane.
    state = _read_state(project)
    assert state["targets"].get(TARGET, {}).get("repair_attempts", {}) == {}, state


def test_apply_rejects_forged_actuator_code_from_wrong_scope(lifecycle_project):
    """Reserved-code gate: an escalated-review actuator-failure env code carried by a codex_cc-scope proposal is
    not its legitimate producer, so apply() rejects it rather than letting it enter the env lane."""

    project = lifecycle_project
    writer = project.writer()
    seed = writer.apply(_proposal(proposal_id="p-seed-1", requested_state="evidence_pending"))
    assert seed.decision == "accepted", seed

    forged = writer.apply(
        _proposal(
            proposal_id="p-forged-actuator",
            base_state_revision=1,
            requested_state="review_blocked",
            actor_role="codex_cc",
            review_scope="codex_cc",  # actuator env code from the wrong scope -> illegitimate producer
            verdict="blocker",
            review_mode="actuator_failure",
            blocker_code=ESCALATED_REVIEW_ACTUATOR_FAILURE_BLOCKER_CODE,
            evidence_refs=["evidence#forged-actuator"],
        )
    )
    assert forged.decision == "rejected", forged
    assert forged.reason == "reserved_environment_blocker_code_mismatch", forged


def test_f2_protected_review_action_spawned_lease_is_not_released(lifecycle_project, monkeypatch):
    """F2: an overdue spawned lease for a PROTECTED review action is NOT released.

    Seeding then ``continue`` spawns p-seed-1, whose obligation is ``codex_cc_review`` — an external
    review/actuator obligation that legitimately holds its lease far past the stall floor (releasing it
    could let a second actuator double-submit). The aligned LIVE model gates the lease through the
    unified projected-action allowlist BEFORE the age check, so codex_cc_review is rejected because it is
    NOT in SPAWNED_DISPATCH_RECONCILE_ACTIONS (the same review/actuator class the pending-lease janitor
    spares) — age is not what blocks it. Even AGED past SPAWNED_DISPATCH_STALL_SECONDS,
    --release-if-terminal must reject ``not_spawned_dispatch_reconcile_action`` (exit 2) and leave the
    lease intact.
    """

    project = lifecycle_project
    writer = project.writer()

    seed = writer.apply(_proposal(proposal_id="p-seed-1"))
    assert seed.decision == "accepted"
    assert seed.next_required_action == "codex_cc_review"
    spawned = project.cli("continue", "--root", str(project.root))
    assert spawned.returncode == 0, spawned.stderr
    assert json.loads(spawned.stdout)["result"] == "spawned"

    _age_dispatch_lease(project, "p-seed-1")

    released = project.cli(
        "reconcile-spawned-dispatch",
        "--root",
        str(project.root),
        "--proposal-id",
        "p-seed-1",
        "--evidence",
        "orchestrator:overdue-check",
        "--release-if-terminal",
        **ORCHESTRATOR_ENV,
    )
    assert released.returncode == 2, (released.returncode, released.stdout, released.stderr)
    payload = json.loads(released.stdout)
    assert payload["result"] == "spawned_dispatch_reconcile_rejected", payload
    assert payload["reason"] == "not_spawned_dispatch_reconcile_action", payload
    assert payload["next_required_action"] == "codex_cc_review", payload

    # The lease is intact (still 'spawned'), not deleted.
    state = _read_state(project)
    assert state["dispatched_proposals"]["p-seed-1"]["status"] == "spawned", state


def test_f2_stale_global_dispatch_revision_is_not_released(lifecycle_project, monkeypatch):
    """F2: an overdue ``dispatch_next_slice_plan_mode`` lease whose revision is stale is NOT released.

    Close one chapter -> p-close-1 carries the GLOBAL next-slice obligation
    (dispatch_next_slice_plan_mode) accepted at the then-current head revision. Confirm a spawned lease
    for it and age it overdue. While its decision revision is STILL the canonical head, release is
    ALLOWED (positive control). After a later accepted apply advances the head past it, the lease is a
    STALE global dispatch and --release-if-terminal must reject ``not_current_spawned_dispatch_obligation``
    (the LIVE revision guard, which now also blocks --refresh-time, not only release).
    """

    project = lifecycle_project
    writer = project.writer()

    # seed -> codex_cc receipt (stubbed via env) -> close => p-close-1 dispatch_next_slice_plan_mode.
    seed = writer.apply(_proposal(proposal_id="p-seed-1"))
    assert seed.decision == "accepted"
    monkeypatch.setenv("AO_CALLER_TYPE", "codex_cc")
    transcript = project.root / "reports" / "codex-cc-receipts" / "p-cc-1.txt"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("codex-cc transcript ok", encoding="utf-8")
    transcript_sha = hashlib.sha256(transcript.read_bytes()).hexdigest()
    receipt = writer.apply(
        _proposal(
            proposal_id="p-cc-1",
            base_state_revision=1,
            actor_role="codex_cc",
            review_scope="codex_cc",
            verdict="pass",
            model=CODEX_CC_MODEL,
            reasoning_effort=CODEX_CC_REASONING_EFFORT,
            evidence_refs=["transcript#1"],
            codex_cc_transcript_sha256=transcript_sha,
            codex_cc_transcript_artifact_ref="artifact:reports/codex-cc-receipts/p-cc-1.txt",
        )
    )
    assert receipt.next_required_action == "state_writer_closure", receipt
    monkeypatch.delenv("AO_CALLER_TYPE", raising=False)
    close = writer.apply(
        _proposal(
            proposal_id="p-close-1",
            base_state_revision=2,
            requested_state="closed",
            evidence_refs=["evidence#close"],
        )
    )
    assert close.next_required_action == "dispatch_next_slice_plan_mode", close

    # Confirm a spawned lease for the global dispatch obligation and age it overdue.
    writer.confirm_dispatch("p-close-1", authorized_by="orchestrator_policy")
    _age_dispatch_lease(project, "p-close-1")

    # POSITIVE CONTROL: while the decision revision still equals the canonical head, the aged global
    # dispatch lease IS releasable (the revision guard does not over-reject a current dispatch).
    head_state = _read_state(project)
    assert head_state["state_revision"] == close.state_revision, head_state
    released_current = project.cli(
        "reconcile-spawned-dispatch",
        "--root",
        str(project.root),
        "--proposal-id",
        "p-close-1",
        "--evidence",
        "orchestrator:overdue-check",
        "--release-if-terminal",
        **ORCHESTRATOR_ENV,
    )
    assert released_current.returncode == 0, (released_current.stdout, released_current.stderr)
    assert json.loads(released_current.stdout)["result"] == "spawned_dispatch_terminal_released"

    # Re-confirm + re-age the same lease, then ADVANCE the canonical head past p-close-1's revision so
    # the stored decision is now a STALE global dispatch.
    writer.confirm_dispatch("p-close-1", authorized_by="orchestrator_policy")
    _age_dispatch_lease(project, "p-close-1")
    advance = writer.apply(
        _proposal(proposal_id="p-seed-2", target_id="chapter-2", base_state_revision=close.state_revision)
    )
    assert advance.decision == "accepted", advance
    advanced_state = _read_state(project)
    assert advanced_state["state_revision"] > advanced_state["proposal_results"]["p-close-1"]["state_revision"], (
        advanced_state["state_revision"],
        advanced_state["proposal_results"]["p-close-1"]["state_revision"],
    )

    stale = project.cli(
        "reconcile-spawned-dispatch",
        "--root",
        str(project.root),
        "--proposal-id",
        "p-close-1",
        "--evidence",
        "orchestrator:overdue-check",
        "--release-if-terminal",
        **ORCHESTRATOR_ENV,
    )
    assert stale.returncode == 2, (stale.returncode, stale.stdout, stale.stderr)
    payload = json.loads(stale.stdout)
    assert payload["result"] == "spawned_dispatch_reconcile_rejected", payload
    assert payload["reason"] == "not_current_spawned_dispatch_obligation", payload

    # The stale lease is intact (still 'spawned'), not deleted.
    assert advanced_state["dispatched_proposals"]["p-close-1"]["status"] == "spawned", advanced_state


def test_aligned_env_inflated_exhausted_spawned_lease_is_releasable(lifecycle_project, monkeypatch):
    """ALIGN (raw->projected): an overdue spawned lease whose RAW action is an ENV-INFLATED
    ``repair_attempts_exhausted`` IS releasable, so the still-recoverable target is not wedged.

    WHY THIS MATTERS (the anti-stall purpose, not just an exit code)
    ---------------------------------------------------------------
    Before aligning to LIVE, the normal-spawned reconcile path gated on the RAW
    ``next_required_action`` against a 4-set that excluded ``repair_attempts_exhausted``. An
    env-inflated exhaustion (``repair_attempts`` all env-coded, content_repair_total == 0) therefore
    presented its raw exhausted token and was rejected ``spawned_dispatch_unreleasable_action`` — an
    overdue, stalled spawned lease that could NEVER be released, a permanent anti-stall hole (a spawned
    lease has no auto-expiry; only reconcile releases it). LIVE projects the action first: the
    env-inflated exhaustion projects to the ``repair_active`` it really is (in the 5-set), so the lease
    is released, the obligation re-surfaces, and the read-time projection re-dispatches the recovering
    target instead of leaving a dead lease behind. This test proves the released path AND the post-release
    direction (recover, not dead-end).
    """

    project = lifecycle_project
    _drive_env_inflated_exhaustion(project, monkeypatch)

    # `continue` sees the env-inflated false exhaustion as repair_active (E2 read-time projection) and
    # spawns a dispatch lease for the recovering target.
    spawned_cont = project.cli("continue", "--root", str(project.root))
    assert spawned_cont.returncode == 0, (spawned_cont.stdout, spawned_cont.stderr)
    assert json.loads(spawned_cont.stdout)["result"] == "spawned", spawned_cont.stdout

    state = _read_state(project)
    spawned = {pid: r for pid, r in state["dispatched_proposals"].items() if r.get("status") == "spawned"}
    assert len(spawned) == 1, spawned
    spawned_pid = next(iter(spawned))
    # The lease's stored obligation is the env-inflated exhaustion (raw token), the legacy dead-end.
    assert state["proposal_results"][spawned_pid]["next_required_action"] == "repair_attempts_exhausted", state

    _age_dispatch_lease(project, spawned_pid)

    released = project.cli(
        "reconcile-spawned-dispatch",
        "--root",
        str(project.root),
        "--proposal-id",
        spawned_pid,
        "--evidence",
        "orchestrator:overdue-check",
        "--release-if-terminal",
        **ORCHESTRATOR_ENV,
    )
    # RELEASED (projected repair_active is reconcilable), exit 0 — NOT rejected unreleasable.
    assert released.returncode == 0, (released.returncode, released.stdout, released.stderr)
    assert json.loads(released.stdout)["result"] == "spawned_dispatch_terminal_released", released.stdout

    # The stale lease is actually DELETED, not merely re-stamped.
    after = _read_state(project)
    assert spawned_pid not in after["dispatched_proposals"], after["dispatched_proposals"]

    # POST-RELEASE DIRECTION: with the lease gone, the recovering target is re-dispatched (repair_active),
    # NOT dead-ended at owner_proxy_convergence_required — the lease release actively unblocks recovery.
    cont = project.cli("continue", "--root", str(project.root))
    assert cont.returncode == 0, (cont.returncode, cont.stdout, cont.stderr)
    assert json.loads(cont.stdout)["result"] == "spawned", cont.stdout
