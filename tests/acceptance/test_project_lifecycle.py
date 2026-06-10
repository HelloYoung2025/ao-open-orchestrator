"""Public acceptance harness: drive ONE project through its full lifecycle.

WHY THIS EXISTS (the spec, not just a check)
--------------------------------------------
The public ``ao_state_writer`` core can SEED, DISPATCH a worker (spawn + session
attestation), record a Codex-cc review receipt, and CLOSE a single small chapter.
It can NOW ALSO *converge and close the project* once a target dead-ends: a
``repair_attempts_exhausted`` obligation routes to a fail-closed
``owner_proxy_convergence_required`` envelope, and the public CLI now carries the
consumer that RESOLVES it — ``record-final-convergence``.

Three convergence/close subcommands were added to the public CLI (the L2
convergence-consumer port):
    * record-final-convergence  (the convergence-close happy path)
    * reconcile-spawn-attestation
    * reconcile-spawned-dispatch
``test_convergence_close_is_reachable`` is the executable proof that the loop now
reaches a converged/closed terminal instead of dead-ending forever. The two
reconcile consumers are wired but public-state-scoped (fail-closed on states the
public core does not currently produce); ``test_reconcile_consumers_wired_but_inert``
locks that contract.

DISCIPLINE
----------
* BLACK-BOX against the public CLI / public package only. No private repo is read.
* The codex_cc review is STUBBED purely via env ``AO_CALLER_TYPE=codex_cc`` plus
  the hard-coded model/effort constants IMPORTED from the public package — no real
  ``codex`` is ever invoked, and the test does not literalize the model string.
* The ``ao`` engine is a fake shim (see conftest.write_fake_ao); the real
  @aoagents/ao engine is never required.
* The convergence consumer is orchestrator-gated: it requires
  ``AO_CALLER_TYPE=orchestrator`` plus ``AO_SESSION_ID`` matching the contract's
  ``continuation_policy.orchestrator_session``. conftest.cli_env STRIPS all AO_*
  vars, so the GREEN call passes them explicitly via cli(..., env_overrides).
* Nothing in this file is a committed absolute host path or private id — the
  fixture builds everything under tmp_path at runtime.
"""

from __future__ import annotations

import hashlib
import json

from conftest import _state_paths

from ao_state_writer.continuation import NON_EXECUTABLE_ACTIONS

# Import the model/effort constants from the PUBLIC package rather than hard-coding
# "gpt-5.5"/"xhigh" — the test must not depend on the specific model string, only on
# "whatever the public core currently demands". If the constant changes, the receipt
# stays valid automatically; that is the intent (stub the policy, don't pin it).
from ao_state_writer.writer import (
    CODEX_CC_MODEL,
    CODEX_CC_REASONING_EFFORT,
    FINAL_CONVERGENCE_OUTCOME,
    StateTransitionProposal,
)

# The three convergence/close consumers the L2 port added. They must all be present
# in --help and invokable — the structural half of the now-GREEN port.
CONVERGENCE_SUBCOMMANDS = (
    "record-final-convergence",
    "reconcile-spawn-attestation",
    "reconcile-spawned-dispatch",
)

# The orchestrator identity the convergence consumer requires. AO_SESSION_ID must equal
# the contract's continuation_policy.orchestrator_session (conftest.write_contract sets
# it to "example-orchestrator"); AO_CALLER_TYPE must be "orchestrator".
ORCHESTRATOR_ENV = {
    "AO_CALLER_TYPE": "orchestrator",
    "AO_SESSION_ID": "example-orchestrator",
}


def _read_state(project) -> dict:
    state_path, _ = _state_paths(project.root)
    return json.loads(state_path.read_text(encoding="utf-8"))

TARGET = "chapter-1"
EXHAUSTED_TARGET = "chapter-2"


