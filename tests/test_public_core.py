from __future__ import annotations

from datetime import datetime, timezone
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


def test_pass_with_advisory_alias_is_normalized_to_advisory(tmp_path: Path, monkeypatch) -> None:
    # WHY: a reviewer may emit the non-canonical "pass_with_advisory". It matches neither the
    # advisory closure branch nor the blocker branch, so without normalization the obligation
    # records next_required_action=None and dispatch cannot route a null action -> the orchestrator
    # freezes. apply() must normalize the alias to "advisory" BEFORE routing so closure fires.
    _write_contract(tmp_path)
    monkeypatch.setenv("AO_CALLER_TYPE", "codex_cc")
    monkeypatch.setenv("AO_SESSION_ID", "example-orchestrator")
    decision = _writer(tmp_path).apply(
        StateTransitionProposal(
            proposal_id="codex-alias-1",
            target_kind="small_chapter",
            target_id="slice-1",
            base_state_revision=0,
            requested_state="closure_candidate",
            actor_role="codex_cc",
            evidence_refs=["transcript:codex"],
            review_scope="codex_cc",
            verdict="pass_with_advisory",
            model=CODEX_CC_MODEL,
            reasoning_effort=CODEX_CC_REASONING_EFFORT,
            codex_cc_transcript_sha256="a" * 64,
        )
    )

    assert decision.decision == "accepted"
    # Routes exactly as a canonical "advisory" codex_cc verdict would (state_writer_closure),
    # never None.
    assert decision.next_required_action == "state_writer_closure"


def _seed_lease_state(tmp_path: Path, dispatched: dict, proposal_results: dict | None = None) -> Path:
    state_path, _ = _state_paths(tmp_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "state_revision": 1,
                "targets": {},
                "proposal_results": proposal_results or {},
                "dispatched_proposals": dispatched,
            }
        ),
        encoding="utf-8",
    )
    return state_path


def test_reclaim_orphaned_pending_leases_deletes_only_expired_pending(tmp_path: Path) -> None:
    # WHY: an orchestrator SIGKILLed between claim and confirm orphans a 'pending' lease. The
    # janitor must delete the expired-pending orphan but SPARE (a) a fresh pending — a concurrent
    # claim may have just refreshed it (race guard) — and (b) a 'spawned' record (consume-once).
    _write_contract(tmp_path)
    old = "2000-01-01T00:00:00+0000"
    fresh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")
    state_path = _seed_lease_state(
        tmp_path,
        {
            "expired-pending": {"status": "pending", "time": old},
            "fresh-pending": {"status": "pending", "time": fresh},
            "spawned": {"status": "spawned", "time": old},
        },
    )
    writer = _writer(tmp_path)
    ids = ["expired-pending", "fresh-pending", "spawned"]

    dry = writer.reclaim_orphaned_pending_leases(ids, apply=False)
    assert dry["reclaimed"] == ["expired-pending"]
    # dry-run writes nothing
    assert set(json.loads(state_path.read_text())["dispatched_proposals"]) == set(ids)

    applied = writer.reclaim_orphaned_pending_leases(ids, apply=True)
    assert applied["reclaimed"] == ["expired-pending"]
    remaining = json.loads(state_path.read_text())["dispatched_proposals"]
    assert set(remaining) == {"fresh-pending", "spawned"}


def test_reconcile_leases_cli_spares_actuator_leases(tmp_path: Path, capsys) -> None:
    # WHY: an expired 'pending' lease whose action is an external review/actuator action
    # (gpt_pro_desktop_review) must be SPARED even though it is past the orphan floor — those leases
    # legitimately hold 'pending' far longer, and deleting one could let a second actuator run
    # double-submit. Only fast auto-spawn continuation orphans are reclaimed.
    from ao_state_writer.cli import main

    _write_contract(tmp_path)
    old = "2000-01-01T00:00:00+0000"

    def _result(action: str) -> dict:
        return {
            "decision": "accepted",
            "proposal_id": "x",
            "reason": "accepted",
            "state_revision": 1,
            "new_state": "s",
            "next_required_action": action,
            "blocker_code": None,
        }

    state_path = _seed_lease_state(
        tmp_path,
        {
            "fast": {"status": "pending", "time": old},
            "actuator": {"status": "pending", "time": old},
        },
        {
            "fast": _result("dispatch_next_slice_plan_mode"),
            "actuator": _result("gpt_pro_desktop_review"),
        },
    )

    rc = main(["reconcile-leases", "--root", str(tmp_path), "--apply"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["reclaimed"] == ["fast"]
    assert any(entry["proposal_id"] == "actuator" for entry in out["action_skipped"])
    assert set(json.loads(state_path.read_text())["dispatched_proposals"]) == {"actuator"}


def test_phantom_obligation_fails_closed_when_missing_from_existing_ledger(tmp_path: Path) -> None:
    # WHY: proposal_results is an audit history, not a queue. An accepted entry that is MISSING from
    # an EXISTING ledger is stale/corrupt; if it stayed "current" it would shadow healthy sibling
    # obligations at the global preflight chokepoint and freeze progress. It must fail CLOSED.
    # A legitimately-absent ledger FILE cannot disambiguate, so it stays current (back-compat).
    from ao_state_writer.cli import _is_current_actionable_obligation

    state = {"state_revision": 5, "targets": {}}
    stored = {
        "next_required_action": "state_writer_closure",
        "state_revision": 5,
        "new_state": "closure_candidate",
    }

    existing_ledger = tmp_path / "ledger.jsonl"
    existing_ledger.write_text(
        json.dumps({"proposal": {"proposal_id": "other", "target_id": "t"}}) + "\n",
        encoding="utf-8",
    )
    assert _is_current_actionable_obligation(state, "phantom", stored, existing_ledger) is False
    assert _is_current_actionable_obligation(state, "phantom", stored, tmp_path / "absent.jsonl") is True


def test_gpt_pro_receipt_guard_accepts_pass_with_advisory_raw_artifact(tmp_path: Path) -> None:
    # WHY: the GPT Pro desktop path is gated by an INDEPENDENT raw-artifact re-scan
    # (_gpt_pro_closure_receipt_guard). The writer normalizes pass_with_advisory -> advisory in the
    # ledger, but the raw artifact still carries the reviewer's literal token. The re-scan must
    # recognize the alias and normalize it before the pass-family membership test, or a legitimate
    # pass receipt wedges major closure — the half-ported failure mode the alias fix must avoid.
    from ao_state_writer.cli import _gpt_pro_closure_receipt_guard

    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "proposal": {
                    "proposal_id": "receipt-1",
                    "target_id": "chapter-1",
                    "verdict": "advisory",
                    "evidence_refs": ["artifact:reports/gpt-pro-receipts/receipt-1.txt"],
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    artifact = tmp_path / "reports" / "gpt-pro-receipts" / "receipt-1.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("verdict: pass_with_advisory\nsummary: ok\n", encoding="utf-8")

    state = {"targets": {"chapter-1": {"kind": "major_chapter", "state": "closure_candidate"}}}
    assert _gpt_pro_closure_receipt_guard(tmp_path, state, ledger, "receipt-1") is None
