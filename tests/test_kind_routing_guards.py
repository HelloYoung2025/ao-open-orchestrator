"""C-FIX-2 regression suite (forward-port slice M1-S3): same-class kind/scope routing guards.

WHY THIS EXISTS (the spec, not just a check): these are the mirror SIBLINGS of the
``escalated_review_requires_major_chapter`` deadlock fix. ``_accept`` routes evidence_pending and
the codex_cc / escalated-review advisory-pass receipts to closure actions by requested_state /
review_scope ALONE — kind-agnostically — and ``apply`` then unconditionally overwrites
``target["kind"]`` from the proposal. Without the guards a mis-kinded proposal is accepted into
the WRONG review lane (and can silently corrupt an existing target's recorded kind), yielding a
closure action that can never be satisfied: a major routed to the small-chapter
state_writer_closure can never clear the major close's missing_escalated_review_receipt, and a
major mislabeled small bypasses escalated review entirely. Every mis-kinded routing must be
rejected at the source, BEFORE any state mutation — each test pins both the rejection reason and
the zero-mutation consequence, plus the matching healthy flow (the guards are surgical, not
blanket bans).
"""

from __future__ import annotations

import hashlib

from test_public_core import _apply_package_gate, _write_contract, _writer

from ao_state_writer.writer import (
    CODEX_CC_MODEL,
    CODEX_CC_REASONING_EFFORT,
    StateTransitionProposal,
)


def _read_targets(writer) -> dict:
    import json

    return json.loads(writer.state_path.read_text(encoding="utf-8")).get("targets", {})


def test_evidence_pending_rejected_for_non_small_chapter(tmp_path) -> None:
    """SIBLING(orig): evidence_pending is the SMALL-chapter codex_cc tier ONLY (major repairs
    submit escalated_review_pending, NOT evidence_pending). A major_chapter evidence_pending must
    be rejected at the source, not misrouted into codex_cc_review where the major could never
    close (the major close needs an escalated-review receipt)."""
    _write_contract(tmp_path)
    writer = _writer(tmp_path)
    decision = writer.apply(
        StateTransitionProposal(
            proposal_id="m1",
            target_kind="major_chapter",
            target_id="major-1",
            base_state_revision=0,
            requested_state="evidence_pending",
            actor_role="worker",
            evidence_refs=["session-log#major-1"],
        )
    )
    assert decision.decision == "rejected"
    assert decision.reason == "evidence_pending_requires_small_chapter"
    assert not writer.state_path.exists()  # a rejection persists nothing
    # healthy small-chapter evidence_pending still routes to codex_cc_review
    ok = writer.apply(
        StateTransitionProposal(
            proposal_id="s1",
            target_kind="small_chapter",
            target_id="chapter-1",
            base_state_revision=0,
            requested_state="evidence_pending",
            actor_role="worker",
            evidence_refs=["session-log#chapter-1"],
        )
    )
    assert ok.decision == "accepted"
    assert ok.next_required_action == "codex_cc_review"
    targets = _read_targets(writer)
    assert "major-1" not in targets  # rejected major never created
    assert "chapter-1" in targets


def test_codex_cc_advisory_receipt_rejected_for_major_chapter(tmp_path, monkeypatch) -> None:
    """SIBLING 5a: a codex_cc advisory-pass receipt routes to the small-chapter closure
    (state_writer_closure) for ANY kind. For a major_chapter that closure can never satisfy the
    major close (missing_escalated_review_receipt), and it bypasses escalated review. The
    kind-consistency guard cannot catch it (the receipt correctly claims major), so the scope
    guard must. Reject the mis-scoped receipt."""
    _write_contract(tmp_path)
    writer = _writer(tmp_path)
    transcript = tmp_path / "reports" / "codex-cc-receipts" / "major-misroute.txt"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("codex-cc transcript ok", encoding="utf-8")
    transcript_sha = hashlib.sha256(transcript.read_bytes()).hexdigest()
    monkeypatch.setenv("AO_CALLER_TYPE", "codex_cc")
    decision = writer.apply(
        StateTransitionProposal(
            proposal_id="p1",
            target_kind="major_chapter",
            target_id="major-1",
            base_state_revision=0,
            requested_state="closure_candidate",
            actor_role="codex_cc",
            review_scope="codex_cc",
            verdict="advisory",
            model=CODEX_CC_MODEL,
            reasoning_effort=CODEX_CC_REASONING_EFFORT,
            evidence_refs=["cc-review#major-1", "artifact:reports/codex-cc-receipts/major-misroute.txt"],
            codex_cc_transcript_sha256=transcript_sha,
            codex_cc_transcript_artifact_ref="artifact:reports/codex-cc-receipts/major-misroute.txt",
        )
    )
    assert decision.decision == "rejected"
    assert decision.reason == "codex_cc_receipt_requires_small_chapter"
    assert not writer.state_path.exists()  # rejected before any target creation/persist