def _drive_to_repair_attempts_exhausted(project, monkeypatch) -> None:
    """Close chapter-1, then drive chapter-2 to repair_attempts_exhausted (rev 0 -> 5).

    Leaves the project with exactly one remaining obligation: the unconsumed
    p-blocker-2 repair_attempts_exhausted dead-end.
    """

    writer = project.writer()
    revision = _seed_then_close_small_chapter(project, monkeypatch)
    assert revision == 3

    seed2 = writer.apply(
        _proposal(
            proposal_id="p-seed-2",
            target_id=EXHAUSTED_TARGET,
            base_state_revision=3,
            requested_state="evidence_pending",
        )
    )
    assert seed2.decision == "accepted", seed2
    assert seed2.next_required_action == "codex_cc_review"

    # A blocker must carry a real reviewer scope (unscoped blockers are refused); a CONTENT blocker
    # comes from a codex_cc review, and AO_CALLER_TYPE mirrors that caller.
    monkeypatch.setenv("AO_CALLER_TYPE", "codex_cc")
    blocker = writer.apply(
        _proposal(
            proposal_id="p-blocker-2",
            target_id=EXHAUSTED_TARGET,
            base_state_revision=4,
            requested_state="review_blocked",
            actor_role="codex_cc",
            review_scope="codex_cc",
            evidence_refs=["evidence#blocker"],
            verdict="blocker",
            blocker_code="hard_fail",
            model=CODEX_CC_MODEL,
            reasoning_effort=CODEX_CC_REASONING_EFFORT,
            non_retryable=True,
        )
    )
    monkeypatch.delenv("AO_CALLER_TYPE", raising=False)
    assert blocker.decision == "accepted", blocker
    assert blocker.new_state == "repair_attempts_exhausted", blocker
    assert blocker.next_required_action == "repair_attempts_exhausted", blocker


def _proposal(**overrides) -> StateTransitionProposal:
    """Build a proposal from the 6 required fields + sensible defaults.

    AO_CALLER_TYPE is deliberately NOT a field here: it is an ENVIRONMENT variable
    (compat.CALLER_TYPE_ENV) checked at receipt time, and placing it in the payload
    would raise TypeError in StateTransitionProposal(**payload).
    """

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


def _seed_then_close_small_chapter(project, monkeypatch) -> int:
    """Drive seed -> codex_cc receipt -> close for one small chapter in-process.

    Returns the state_revision after the close so the caller can continue ticking
    from the correct base_state_revision (each accepted apply increments it by 1,
    and base_state_revision must equal the current revision or apply rejects
    'stale_revision').
    """

    writer = project.writer()

    # STEP 1 — SEED. evidence_pending routes to next_required_action=codex_cc_review.
    seed = writer.apply(
        _proposal(proposal_id="p-seed-1", requested_state="evidence_pending")
    )
    assert seed.decision == "accepted", seed
    assert seed.new_state == "evidence_pending"
    assert seed.next_required_action == "codex_cc_review"
    assert seed.state_revision == 1

    # STEP 3 — codex_cc RECEIPT (STUBBED via env, no real codex). Routes the target
    # into closure_candidate with next_required_action=state_writer_closure.
    monkeypatch.setenv("AO_CALLER_TYPE", "codex_cc")
    transcript = project.root / "reports" / "codex-cc-receipts" / "p-cc-1.txt"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("codex-cc transcript ok", encoding="utf-8")
    transcript_sha = hashlib.sha256(transcript.read_bytes()).hexdigest()
    receipt = writer.apply(
        _proposal(
            proposal_id="p-cc-1",
            base_state_revision=1,
            requested_state="evidence_pending",
            actor_role="codex_cc",
            evidence_refs=["transcript#1"],
            review_scope="codex_cc",
            verdict="pass",
            model=CODEX_CC_MODEL,
            reasoning_effort=CODEX_CC_REASONING_EFFORT,
            codex_cc_transcript_sha256=transcript_sha,
            codex_cc_transcript_artifact_ref="artifact:reports/codex-cc-receipts/p-cc-1.txt",
        )
    )
    assert receipt.decision == "accepted", receipt
    assert receipt.new_state == "closure_candidate"
    assert receipt.next_required_action == "state_writer_closure"
    monkeypatch.delenv("AO_CALLER_TYPE", raising=False)

    # STEP 4 — CLOSE. Receipt-gated close yields dispatch_next_slice_plan_mode.
    close = writer.apply(
        _proposal(
            proposal_id="p-close-1",
            base_state_revision=2,
            requested_state="closed",
            evidence_refs=["evidence#close"],
        )
    )
    assert close.decision == "accepted", close
    assert close.new_state == "closed"
    assert close.next_required_action == "dispatch_next_slice_plan_mode"
    return close.state_revision


