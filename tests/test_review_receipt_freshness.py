"""Receipt-freshness regression suite (forward-port slice M1-S3): stale review receipts must
never overwrite a LIVE parked/blocked/closed obligation.

WHY THIS EXISTS (the spec, not just a check): review receipts arrive asynchronously (an external
escalated review or a codex_cc transcript lands whenever its reviewer finishes), so a receipt can
reference a gate/obligation the state machine has ALREADY moved past. Each fix in this suite pins
one way a stale receipt could rewind live state:

  * C-FIX-3/F1 — once a blocker consumed the escalated-review gate (target -> review_blocked), a
    LATER pass-family receipt reusing that gate id is stale: accepting it would un-block the
    target back to closure_candidate and suppress the live repair_active obligation.
  * C-FIX-5 — closed is terminal: a late receipt must not re-open a closed target.
  * C-FIX-6 — the parked states (repair_attempts_exhausted; the env-unavailable parking) must not
    be re-opened/downgraded by a stale receipt.
  * C-FIX-7 — a stale codex_cc receipt must not erase the env-unavailable parking either (the
    codex_cc sibling of the C-FIX-6 escalated-review case).

Each test drives a REAL gate lifecycle (package gate -> authorization -> receipt), then replays
the stale receipt and asserts both the typed rejection and the preserved obligation.
"""

from __future__ import annotations

import hashlib
import json

from test_public_core import _apply_package_gate, _write_contract, _writer

from ao_state_writer.writer import StateTransitionProposal


def _read_state(writer) -> dict:
    return json.loads(writer.state_path.read_text(encoding="utf-8"))


def _write_receipt_artifact(root, name: str) -> tuple[str, str]:
    """An escalated-review receipt artifact under reports/; returns (sha256, artifact ref)."""
    path = root / "reports" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"verdict": "advisory", "artifact": name}, sort_keys=True) + "\n"
    path.write_text(body, encoding="utf-8")
    return hashlib.sha256(body.encode("utf-8")).hexdigest(), f"artifact:reports/{name}"


def _authorize_gate(writer, monkeypatch, proposal_id: str = "gate-1") -> str:
    """Record the orchestrator authorization for the package gate; returns the submission nonce."""
    monkeypatch.setenv("AO_CALLER_TYPE", "orchestrator")
    monkeypatch.setenv("AO_SESSION_ID", "example-orchestrator")
    authorization = writer.record_authorization(
        proposal_id=proposal_id,
        evidence_refs=["owner-proxy:approved"],
        scope="escalated_review",
    )
    assert authorization["decision"] == "recorded", authorization
    return authorization["authorization"]["escalated_review_gate"]["external_review_submission_nonce"]


def _escalated_receipt(
    *, proposal_id: str, base_state_revision: int, verdict: str, nonce: str,
    receipt_sha: str, artifact_ref: str, package_sha256: str, blocker_code: str | None = None,
) -> StateTransitionProposal:
    return StateTransitionProposal(
        proposal_id=proposal_id,
        target_kind="major_chapter",
        target_id="chapter-1",
        base_state_revision=base_state_revision,
        requested_state="closure_candidate",
        actor_role="escalated_review",
        review_scope="escalated_review",
        verdict=verdict,
        blocker_code=blocker_code,
        evidence_refs=[artifact_ref],
        package_sha256=package_sha256,
        external_review_receipt_sha256=receipt_sha,
        external_review_submission_nonce=nonce,
        external_review_artifact_ref=artifact_ref,
        external_review_gate_proposal_id="gate-1",
    )