def test_escalated_advisory_receipt_mislabeled_small_is_rejected_kind_mismatch(
    tmp_path, monkeypatch
) -> None:
    """SIBLING 5b: an escalated-review advisory-pass receipt that mislabels its target_kind as
    small_chapter (while referencing a real major gate) would otherwise be accepted, OVERWRITE the
    stored major kind to small (apply rewrites target['kind']), yet route to
    major_closure_candidate. The kind-consistency guard rejects the contradiction before any
    mutation; the major stays major."""
    _write_contract(tmp_path)
    gate = _apply_package_gate(tmp_path)
    writer = _writer(tmp_path)

    monkeypatch.setenv("AO_CALLER_TYPE", "orchestrator")
    monkeypatch.setenv("AO_SESSION_ID", "example-orchestrator")
    authorization = writer.record_authorization(
        proposal_id="gate-1",
        evidence_refs=["owner-proxy:approved"],
        scope="escalated_review",
    )
    assert authorization["decision"] == "recorded"
    nonce = authorization["authorization"]["escalated_review_gate"]["external_review_submission_nonce"]

    artifact = tmp_path / "reports" / "escalated-review-receipts" / "gate-1.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"verdict":"advisory","summary":"ok"}', encoding="utf-8")
    artifact_sha = hashlib.sha256(artifact.read_bytes()).hexdigest()

    monkeypatch.setenv("AO_CALLER_TYPE", "escalated_review_actuator")
    decision = writer.apply(
        StateTransitionProposal(
            proposal_id="g2",
            target_kind="small_chapter",  # the lie
            target_id="chapter-1",
            base_state_revision=1,
            requested_state="closure_candidate",
            actor_role="escalated_review",
            review_scope="escalated_review",
            verdict="advisory",
            evidence_refs=["artifact:reports/escalated-review-receipts/gate-1.txt"],
            package_sha256=gate.package_sha256,
            external_review_receipt_sha256=artifact_sha,
            external_review_submission_nonce=nonce,
            external_review_artifact_ref="artifact:reports/escalated-review-receipts/gate-1.txt",
            external_review_gate_proposal_id="gate-1",
        )
    )
    assert decision.decision == "rejected"
    assert decision.reason == "target_kind_mismatch"
    targets = _read_targets(writer)
    assert targets["chapter-1"]["kind"] == "major_chapter"  # NOT corrupted to small
    assert targets["chapter-1"]["state"] == "escalated_review_pending"


def test_codex_cc_blocker_rejected_for_major_chapter(tmp_path, monkeypatch) -> None:
    """C-FIX-3/F2: a codex_cc review is small-chapter only, VERDICT-AGNOSTICALLY. A codex_cc
    BLOCKER for a major (the verdict the advisory-only guard let slip) would otherwise route
    through _accept's blocker branch to review_blocked/repair_active, knocking the major out of
    its legitimate escalated_review_pending gate (its inferred live gate then disappears). Reject
    the mis-scoped receipt before mutation."""
    _write_contract(tmp_path)
    _apply_package_gate(tmp_path)
    writer = _writer(tmp_path)
    monkeypatch.setenv("AO_CALLER_TYPE", "codex_cc")
    decision = writer.apply(
        StateTransitionProposal(
            proposal_id="cc-blk",
            target_kind="major_chapter",
            target_id="chapter-1",
            base_state_revision=1,
            requested_state="review_blocked",
            actor_role="codex_cc",
            review_scope="codex_cc",
            verdict="blocker",
            blocker_code="content_gap",
            model=CODEX_CC_MODEL,
            reasoning_effort=CODEX_CC_REASONING_EFFORT,
            evidence_refs=["cc-review#chapter-1"],
        )
    )
    assert decision.decision == "rejected"
    assert decision.reason == "codex_cc_receipt_requires_small_chapter"
    targets = _read_targets(writer)
    # the major stays in its escalated-review gate; the codex_cc blocker did not knock it out
    assert targets["chapter-1"]["state"] == "escalated_review_pending"


def test_evidence_pending_small_on_stored_major_is_rejected_kind_mismatch(tmp_path) -> None:
    """SIBLING case-4: small_chapter+evidence_pending submitted for an EXISTING major target
    passes the evidence_pending=small guard (it DOES claim small) but would overwrite the stored
    major kind to small and misroute the major into codex_cc_review. The kind-consistency guard
    rejects the contradiction with the recorded kind before mutation."""
    _write_contract(tmp_path)
    _apply_package_gate(tmp_path)
    writer = _writer(tmp_path)
    decision = writer.apply(
        StateTransitionProposal(
            proposal_id="w2",
            target_kind="small_chapter",  # contradicts stored major
            target_id="chapter-1",
            base_state_revision=1,
            requested_state="evidence_pending",
            actor_role="worker",
            evidence_refs=["session-log#chapter-1b"],
        )
    )
    assert decision.decision == "rejected"
    assert decision.reason == "target_kind_mismatch"
    targets = _read_targets(writer)
    assert targets["chapter-1"]["kind"] == "major_chapter"  # NOT corrupted
    assert targets["chapter-1"]["state"] == "escalated_review_pending"