def test_step2_dispatch_spawns_worker_with_session_attestation(
    lifecycle_project, monkeypatch
):
    """STEP 2 (GREEN): ``continue`` execs the fake ``ao spawn`` and attests a session.

    This proves the spawn-attestation contract black-box: the public core runs
    ['ao','spawn','--prompt',...] and parses exactly one ``SESSION=<id>`` line. It
    is the dispatch half of the loop that already works today.
    """

    project = lifecycle_project
    writer = project.writer()

    # SEED so there is a unique ready candidate (codex_cc_review) for `continue`.
    seed = writer.apply(_proposal(proposal_id="p-seed-1"))
    assert seed.decision == "accepted"
    assert seed.next_required_action == "codex_cc_review"

    # --dry-run first: proves the command vector is the ao spawn contract. The CLI
    # surfaces the resolved command under the "cmd" key (not "would_run").
    dry = project.cli("continue", "--root", str(project.root), "--dry-run-continuation")
    assert dry.returncode == 0, dry.stderr
    dry_payload = json.loads(dry.stdout)
    assert dry_payload["result"] == "would_spawn", dry_payload
    assert dry_payload["cmd"][0:2] == ["ao", "spawn"], dry_payload

    # Real run: the fake `ao` on PATH prints SESSION=<id>; the core attests it.
    spawned = project.cli("continue", "--root", str(project.root))
    assert spawned.returncode == 0, spawned.stderr
    payload = json.loads(spawned.stdout)
    assert payload["result"] == "spawned", payload
    assert payload["spawn_session_id"] == "fake-sess-001", payload


def test_step1_through_4_small_chapter_cycle_closes_today(
    lifecycle_project, monkeypatch
):
    """STEP 1->4 (GREEN): one full small-chapter cycle closes in the public core.

    seed(evidence_pending) -> codex_cc receipt(pass, stubbed) -> close(closed).
    This is the baseline the convergence port must NOT regress: a single chapter
    can already be driven all the way to ``closed``.
    """

    revision = _seed_then_close_small_chapter(lifecycle_project, monkeypatch)
    # 3 accepted applies from state_revision 0 -> 3.
    assert revision == 3


