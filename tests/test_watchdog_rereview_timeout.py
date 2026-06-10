"""C-FIX-4/F3 regression suite (forward-port slice M1-S3): a stale pass receipt must not blind
the watchdog to a stalled RE-review.

WHY THIS EXISTS (the spec, not just a check): ``review_receipts`` is append-only and carries no
per-round id. A pass-family receipt always moves the target OUT of its scope's active-review
state (codex_cc: evidence_pending; escalated_review: escalated_review_pending), so a pass can
never coexist with the same review still active. When the target RE-ENTERS its active-review
state for a new round, any matching pass on record is therefore from a PRIOR concluded round —
suppressing the timeout on its account is false quiescence exactly when the re-review has
stalled. The watchdog must suppress only once the review has concluded (target NOT in its
active-review state); a concluded target (closure_candidate / review_blocked / ...) still
suppresses a late/duplicate observation.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from test_public_core import _apply_package_gate, _write_contract, _writer
from test_review_receipt_freshness import _authorize_gate, _escalated_receipt, _write_receipt_artifact

from ao_state_writer.watchdog import ReviewWatchdogObservation, evaluate_watchdog
from ao_state_writer.writer import (
    CODEX_CC_MODEL,
    CODEX_CC_REASONING_EFFORT,
    StateTransitionProposal,
)


def _read_state(writer) -> dict:
    return json.loads(writer.state_path.read_text(encoding="utf-8"))


def test_watchdog_times_out_rereview_despite_stale_codex_cc_pass(tmp_path, monkeypatch) -> None:
    """A codex_cc PASS concludes review 1 (-> closure_candidate). If the target then RE-ENTERS
    evidence_pending for a NEW codex_cc round and that re-review stalls, the stale review-1 pass
    (append-only, no per-round id) must NOT suppress the timeout."""
    _write_contract(tmp_path)
    writer = _writer(tmp_path)
    writer.apply(
        StateTransitionProposal(
            proposal_id="p1",
            target_kind="small_chapter",
            target_id="chapter-1",
            base_state_revision=0,
            requested_state="evidence_pending",
            actor_role="worker",
            evidence_refs=["session-log#chapter-1"],
        )
    )
    transcript = tmp_path / "reports" / "codex-cc-receipts" / "r1.txt"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("codex-cc transcript ok", encoding="utf-8")
    transcript_sha = hashlib.sha256(transcript.read_bytes()).hexdigest()
    monkeypatch.setenv("AO_CALLER_TYPE", "codex_cc")
    writer.apply(
        StateTransitionProposal(
            proposal_id="p2",
            target_kind="small_chapter",
            target_id="chapter-1",
            base_state_revision=1,
            requested_state="closure_candidate",
            actor_role="codex_cc",
            review_scope="codex_cc",
            verdict="advisory",
            model=CODEX_CC_MODEL,
            reasoning_effort=CODEX_CC_REASONING_EFFORT,
            evidence_refs=["cc-review#chapter-1", "artifact:reports/codex-cc-receipts/r1.txt"],
            codex_cc_transcript_sha256=transcript_sha,
            codex_cc_transcript_artifact_ref="artifact:reports/codex-cc-receipts/r1.txt",
        )
    )
    monkeypatch.delenv("AO_CALLER_TYPE", raising=False)
    # review 2: the target re-enters evidence_pending (a NEW codex_cc review round)
    reentry = writer.apply(
        StateTransitionProposal(
            proposal_id="p3",
            target_kind="small_chapter",
            target_id="chapter-1",
            base_state_revision=2,
            requested_state="evidence_pending",
            actor_role="worker",
            evidence_refs=["session-log#chapter-1-round2"],
        )
    )
    assert reentry.decision == "accepted", reentry
    assert _read_state(writer)["targets"]["chapter-1"]["state"] == "evidence_pending"
    decision = evaluate_watchdog(
        root=tmp_path,
        observation=ReviewWatchdogObservation(
            target_id="chapter-1",
            review_scope="codex_cc",
            started_at="2026-05-28T12:00:00Z",
            last_activity_at="2026-05-28T12:00:00Z",
            target_kind="small_chapter",
            evidence_refs=["watchdog#chapter-1-round2"],
        ),
        now=datetime(2026, 5, 28, 13, 0, tzinfo=timezone.utc),
    )
    assert decision.decision == "timeout_candidate"
    assert decision.blocker_code == "codex_cc_review_timeout"
    assert decision.proposal is not None


def test_watchdog_times_out_escalated_rereview_despite_same_package_stale_pass(
    tmp_path, monkeypatch
) -> None:
    """Escalated-review lane: the package-sha cannot distinguish a stale pass from a SAME-package
    re-review. After an escalated-review pass (-> closure_candidate) the target re-gates to
    escalated_review_pending with the same package; a stalled re-review must still time out
    (target is back in its active-review state), not be suppressed by the matching stale pass."""
    _write_contract(tmp_path)
    gate = _apply_package_gate(tmp_path)
    writer = _writer(tmp_path)
    nonce1 = _authorize_gate(writer, monkeypatch, "gate-1")

    monkeypatch.setenv("AO_CALLER_TYPE", "escalated_review_actuator")
    pass_sha, pass_ref = _write_receipt_artifact(tmp_path, "escalated-review-receipts/pass1.json")
    pass_decision = writer.apply(
        _escalated_receipt(
            proposal_id="er-pass-1", base_state_revision=1, verdict="advisory",
            nonce=nonce1, receipt_sha=pass_sha, artifact_ref=pass_ref,
            package_sha256=gate.package_sha256,
        )
    )
    assert pass_decision.decision == "accepted", pass_decision  # the pass must actually record a receipt
    assert _read_state(writer)["targets"]["chapter-1"]["state"] == "closure_candidate"

    # re-gate to escalated_review_pending with the SAME package -> a new active review round
    monkeypatch.delenv("AO_CALLER_TYPE", raising=False)
    regate = writer.apply(
        StateTransitionProposal(
            proposal_id="gate-2",
            target_kind="major_chapter",
            target_id="chapter-1",
            base_state_revision=2,
            requested_state="escalated_review_pending",
            actor_role="worker",
            evidence_refs=["package:review-package.zip"],
            package_path="review-package.zip",
            prompt_path="review-prompt.md",
            package_sha256=gate.package_sha256,
        )
    )
    assert regate.decision == "accepted", regate
    assert _read_state(writer)["targets"]["chapter-1"]["state"] == "escalated_review_pending"
    decision = evaluate_watchdog(
        root=tmp_path,
        observation=ReviewWatchdogObservation(
            target_id="chapter-1",
            review_scope="escalated_review",
            started_at="2026-05-28T10:00:00Z",
            last_activity_at="2026-05-28T10:00:00Z",
            target_kind="major_chapter",
            evidence_refs=["watchdog#chapter-1-round2"],
        ),
        now=datetime(2026, 5, 28, 13, 0, tzinfo=timezone.utc),  # ~180 min > hard timeout
    )
    assert decision.decision == "timeout_candidate"
    assert decision.proposal is not None