def _drive_small_close(tmp_path, writer, monkeypatch, target_id: str = "chapter-9") -> None:
    """Drive a small chapter to an accepted terminal `closed` (seed -> codex_cc pass -> close)."""
    from ao_state_writer.writer import CODEX_CC_MODEL, CODEX_CC_REASONING_EFFORT

    def rev() -> int:
        return _read_state(writer)["state_revision"] if writer.state_path.exists() else 0

    transcript = tmp_path / "reports" / "codex-cc-receipts" / f"{target_id}-close.txt"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("codex-cc transcript ok", encoding="utf-8")
    transcript_sha = hashlib.sha256(transcript.read_bytes()).hexdigest()
    ref = f"artifact:reports/codex-cc-receipts/{target_id}-close.txt"
    assert writer.apply(
        StateTransitionProposal(
            proposal_id=f"{target_id}-p1", target_kind="small_chapter", target_id=target_id,
            base_state_revision=rev(), requested_state="evidence_pending",
            actor_role="worker", evidence_refs=[f"session-log#{target_id}"],
        )
    ).decision == "accepted"
    monkeypatch.setenv("AO_CALLER_TYPE", "codex_cc")
    assert writer.apply(
        StateTransitionProposal(
            proposal_id=f"{target_id}-p2", target_kind="small_chapter", target_id=target_id,
            base_state_revision=rev(), requested_state="closure_candidate",
            actor_role="codex_cc", review_scope="codex_cc", verdict="advisory",
            model=CODEX_CC_MODEL, reasoning_effort=CODEX_CC_REASONING_EFFORT,
            evidence_refs=[f"cc-review#{target_id}", ref],
            codex_cc_transcript_sha256=transcript_sha,
            codex_cc_transcript_artifact_ref=ref,
        )
    ).decision == "accepted"
    monkeypatch.delenv("AO_CALLER_TYPE", raising=False)
    close = writer.apply(
        StateTransitionProposal(
            proposal_id=f"{target_id}-p3", target_kind="small_chapter", target_id=target_id,
            base_state_revision=rev(), requested_state="closed",
            actor_role="state_writer", evidence_refs=[f"session-log#{target_id}"],
        )
    )
    assert close.decision == "accepted", close
    assert _read_state(writer)["targets"][target_id]["state"] == "closed"


def test_late_codex_cc_receipt_cannot_reopen_closed_small_chapter(tmp_path, monkeypatch) -> None:
    """C-FIX-5/Finding 1: a small chapter that reached `closed` must stay closed. A
    stale/adversarial codex_cc BLOCKER arriving after closure would otherwise route through
    _accept's blocker branch back to review_blocked/repair_active (apply overwrites the closed
    state), dragging a done chapter into a phantom repair loop. (An advisory codex_cc receipt
    reopens it to closure_candidate via the same unguarded path — the verdict-agnostic guard
    blocks both; the blocker is the worst harm.)"""
    from ao_state_writer.writer import CODEX_CC_MODEL, CODEX_CC_REASONING_EFFORT

    _write_contract(tmp_path)
    writer = _writer(tmp_path)
    _drive_small_close(tmp_path, writer, monkeypatch)

    monkeypatch.setenv("AO_CALLER_TYPE", "codex_cc")
    late = writer.apply(
        StateTransitionProposal(
            proposal_id="cc-late-blocker", target_kind="small_chapter", target_id="chapter-9",
            base_state_revision=_read_state(writer)["state_revision"],
            requested_state="review_blocked", actor_role="codex_cc", review_scope="codex_cc",
            verdict="blocker", blocker_code="content_gap",
            model=CODEX_CC_MODEL, reasoning_effort=CODEX_CC_REASONING_EFFORT,
            evidence_refs=["cc-review#chapter-9-late"],
        )
    )
    assert late.decision == "rejected"
    assert late.reason == "target_already_closed"
    # closed is terminal: the late blocker did NOT reopen the chapter.
    assert _read_state(writer)["targets"]["chapter-9"]["state"] == "closed"