def test_convergence_close_is_reachable(lifecycle_project, monkeypatch):
    """STEP 5->6 (GREEN): the project CONVERGES/CLOSES via record-final-convergence.

    After closing one chapter, drive a SECOND target to ``repair_attempts_exhausted``
    (a non_retryable blocker). That obligation is in NON_EXECUTABLE_ACTIONS, so before
    convergence ``continue``/``list-ready`` correctly fail-close at
    ``owner_proxy_convergence_required`` (the consumer does NOT auto-resolve; convergence
    is an explicit recorded decision). Then the new ``record-final-convergence`` consumer
    records the owner-proxy decision, and the loop reaches the converged/closed terminal:
    ``continue`` -> nothing_to_continue (exit 0), ``list-ready`` -> candidates=[] (exit 0).

    STRUCTURAL: all 3 convergence consumers are in --help and invokable (no longer an
    argparse 'invalid choice'). SEMANTIC: the convergence record (a) requires the live
    orchestrator caller+proof, and (b) flips the dead-end to a clean terminal.
    """

    project = lifecycle_project

    # First close one chapter, then drive chapter-2 to the exhaustion dead-end.
    _drive_to_repair_attempts_exhausted(project, monkeypatch)

    # PRE-CONVERGENCE dead-end PRESERVED: continue/list-ready still fail-close, no spawn,
    # canonical_write forbidden. The consumer does not auto-resolve — convergence is explicit.
    cont = project.cli("continue", "--root", str(project.root))
    assert cont.returncode == 3, (cont.returncode, cont.stdout, cont.stderr)
    cont_payload = json.loads(cont.stdout)
    assert cont_payload["result"] == "owner_proxy_convergence_required", cont_payload
    assert cont_payload["next_required_action"] == "repair_attempts_exhausted"
    assert "canonical_write" in cont_payload["forbidden_actions"], cont_payload
    assert "spawn_session_id" not in cont_payload, cont_payload

    listed = project.cli("list-ready", "--root", str(project.root))
    assert listed.returncode == 3, (listed.returncode, listed.stdout, listed.stderr)
    assert json.loads(listed.stdout)["result"] == "owner_proxy_convergence_required"

    # STRUCTURAL GREEN: the 3 convergence consumers now EXIST in --help and are invokable.
    help_proc = project.cli("--help")
    assert help_proc.returncode == 0, help_proc.stderr
    for subcommand in CONVERGENCE_SUBCOMMANDS:
        assert subcommand in help_proc.stdout, (subcommand, help_proc.stdout)
        # Invoking with a real --root is NOT an argparse 'invalid choice' anymore.
        invoked = project.cli(subcommand, "--root", str(project.root))
        assert "invalid choice" not in invoked.stderr, (subcommand, invoked.stderr)

    # STEP 6 — record the owner-proxy final convergence for the exhausted obligation.
    # Requires the live orchestrator identity (conftest.cli_env strips AO_*; pass it here).
    converged = project.cli(
        "record-final-convergence",
        "--root",
        str(project.root),
        "--proposal-id",
        "p-blocker-2",
        "--evidence",
        "owner-proxy:final-convergence",
        **ORCHESTRATOR_ENV,
    )
    assert converged.returncode == 0, (converged.returncode, converged.stdout, converged.stderr)
    converged_payload = json.loads(converged.stdout)
    assert converged_payload["result"] == "owner_proxy_final_convergence_recorded", converged_payload
    assert converged_payload["final_convergence"]["outcome"] == FINAL_CONVERGENCE_OUTCOME

    # SEMANTIC GREEN: the obligation is consumed -> continue reaches the converged terminal.
    cont2 = project.cli("continue", "--root", str(project.root))
    assert cont2.returncode == 0, (cont2.returncode, cont2.stdout, cont2.stderr)
    cont2_payload = json.loads(cont2.stdout)
    assert cont2_payload["result"] == "nothing_to_continue", cont2_payload

    # list-ready: clean ready terminal with zero remaining candidates.
    listed2 = project.cli("list-ready", "--root", str(project.root))
    assert listed2.returncode == 0, (listed2.returncode, listed2.stdout, listed2.stderr)
    listed2_payload = json.loads(listed2.stdout)
    assert listed2_payload["result"] == "ready", listed2_payload
    assert listed2_payload["candidates"] == [], listed2_payload


def test_convergence_record_requires_orchestrator_caller_and_proof(
    lifecycle_project, monkeypatch
):
    """Caller/proof fail-closed: record-final-convergence needs the live orchestrator identity.

    * No AO_CALLER_TYPE=orchestrator -> non_orchestrator_caller (exit 2).
    * orchestrator caller but a WRONG AO_SESSION_ID (not the contract's
      orchestrator_session) -> missing_or_stale_orchestrator_proof (exit 2).
    Neither consumes the obligation: it remains the owner_proxy_convergence_required dead-end.
    """

    project = lifecycle_project
    _drive_to_repair_attempts_exhausted(project, monkeypatch)

    # (a) missing orchestrator caller (conftest.cli_env strips AO_* by default).
    no_caller = project.cli(
        "record-final-convergence",
        "--root",
        str(project.root),
        "--proposal-id",
        "p-blocker-2",
        "--evidence",
        "owner-proxy:final-convergence",
    )
    assert no_caller.returncode == 2, (no_caller.stdout, no_caller.stderr)
    assert json.loads(no_caller.stdout)["reason"] == "non_orchestrator_caller"

    # (b) orchestrator caller but stale/mismatched session id.
    bad_proof = project.cli(
        "record-final-convergence",
        "--root",
        str(project.root),
        "--proposal-id",
        "p-blocker-2",
        "--evidence",
        "owner-proxy:final-convergence",
        AO_CALLER_TYPE="orchestrator",
        AO_SESSION_ID="not-the-orchestrator-session",
    )
    assert bad_proof.returncode == 2, (bad_proof.stdout, bad_proof.stderr)
    assert json.loads(bad_proof.stdout)["reason"] == "missing_or_stale_orchestrator_proof"

    # The dead-end is preserved — neither rejected call consumed the obligation.
    cont = project.cli("continue", "--root", str(project.root))
    assert cont.returncode == 3
    assert json.loads(cont.stdout)["result"] == "owner_proxy_convergence_required"


