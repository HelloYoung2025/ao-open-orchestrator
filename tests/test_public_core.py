from __future__ import annotations

from pathlib import Path
import hashlib
import json
import os

from ao_state_writer.cli import preflight_reconcile
from ao_state_writer.writer import (
    CODEX_CC_MODEL,
    CODEX_CC_REASONING_EFFORT,
    StateTransitionProposal,
    StateWriter,
)


def _state_paths(root: Path) -> tuple[Path, Path]:
    base = root / ".omx" / "state" / "ao-state-writer"
    return base / "state.json", base / "state-transitions.jsonl"


def _writer(root: Path) -> StateWriter:
    state_path, ledger_path = _state_paths(root)
    return StateWriter(state_path=state_path, ledger_path=ledger_path)


def _write_contract(root: Path) -> None:
    root.joinpath("DIRECT_PROJECT_CONTRACT.toml").write_text(
        f'''
version = 1

[ao_clone_isolation]
active_root = "{root.as_posix()}"

[owner_proxy]
project_id = "example-project"

[continuation_policy]
orchestrator_session = "example-orchestrator"
auto_spawn_actions = [
  "dispatch_next_slice_plan_mode",
  "codex_cc_review",
  "repair_active",
  "state_writer_closure",
  "major_closure_candidate",
]
gated_actions = ["gpt_pro_desktop_review"]
non_executable_actions = ["repair_attempts_exhausted"]
'''.lstrip(),
        encoding="utf-8",
    )


def _apply_package_gate(root: Path) -> StateTransitionProposal:
    package = root / "review-package.zip"
    prompt = root / "review-prompt.md"
    package.write_bytes(b"package")
    prompt.write_text("review prompt", encoding="utf-8")
    package_sha = hashlib.sha256(package.read_bytes()).hexdigest()
    proposal = StateTransitionProposal(
        proposal_id="gate-1",
        target_kind="major_chapter",
        target_id="chapter-1",
        base_state_revision=0,
        requested_state="gpt_pro_review_pending",
        actor_role="worker",
        evidence_refs=["package:review-package.zip"],
        package_path="review-package.zip",
        prompt_path="review-prompt.md",
        package_sha256=package_sha,
    )
    decision = _writer(root).apply(proposal)
    assert decision.decision == "accepted"
    assert decision.next_required_action == "gpt_pro_desktop_review"
    return proposal


def test_legacy_schema_is_backfilled_on_next_write(tmp_path: Path) -> None:
    _write_contract(tmp_path)
    state_path, _ = _state_paths(tmp_path)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({"state_revision": 0, "targets": {}, "proposal_results": {}}),
        encoding="utf-8",
    )

    decision = _writer(tmp_path).apply(
        StateTransitionProposal(
            proposal_id="p1",
            target_kind="small_chapter",
            target_id="slice-1",
            base_state_revision=0,
            requested_state="evidence_pending",
            actor_role="worker",
            evidence_refs=["evidence:demo"],
        )
    )

    assert decision.decision == "accepted"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1