def test_late_escalated_blocker_cannot_reopen_closed_major_chapter(tmp_path, monkeypatch) -> None:
    """C-FIX-5/Finding 2: a major chapter that reached `closed` must stay closed. Once the gate is
    consumed the current-gate lookup returns None, and the authorization rejection rejects a None
    gate ONLY for pass-family verdicts — so a LATE blocker reusing the old gate id slips through
    to _accept's blocker branch and reopens the closed major to review_blocked/repair_active. The
    existing-target guard rejects it because the target is already closed."""
    _write_contract(tmp_path)
    gate = _apply_package_gate(tmp_path)
    writer = _writer(tmp_path)
    nonce = _authorize_gate(writer, monkeypatch)

    monkeypatch.setenv("AO_CALLER_TYPE", "escalated_review_actuator")
    pass_sha, pass_ref = _write_receipt_artifact(tmp_path, "escalated-review-receipts/pass.json")
    assert writer.apply(
        _escalated_receipt(
            proposal_id="er-pass", base_state_revision=1, verdict="advisory",
            nonce=nonce, receipt_sha=pass_sha, artifact_ref=pass_ref,
            package_sha256=gate.package_sha256,
        )
    ).decision == "accepted"
    assert _read_state(writer)["targets"]["chapter-1"]["state"] == "closure_candidate"
    monkeypatch.delenv("AO_CALLER_TYPE", raising=False)
    close = writer.apply(
        StateTransitionProposal(
            proposal_id="major-close", target_kind="major_chapter", target_id="chapter-1",
            base_state_revision=2, requested_state="closed",
            actor_role="state_writer", evidence_refs=["session-log#chapter-1-closed"],
        )
    )
    assert close.decision == "accepted", close
    assert _read_state(writer)["targets"]["chapter-1"]["state"] == "closed"

    # LATE escalated-review blocker reusing the consumed gate arrives after closure.
    monkeypatch.setenv("AO_CALLER_TYPE", "escalated_review_actuator")
    blk_sha, blk_ref = _write_receipt_artifact(tmp_path, "escalated-review-receipts/late-blocker.json")
    late = writer.apply(
        _escalated_receipt(
            proposal_id="er-late-blocker", base_state_revision=3, verdict="blocker",
            blocker_code="fresh_blocker", nonce=nonce, receipt_sha=blk_sha,
            artifact_ref=blk_ref, package_sha256=gate.package_sha256,
        )
    )
    assert late.decision == "rejected"
    # C-FIX-6 added a content-blocker stale-gate guard in the authorization rejection that fires
    # BEFORE the existing-target target_already_closed backstop for a consumed-gate escalated-review
    # blocker, so the reason is the more-specific stale_escalated_review_gate. The existing-target
    # guard remains the backstop (and still gives target_already_closed for the codex_cc-on-closed
    # case above). Either path rejects the blocker without reopening the major.
    assert late.reason == "stale_escalated_review_gate"
    # closed is terminal: the late blocker did NOT reopen the major.
    assert _read_state(writer)["targets"]["chapter-1"]["state"] == "closed"


def _drive_small_exhausted(tmp_path, writer, monkeypatch, target_id: str = "chapter-x") -> None:
    """Drive a small chapter to repair_attempts_exhausted purely by retryable CONTENT blockers
    overflowing MAX_REPAIR_ATTEMPTS_TOTAL (varied codes so no per-code cap trips first)."""
    from ao_state_writer.writer import (
        CODEX_CC_MODEL,
        CODEX_CC_REASONING_EFFORT,
        MAX_REPAIR_ATTEMPTS_TOTAL,
    )

    seed = writer.apply(
        StateTransitionProposal(
            proposal_id=f"{target_id}-seed", target_kind="small_chapter", target_id=target_id,
            base_state_revision=0, requested_state="evidence_pending",
            actor_role="worker", evidence_refs=[f"session-log#{target_id}"],
        )
    )
    assert seed.decision == "accepted", seed
    monkeypatch.setenv("AO_CALLER_TYPE", "codex_cc")
    last = None
    for index in range(MAX_REPAIR_ATTEMPTS_TOTAL + 1):
        # Real small-chapter repair workflow: each codex_cc round re-enters via a worker
        # evidence_pending re-open. The seed already left the target at evidence_pending for
        # round 0; rounds 1+ re-open from the prior round's review_blocked. The C-FIX-7
        # freshness guard requires this — a codex_cc receipt is only live at evidence_pending —
        # and repair_attempts/_total PERSIST across the re-open (the re-open carries no
        # verdict), so the bounded-repair budget still escalates at exactly the same blocker
        # count.
        if index > 0:
            reopen = writer.apply(
                StateTransitionProposal(
                    proposal_id=f"{target_id}-reopen-{index}",
                    target_kind="small_chapter", target_id=target_id,
                    base_state_revision=_read_state(writer)["state_revision"],
                    requested_state="evidence_pending", actor_role="worker",
                    evidence_refs=[f"session-log#{target_id}-reopen-{index}"],
                )
            )
            assert reopen.decision == "accepted", reopen
        last = writer.apply(
            StateTransitionProposal(
                proposal_id=f"{target_id}-blk-{index}",
                target_kind="small_chapter", target_id=target_id,
                base_state_revision=_read_state(writer)["state_revision"],
                requested_state="review_blocked", actor_role="codex_cc",
                review_scope="codex_cc", verdict="blocker",
                blocker_code=f"content_fail_{index}",
                model=CODEX_CC_MODEL, reasoning_effort=CODEX_CC_REASONING_EFFORT,
                evidence_refs=[f"evidence#{target_id}-blk-{index}"],
                non_retryable=False,
            )
        )
        assert last.decision == "accepted", last
    monkeypatch.delenv("AO_CALLER_TYPE", raising=False)
    assert last is not None and last.next_required_action == "repair_attempts_exhausted", last
    assert _read_state(writer)["targets"][target_id]["state"] == "repair_attempts_exhausted"