def test_convergence_record_rejects_wrong_obligation_and_unknown_proposal(
    lifecycle_project, monkeypatch
):
    """Obligation-type fail-closed.

    * record-final-convergence against an accepted proposal whose next_required_action
      is NOT repair_attempts_exhausted (here p-close-1, dispatch_next_slice_plan_mode) ->
      not_final_convergence_obligation.
    * against an unknown/unaccepted proposal_id -> unknown_or_unaccepted_proposal.
    """

    project = lifecycle_project
    _drive_to_repair_attempts_exhausted(project, monkeypatch)

    wrong_obligation = project.cli(
        "record-final-convergence",
        "--root",
        str(project.root),
        "--proposal-id",
        "p-close-1",  # accepted, but its action is dispatch_next_slice_plan_mode
        "--evidence",
        "owner-proxy:final-convergence",
        **ORCHESTRATOR_ENV,
    )
    assert wrong_obligation.returncode == 2, (wrong_obligation.stdout, wrong_obligation.stderr)
    assert json.loads(wrong_obligation.stdout)["reason"] == "not_final_convergence_obligation"

    unknown = project.cli(
        "record-final-convergence",
        "--root",
        str(project.root),
        "--proposal-id",
        "p-does-not-exist",
        "--evidence",
        "owner-proxy:final-convergence",
        **ORCHESTRATOR_ENV,
    )
    assert unknown.returncode == 2, (unknown.stdout, unknown.stderr)
    assert json.loads(unknown.stdout)["reason"] == "unknown_or_unaccepted_proposal"


def test_convergence_record_rejects_bad_outcome_and_is_idempotent(
    lifecycle_project, monkeypatch
):
    """Bad-outcome fail-closed at argparse AND a second record is an idempotent replay.

    * --outcome other than FINAL_CONVERGENCE_OUTCOME is rejected by argparse (exit 2).
    * A second record-final-convergence for the same proposal_id returns recorded with
      replayed=true and does NOT double-write the convergence record.
    """

    project = lifecycle_project
    _drive_to_repair_attempts_exhausted(project, monkeypatch)

    # argparse choices= rejects a bad --outcome before the writer is reached.
    bad_outcome = project.cli(
        "record-final-convergence",
        "--root",
        str(project.root),
        "--proposal-id",
        "p-blocker-2",
        "--evidence",
        "owner-proxy:final-convergence",
        "--outcome",
        "something-else",
        **ORCHESTRATOR_ENV,
    )
    assert bad_outcome.returncode == 2, (bad_outcome.stdout, bad_outcome.stderr)
    assert "invalid choice" in bad_outcome.stderr

    first = project.cli(
        "record-final-convergence",
        "--root",
        str(project.root),
        "--proposal-id",
        "p-blocker-2",
        "--evidence",
        "owner-proxy:final-convergence",
        **ORCHESTRATOR_ENV,
    )
    assert first.returncode == 0, (first.stdout, first.stderr)
    first_payload = json.loads(first.stdout)
    assert first_payload["result"] == "owner_proxy_final_convergence_recorded"
    assert "replayed" not in first_payload["final_convergence"]

    # Idempotent replay: same proposal_id, recorded again with replayed=true.
    second = project.cli(
        "record-final-convergence",
        "--root",
        str(project.root),
        "--proposal-id",
        "p-blocker-2",
        "--evidence",
        "owner-proxy:final-convergence",
        **ORCHESTRATOR_ENV,
    )
    assert second.returncode == 0, (second.stdout, second.stderr)
    second_payload = json.loads(second.stdout)
    assert second_payload["result"] == "owner_proxy_final_convergence_recorded"
    assert second_payload["replayed"] is True, second_payload