def test_preflight_reports_unsupported_current_obligation(tmp_path: Path) -> None:
    _write_contract(tmp_path)
    state_path, ledger_path = _state_paths(tmp_path)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "state_revision": 1,
                "targets": {"slice-1": {"kind": "small_chapter", "state": "custom_state"}},
                "proposal_results": {
                    "p1": {
                        "decision": "accepted",
                        "proposal_id": "p1",
                        "reason": "accepted",
                        "state_revision": 1,
                        "new_state": "custom_state",
                        "next_required_action": "new_business_action",
                        "blocker_code": None,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    ledger_path.write_text(
        json.dumps({"proposal": {"proposal_id": "p1", "target_id": "slice-1"}}) + "\n",
        encoding="utf-8",
    )

    result = preflight_reconcile(root=tmp_path, writer=_writer(tmp_path), ledger_path=ledger_path)

    assert result is not None
    payload, exit_code = result
    assert exit_code == 3
    assert payload["result"] == "unsupported_current_obligation"
    assert payload["allowed_repair_actions"] == ["orchestrator_vocab_review"]


def test_codex_cc_receipt_requires_trusted_caller(tmp_path: Path) -> None:
    _write_contract(tmp_path)
    decision = _writer(tmp_path).apply(
        StateTransitionProposal(
            proposal_id="codex-1",
            target_kind="small_chapter",
            target_id="slice-1",
            base_state_revision=0,
            requested_state="closure_candidate",
            actor_role="codex_cc",
            evidence_refs=["transcript:codex"],
            review_scope="codex_cc",
            verdict="advisory",
            model=CODEX_CC_MODEL,
            reasoning_effort=CODEX_CC_REASONING_EFFORT,
            codex_cc_transcript_sha256="a" * 64,
        )
    )

    assert decision.decision == "rejected"
    assert decision.reason == "unauthorized_review_receipt_actor"


def test_gpt_pro_receipt_requires_caller_package_nonce_and_artifact(
    tmp_path: Path, monkeypatch
) -> None:
    _write_contract(tmp_path)
    gate = _apply_package_gate(tmp_path)
    writer = _writer(tmp_path)

    monkeypatch.setenv("AO_CALLER_TYPE", "orchestrator")
    monkeypatch.setenv("AO_SESSION_ID", "example-orchestrator")
    authorization = writer.record_authorization(
        proposal_id="gate-1",
        evidence_refs=["owner-proxy:approved"],
        scope="gpt_pro_desktop_review",
    )
    assert authorization["decision"] == "recorded"
    nonce = authorization["authorization"]["gpt_pro_review_gate"]["external_review_submission_nonce"]

    artifact = tmp_path / "reports" / "gpt-pro-receipts" / "gate-1.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"verdict":"advisory","summary":"ok"}', encoding="utf-8")
    artifact_sha = hashlib.sha256(artifact.read_bytes()).hexdigest()

    monkeypatch.setenv("AO_CALLER_TYPE", "gpt_pro_review_actuator")
    decision = writer.apply(
        StateTransitionProposal(
            proposal_id="receipt-1",
            target_kind="major_chapter",
            target_id="chapter-1",
            base_state_revision=1,
            requested_state="closure_candidate",
            actor_role="gpt_pro",
            evidence_refs=["artifact:reports/gpt-pro-receipts/gate-1.txt"],
            review_scope="gpt_pro",
            verdict="advisory",
            package_sha256=gate.package_sha256,
            external_review_receipt_sha256=artifact_sha,
            external_review_submission_nonce=nonce,
            external_review_artifact_ref="artifact:reports/gpt-pro-receipts/gate-1.txt",
            external_review_gate_proposal_id="gate-1",
        )
    )

    assert decision.decision == "accepted"
    assert decision.next_required_action == "major_closure_candidate"


def test_gpt_pro_artifact_outside_reports_is_rejected(tmp_path: Path, monkeypatch) -> None:
    _write_contract(tmp_path)
    gate = _apply_package_gate(tmp_path)
    writer = _writer(tmp_path)

    monkeypatch.setenv("AO_CALLER_TYPE", "orchestrator")
    monkeypatch.setenv("AO_SESSION_ID", "example-orchestrator")
    authorization = writer.record_authorization(
        proposal_id="gate-1",
        evidence_refs=["owner-proxy:approved"],
        scope="gpt_pro_desktop_review",
    )
    nonce = authorization["authorization"]["gpt_pro_review_gate"]["external_review_submission_nonce"]

    outside = tmp_path / "outside.txt"
    outside.write_text('{"verdict":"advisory"}', encoding="utf-8")
    monkeypatch.setenv("AO_CALLER_TYPE", "gpt_pro_review_actuator")
    decision = writer.apply(
        StateTransitionProposal(
            proposal_id="receipt-bad",
            target_kind="major_chapter",
            target_id="chapter-1",
            base_state_revision=1,
            requested_state="closure_candidate",
            actor_role="gpt_pro",
            evidence_refs=["artifact:outside.txt"],
            review_scope="gpt_pro",
            verdict="advisory",
            package_sha256=gate.package_sha256,
            external_review_receipt_sha256=hashlib.sha256(outside.read_bytes()).hexdigest(),
            external_review_submission_nonce=nonce,
            external_review_artifact_ref="artifact:outside.txt",
            external_review_gate_proposal_id="gate-1",
        )
    )

    assert decision.decision == "rejected"
    assert decision.reason == "invalid_external_review_artifact_ref"