def test_late_codex_cc_receipt_cannot_reopen_exhausted_target(tmp_path, monkeypatch) -> None:
    """C-FIX-6/Finding 1: a target parked at repair_attempts_exhausted (its convergence obligation
    is owner-visible, advanced ONLY via the record-final-convergence CLI or a fresh worker
    re-open) must not be dragged back to closure_candidate by a stale codex_cc receipt — that
    silently erases the owner-visible final-convergence parking."""
    from ao_state_writer.writer import CODEX_CC_MODEL, CODEX_CC_REASONING_EFFORT

    _write_contract(tmp_path)
    writer = _writer(tmp_path)
    _drive_small_exhausted(tmp_path, writer, monkeypatch)

    transcript = tmp_path / "reports" / "codex-cc-receipts" / "late-on-exhausted.txt"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("codex-cc transcript ok", encoding="utf-8")
    transcript_sha = hashlib.sha256(transcript.read_bytes()).hexdigest()
    monkeypatch.setenv("AO_CALLER_TYPE", "codex_cc")
    late = writer.apply(
        StateTransitionProposal(
            proposal_id="cc-late-on-exhausted", target_kind="small_chapter", target_id="chapter-x",
            base_state_revision=_read_state(writer)["state_revision"],
            requested_state="closure_candidate", actor_role="codex_cc", review_scope="codex_cc",
            verdict="advisory", model=CODEX_CC_MODEL, reasoning_effort=CODEX_CC_REASONING_EFFORT,
            evidence_refs=["cc-review#late", "artifact:reports/codex-cc-receipts/late-on-exhausted.txt"],
            codex_cc_transcript_sha256=transcript_sha,
            codex_cc_transcript_artifact_ref="artifact:reports/codex-cc-receipts/late-on-exhausted.txt",
        )
    )
    assert late.decision == "rejected"
    assert late.reason == "target_already_repair_attempts_exhausted"
    # the owner-visible exhaustion parking is preserved, not silently reopened.
    assert _read_state(writer)["targets"]["chapter-x"]["state"] == "repair_attempts_exhausted"