def _spawn_repair_active_lease(project, monkeypatch) -> str:
    """Drive p-seed-1 to a RECONCILABLE ``repair_active`` obligation (one retryable content blocker),
    then ``continue`` to spawn its dispatch lease. Returns the spawned lease's proposal_id.

    A reconcile-spawned-dispatch demonstration needs a lease whose projected action is IN
    ``SPAWNED_DISPATCH_RECONCILE_ACTIONS`` (the LIVE unified gate runs before the refresh/release split,
    so a fresh ``codex_cc_review`` lease is inert — see test_reconcile_spawned_dispatch_inert_for_review).
    ``repair_active`` is reconcilable, so refresh + not-overdue exercise the real normal-spawned path.
    """

    writer = project.writer()
    seed = writer.apply(_proposal(proposal_id="p-seed-1", requested_state="evidence_pending"))
    assert seed.decision == "accepted", seed
    assert seed.next_required_action == "codex_cc_review"

    # ONE retryable CONTENT blocker (non_retryable=False) -> repair_active (1 < MAX_REPAIR_ATTEMPTS),
    # never repair_attempts_exhausted. AO_CALLER_TYPE mirrors the codex_cc reviewer producing it.
    monkeypatch.setenv("AO_CALLER_TYPE", "codex_cc")
    blocker = writer.apply(
        _proposal(
            proposal_id="p-blocker-1",
            base_state_revision=1,
            requested_state="review_blocked",
            actor_role="codex_cc",
            review_scope="codex_cc",
            evidence_refs=["evidence#soft-blocker"],
            verdict="blocker",
            blocker_code="soft_fail",
            model=CODEX_CC_MODEL,
            reasoning_effort=CODEX_CC_REASONING_EFFORT,
            non_retryable=False,
        )
    )
    monkeypatch.delenv("AO_CALLER_TYPE", raising=False)
    assert blocker.decision == "accepted", blocker
    assert blocker.next_required_action == "repair_active", blocker

    spawned = project.cli("continue", "--root", str(project.root))
    assert spawned.returncode == 0, spawned.stderr
    assert json.loads(spawned.stdout)["result"] == "spawned"

    state = _read_state(project)
    leases = {pid: r for pid, r in state["dispatched_proposals"].items() if r.get("status") == "spawned"}
    assert len(leases) == 1, leases
    return next(iter(leases))


def test_reconcile_consumers_wired_but_inert(lifecycle_project, monkeypatch):
    """The two reconcile consumers are wired + public-state-scoped (the declaw lock).

    * reconcile-spawn-attestation against the public post-spawn record (status 'spawned',
      not 'spawned_unattested') -> not_spawned_unattested (exit 2).
    * reconcile-spawned-dispatch --refresh-time on a fresh RECONCILABLE 'spawned' record -> refreshed
      (exit 0); --release-if-terminal on a not-yet-overdue record -> spawned_dispatch_not_overdue.
    Proves they carry NONE of the spawn-baseline (mismatch/unverified/readback)
    reconciliation branches absent from the public state model.
    """

    project = lifecycle_project
    # A reconcilable (repair_active) lease so the refresh / not-overdue paths past the unified action
    # gate are exercised (the gate itself is locked separately for review leases below).
    lease_pid = _spawn_repair_active_lease(project, monkeypatch)

    # reconcile-spawn-attestation: the record is 'spawned' (attested), not 'spawned_unattested'.
    attest = project.cli(
        "reconcile-spawn-attestation",
        "--root",
        str(project.root),
        "--proposal-id",
        lease_pid,
        "--evidence",
        "orchestrator:attestation-check",
        "--release-if-absent",
        **ORCHESTRATOR_ENV,
    )
    assert attest.returncode == 2, (attest.stdout, attest.stderr)
    assert json.loads(attest.stdout)["reason"] == "not_spawned_unattested"

    # reconcile-spawned-dispatch --refresh-time on the fresh reconcilable 'spawned' lease: refreshed.
    refresh = project.cli(
        "reconcile-spawned-dispatch",
        "--root",
        str(project.root),
        "--proposal-id",
        lease_pid,
        "--evidence",
        "orchestrator:still-progressing",
        "--refresh-time",
        **ORCHESTRATOR_ENV,
    )
    assert refresh.returncode == 0, (refresh.stdout, refresh.stderr)
    assert json.loads(refresh.stdout)["result"] == "spawned_dispatch_time_refreshed"

    # --release-if-terminal on a not-yet-overdue lease: fail-closed (not stale enough).
    not_overdue = project.cli(
        "reconcile-spawned-dispatch",
        "--root",
        str(project.root),
        "--proposal-id",
        lease_pid,
        "--evidence",
        "orchestrator:overdue-check",
        "--release-if-terminal",
        **ORCHESTRATOR_ENV,
    )
    assert not_overdue.returncode == 2, (not_overdue.stdout, not_overdue.stderr)
    assert json.loads(not_overdue.stdout)["reason"] == "spawned_dispatch_not_overdue"


def test_reconcile_spawned_dispatch_inert_for_review_lease(lifecycle_project, monkeypatch):
    """reconcile-spawned-dispatch is INERT for a review/actuator lease (LIVE unified-gate model).

    A freshly-spawned obligation is ``codex_cc_review`` — an external review/actuator action that is NOT
    in SPAWNED_DISPATCH_RECONCILE_ACTIONS (the same class the pending-lease janitor spares, since
    releasing one could let a second actuator double-submit). The aligned LIVE model resolves + projects
    the action and gates it BEFORE the refresh/release split, so BOTH --refresh-time AND
    --release-if-terminal reject ``not_spawned_dispatch_reconcile_action`` (exit 2) regardless of age —
    age is never even reached. This locks the gate-ordering change introduced by the raw->projected
    alignment: previously --refresh-time bypassed the action gate and re-stamped a review lease.
    """

    project = lifecycle_project
    writer = project.writer()
    seed = writer.apply(_proposal(proposal_id="p-seed-1", requested_state="evidence_pending"))
    assert seed.decision == "accepted"
    assert seed.next_required_action == "codex_cc_review"
    spawned = project.cli("continue", "--root", str(project.root))
    assert spawned.returncode == 0, spawned.stderr
    assert json.loads(spawned.stdout)["result"] == "spawned"

    state = _read_state(project)
    leases = {pid: r for pid, r in state["dispatched_proposals"].items() if r.get("status") == "spawned"}
    assert len(leases) == 1, leases
    review_pid = next(iter(leases))
    assert state["proposal_results"][review_pid]["next_required_action"] == "codex_cc_review", state

    for op_flag in ("--refresh-time", "--release-if-terminal"):
        result = project.cli(
            "reconcile-spawned-dispatch",
            "--root",
            str(project.root),
            "--proposal-id",
            review_pid,
            "--evidence",
            "orchestrator:review-lease",
            op_flag,
            **ORCHESTRATOR_ENV,
        )
        assert result.returncode == 2, (op_flag, result.stdout, result.stderr)
        payload = json.loads(result.stdout)
        assert payload["reason"] == "not_spawned_dispatch_reconcile_action", (op_flag, payload)
        assert payload["next_required_action"] == "codex_cc_review", (op_flag, payload)

    # The review lease is intact (still 'spawned'), neither released nor re-stamped.
    after = _read_state(project)
    assert after["dispatched_proposals"][review_pid]["status"] == "spawned", after


def test_non_executable_actions_vocabulary_is_locked(lifecycle_project):
    """codex finding #3 lock: the convergence port introduced ZERO new action tokens.

    NON_EXECUTABLE_ACTIONS stays exactly ('repair_attempts_exhausted',). final_convergence_recorded
    is a dispatched_proposals STATUS (never matched by compat.validate_contract_compat) and
    repair_ladder_exhausted is an --outcome CLI choice, not a continuation action. So the contract's
    [continuation_policy] arrays stay in lock-step automatically and continue/apply never returns
    contract_action_vocabulary_mismatch. If someone "tidies up" by adding a status/outcome token into
    a continuation tuple, this assertion + the live apply below fail loudly.
    """

    assert NON_EXECUTABLE_ACTIONS == ("repair_attempts_exhausted",), NON_EXECUTABLE_ACTIONS

    # The contract arrays are derived from the live tuples (conftest._action_array), so a normal
    # apply through the CLI must NOT trip the vocabulary mismatch gate.
    project = lifecycle_project
    writer = project.writer()
    seed = writer.apply(_proposal(proposal_id="p-seed-1"))
    assert seed.decision == "accepted"
    listed = project.cli("list-ready", "--root", str(project.root))
    assert listed.returncode == 0, (listed.stdout, listed.stderr)
    assert json.loads(listed.stdout)["result"] != "contract_action_vocabulary_mismatch"