def test_stale_escalated_blocker_cannot_downgrade_review_environment_unavailable(
    tmp_path, monkeypatch
) -> None:
    """C-FIX-6/Finding 2: once a major escalates to the owner-visible
    review_environment_unavailable obligation (new_state review_blocked), a stale
    escalated-review CONTENT blocker reusing the consumed gate must NOT be accepted — it would
    downgrade the environment-outage obligation to repair_active and make the orchestrator repair
    content while the review environment is actually down. Stale-gate rejection was pass-family
    only, so the blocker slips through without this guard."""
    _write_contract(tmp_path)
    gate = _apply_package_gate(tmp_path)
    writer = _writer(tmp_path)
    nonce = _authorize_gate(writer, monkeypatch)

    # accumulate env faults until the owner-visible review_environment_unavailable escalation.
    monkeypatch.setenv("AO_CALLER_TYPE", "watchdog")
    last_action = None
    for index in range(6):
        d = writer.apply(
            StateTransitionProposal(
                proposal_id=f"env-{index}", target_kind="major_chapter", target_id="chapter-1",
                base_state_revision=_read_state(writer)["state_revision"],
                requested_state="closure_candidate", actor_role="escalated_review",
                review_scope="escalated_review", verdict="blocker",
                review_mode="watchdog_timeout", blocker_code="escalated_review_hard_timeout",
                evidence_refs=[f"watchdog#{index}"],
            )
        )
        last_action = d.next_required_action
    assert last_action == "review_environment_unavailable"
    assert _read_state(writer)["targets"]["chapter-1"]["state"] == "review_blocked"

    # late stale escalated-review CONTENT blocker reusing the consumed gate.
    monkeypatch.setenv("AO_CALLER_TYPE", "escalated_review_actuator")
    blk_sha, blk_ref = _write_receipt_artifact(tmp_path, "escalated-review-receipts/stale-env-blocker.json")
    late = writer.apply(
        _escalated_receipt(
            proposal_id="er-stale-blocker-on-env",
            base_state_revision=_read_state(writer)["state_revision"],
            verdict="blocker", blocker_code="content_gap", nonce=nonce,
            receipt_sha=blk_sha, artifact_ref=blk_ref, package_sha256=gate.package_sha256,
        )
    )
    assert late.decision == "rejected"
    assert late.reason == "stale_escalated_review_gate"
    # the owner-visible environment-outage obligation is preserved (state unchanged, not
    # downgraded to a repair_active content-repair lane).
    assert _read_state(writer)["targets"]["chapter-1"]["state"] == "review_blocked"


def test_stale_codex_cc_receipt_cannot_downgrade_review_environment_unavailable(
    tmp_path, monkeypatch
) -> None:
    """C-FIX-7 (the codex_cc sibling of the C-FIX-6 escalated-review Finding 2): a SMALL chapter
    can escalate to the owner-visible review_environment_unavailable obligation (new_state
    review_blocked) via cumulative codex_cc watchdog env faults. codex_cc receipts have NO gate,
    so the escalated-review stale-gate guard does not apply, and the parked-state guard only keys
    on {closed, repair_attempts_exhausted}. A late/stale codex_cc PASS receipt (from an earlier
    timed-out round) must NOT be accepted — it would downgrade the env-outage obligation to
    closure_candidate and make the orchestrator proceed as if the chapter passed review. A genuine
    fresh recovery first re-opens the target to evidence_pending via a worker; a codex_cc receipt
    arriving while the target is parked at review_blocked is stale."""
    from ao_state_writer.writer import CODEX_CC_MODEL, CODEX_CC_REASONING_EFFORT

    _write_contract(tmp_path)
    writer = _writer(tmp_path)
    writer.apply(
        StateTransitionProposal(
            proposal_id="seed", target_kind="small_chapter", target_id="chapter-1",
            base_state_revision=0, requested_state="evidence_pending",
            actor_role="worker", evidence_refs=["session-log#chapter-1"],
        )
    )
    # accumulate codex_cc env faults until the owner-visible review_environment_unavailable escalation.
    monkeypatch.setenv("AO_CALLER_TYPE", "watchdog")
    last_action = None
    for index in range(6):
        d = writer.apply(
            StateTransitionProposal(
                proposal_id=f"cc-env-{index}", target_kind="small_chapter", target_id="chapter-1",
                base_state_revision=_read_state(writer)["state_revision"],
                requested_state="closure_candidate", actor_role="codex_cc",
                review_scope="codex_cc", verdict="blocker", review_mode="watchdog_timeout",
                blocker_code="codex_cc_review_timeout", evidence_refs=[f"watchdog#{index}"],
            )
        )
        last_action = d.next_required_action
    assert last_action == "review_environment_unavailable"
    assert _read_state(writer)["targets"]["chapter-1"]["state"] == "review_blocked"

    # late/stale codex_cc PASS receipt from an earlier timed-out round.
    transcript = tmp_path / "reports" / "codex-cc-receipts" / "stale-pass.txt"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("codex-cc transcript ok", encoding="utf-8")
    transcript_sha = hashlib.sha256(transcript.read_bytes()).hexdigest()
    monkeypatch.setenv("AO_CALLER_TYPE", "codex_cc")
    late = writer.apply(
        StateTransitionProposal(
            proposal_id="cc-stale-pass-on-env", target_kind="small_chapter", target_id="chapter-1",
            base_state_revision=_read_state(writer)["state_revision"],
            requested_state="closure_candidate", actor_role="codex_cc", review_scope="codex_cc",
            verdict="advisory", model=CODEX_CC_MODEL, reasoning_effort=CODEX_CC_REASONING_EFFORT,
            evidence_refs=["cc-review#stale", "artifact:reports/codex-cc-receipts/stale-pass.txt"],
            codex_cc_transcript_sha256=transcript_sha,
            codex_cc_transcript_artifact_ref="artifact:reports/codex-cc-receipts/stale-pass.txt",
        )
    )
    assert late.decision == "rejected"
    assert late.reason == "stale_review_receipt_for_inactive_round"
    # the owner-visible environment-outage obligation is preserved.
    assert _read_state(writer)["targets"]["chapter-1"]["state"] == "review_blocked"


def test_worker_evidence_pending_may_reopen_closed_chapter_for_new_round(tmp_path, monkeypatch) -> None:
    """Carve-out companion to the two guards above: `closed` is terminal for REVIEW receipts ONLY.
    A fresh WORKER evidence_pending proposal (review_scope "none", no verdict) is the designed
    obligation-supersession re-open and MUST still be accepted — otherwise the guard would freeze
    a chapter the master plan legitimately re-opens for another round. Guards the predicate
    against a future tighten-to-blanket regression: review_scope defaults to the STRING "none",
    not Python None, so the guard must test `!= "none"`, not `is not None`."""
    _write_contract(tmp_path)
    writer = _writer(tmp_path)
    _drive_small_close(tmp_path, writer, monkeypatch)

    reopen = writer.apply(
        StateTransitionProposal(
            proposal_id="reopen", target_kind="small_chapter", target_id="chapter-9",
            base_state_revision=_read_state(writer)["state_revision"],
            requested_state="evidence_pending", actor_role="worker",
            evidence_refs=["session-log#chapter-9-round2"],
        )
    )
    assert reopen.decision == "accepted"
    assert _read_state(writer)["targets"]["chapter-9"]["state"] == "evidence_pending"


def test_stale_escalated_pass_after_blocker_is_rejected(tmp_path, monkeypatch) -> None:
    """C-FIX-3/F1: once an escalated-review blocker moves a major to review_blocked, the gate is
    consumed and the current-gate lookup returns None. A LATER pass-family receipt reusing that
    gate id must be rejected as stale — otherwise it un-blocks the target back to
    closure_candidate and suppresses the repair_active obligation. Blockers (non-pass-family) are
    unaffected by this guard — a legitimate later blocker arrives with the gate already None."""
    _write_contract(tmp_path)
    gate = _apply_package_gate(tmp_path)
    writer = _writer(tmp_path)
    nonce = _authorize_gate(writer, monkeypatch)

    monkeypatch.setenv("AO_CALLER_TYPE", "escalated_review_actuator")
    blocker_sha, blocker_ref = _write_receipt_artifact(tmp_path, "escalated-review-receipts/blocker.json")
    blocked = writer.apply(
        _escalated_receipt(
            proposal_id="er-blocker", base_state_revision=1, verdict="blocker",
            blocker_code="fresh_blocker", nonce=nonce, receipt_sha=blocker_sha,
            artifact_ref=blocker_ref, package_sha256=gate.package_sha256,
        )
    )
    assert blocked.decision == "accepted", blocked
    assert _read_state(writer)["targets"]["chapter-1"]["state"] == "review_blocked"

    pass_sha, pass_ref = _write_receipt_artifact(tmp_path, "escalated-review-receipts/late-pass.json")
    decision = writer.apply(
        _escalated_receipt(
            proposal_id="er-late-pass", base_state_revision=2, verdict="advisory",
            nonce=nonce, receipt_sha=pass_sha, artifact_ref=pass_ref,
            package_sha256=gate.package_sha256,
        )
    )
    assert decision.decision == "rejected"
    assert decision.reason == "stale_escalated_review_gate"
    # the repair obligation is preserved: the late pass did NOT un-block the target
    assert _read_state(writer)["targets"]["chapter-1"]["state"] == "review_blocked"
