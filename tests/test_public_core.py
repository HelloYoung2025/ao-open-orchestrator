from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os

from ao_state_writer.cli import preflight_reconcile
from ao_state_writer.continuation import ACTION_DIRECTIVES, _render_action_prompt
from ao_state_writer.writer import (
    CODEX_CC_MODEL,
    CODEX_CC_REASONING_EFFORT,
    StateTransitionDecision,
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
gated_actions = ["escalated_review"]
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
        requested_state="escalated_review_pending",
        actor_role="worker",
        evidence_refs=["package:review-package.zip"],
        package_path="review-package.zip",
        prompt_path="review-prompt.md",
        package_sha256=package_sha,
    )
    decision = _writer(root).apply(proposal)
    assert decision.decision == "accepted"
    assert decision.next_required_action == "escalated_review"
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


def test_escalated_review_receipt_requires_caller_package_nonce_and_artifact(
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
            proposal_id="receipt-1",
            target_kind="major_chapter",
            target_id="chapter-1",
            base_state_revision=1,
            requested_state="closure_candidate",
            actor_role="escalated_review",
            evidence_refs=["artifact:reports/escalated-review-receipts/gate-1.txt"],
            review_scope="escalated_review",
            verdict="advisory",
            package_sha256=gate.package_sha256,
            external_review_receipt_sha256=artifact_sha,
            external_review_submission_nonce=nonce,
            external_review_artifact_ref="artifact:reports/escalated-review-receipts/gate-1.txt",
            external_review_gate_proposal_id="gate-1",
        )
    )

    assert decision.decision == "accepted"
    assert decision.next_required_action == "major_closure_candidate"


def test_escalated_review_artifact_outside_reports_is_rejected(tmp_path: Path, monkeypatch) -> None:
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
    nonce = authorization["authorization"]["escalated_review_gate"]["external_review_submission_nonce"]

    outside = tmp_path / "outside.txt"
    outside.write_text('{"verdict":"advisory"}', encoding="utf-8")
    monkeypatch.setenv("AO_CALLER_TYPE", "escalated_review_actuator")
    decision = writer.apply(
        StateTransitionProposal(
            proposal_id="receipt-bad",
            target_kind="major_chapter",
            target_id="chapter-1",
            base_state_revision=1,
            requested_state="closure_candidate",
            actor_role="escalated_review",
            evidence_refs=["artifact:outside.txt"],
            review_scope="escalated_review",
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
    transcript = tmp_path / "reports" / "codex-cc-receipts" / "codex-alias-1.txt"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("codex-cc transcript ok", encoding="utf-8")
    transcript_sha = hashlib.sha256(transcript.read_bytes()).hexdigest()
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
            codex_cc_transcript_sha256=transcript_sha,
            codex_cc_transcript_artifact_ref="artifact:reports/codex-cc-receipts/codex-alias-1.txt",
        )
    )

    assert decision.decision == "accepted"
    # Routes exactly as a canonical "advisory" codex_cc verdict would (state_writer_closure),
    # never None.
    assert decision.next_required_action == "state_writer_closure"


def test_codex_cc_producer_directive_demands_transcript_artifact() -> None:
    # WHY: the writer now rejects a codex_cc pass-type receipt lacking codex_cc_transcript_artifact_ref
    # (missing_codex_cc_transcript_artifact). If the PRODUCER guidance the orchestrator dispatches to a
    # codex_cc reviewer still demanded "sha256 only", a real continuation-generated receipt would fail
    # that gate and freeze the loop -- a consumer/producer asymmetry stall. Guard BOTH producer surfaces
    # (the action directive AND the rendered scoped-review requirement) so no future edit can regress to
    # sha-only guidance while the writer requires the artifact ref.
    #
    # Site 1 -- the action directive -- is a module constant, so assert it directly (compaction-immune):
    assert "codex_cc_transcript_artifact_ref" in ACTION_DIRECTIVES["codex_cc_review"]
    assert "missing_codex_cc_transcript_artifact" in ACTION_DIRECTIVES["codex_cc_review"]

    # Site 2 -- the rendered "Codex cc scoped review requirement" bullet -- only materializes inside
    # _render_action_prompt, which COMPACTS any prompt over AO_PROMPT_SOFT_LIMIT and elides the middle.
    # The codex_cc_review prompt is always over the limit, and a LONG active_root grows the preserved
    # tail (it embeds `apply --root <active_root>`), shrinking the head budget until the scoped bullet's
    # rejection-code tail is elided. So we render with a FIXED, very short active_root to keep the
    # assertion deterministic (NOT tmp_path, whose machine-dependent length flips the result). Site 1
    # lives at the prompt head and survives compaction in production regardless of root length, so the
    # asymmetry is closed even when site 2 is elided -- but we still guard site 2 against a sha-only
    # regression here. Split at the header so the site-1 occurrence above cannot satisfy this check.
    decision = StateTransitionDecision(
        decision="accepted",
        proposal_id="p-cc-1",
        reason="evidence_pending",
        state_revision=1,
        new_state="evidence_pending",
        next_required_action="codex_cc_review",
    )
    prompt = _render_action_prompt(
        "codex_cc_review",
        current_state={
            "current_phase": "phase-x",
            "next_locked_action": "codex_cc_review",
            "review_gate_state": "evidence_pending",
            "latest_session_log_anchor": "anchor-1",
        },
        decision=decision,
        active_root=Path("/x"),
        orchestrator_session="example-orchestrator",
    )
    assert "Codex cc scoped review requirement:" in prompt
    scoped_block = prompt.split("Codex cc scoped review requirement:", 1)[1]
    assert "codex_cc_transcript_artifact_ref" in scoped_block
    assert "missing_codex_cc_transcript_artifact" in scoped_block


def test_unmodeled_requested_state_is_rejected_not_persisted_as_null_action(tmp_path: Path) -> None:
    # WHY: an UNMODELED (requested_state, verdict, review_scope, target_kind) tuple falls through every
    # _accept routing branch, so the decision would be accepted with next_required_action=None. Persisting
    # that creates an obligation the dispatch chokepoint reads as unsupported_current_obligation and
    # freezes the loop on orchestrator_vocab_review. apply() must fail CLOSED at the write path, before
    # any state mutation, instead of recording a null-action obligation.
    _write_contract(tmp_path)
    state_path, ledger_path = _state_paths(tmp_path)
    decision = _writer(tmp_path).apply(
        StateTransitionProposal(
            proposal_id="unmodeled-1",
            target_kind="small_chapter",
            target_id="slice-1",
            base_state_revision=0,
            # review_blocked is not blocker/advisory/closed/escalated_review_pending/evidence_pending, and
            # review_scope defaults to "none" + verdict to None, so no _accept branch sets an action.
            requested_state="review_blocked",
            actor_role="implementer",
            evidence_refs=["evidence:demo"],
        )
    )

    assert decision.decision == "rejected", decision
    assert decision.reason == "unsupported_requested_state_transition", decision
    # Fail-closed BEFORE any persist: a fresh project must have written neither state.json nor the ledger.
    assert decision.state_revision == 0, decision
    assert not state_path.exists(), "rejection must not persist a state.json"
    assert not ledger_path.exists(), "rejection must not append to the ledger"


def test_unscoped_blocker_verdict_is_rejected(tmp_path: Path) -> None:
    # WHY: a blocker with review_scope="none" skips the review-actor / model / caller auth gates yet
    # still manufactures repair attempts + a forged-scope review receipt. Only a genuine reviewer scope
    # (codex_cc/escalated_review) may emit a blocker; an unscoped blocker must fail closed.
    _write_contract(tmp_path)
    decision = _writer(tmp_path).apply(
        StateTransitionProposal(
            proposal_id="unscoped-blocker-1",
            target_kind="small_chapter",
            target_id="slice-1",
            base_state_revision=0,
            requested_state="review_blocked",
            actor_role="worker",
            evidence_refs=["evidence:demo"],
            verdict="blocker",
            blocker_code="content_fail",
        )
    )

    assert decision.decision == "rejected", decision
    assert decision.reason == "unscoped_blocker_verdict", decision


def test_unsupported_review_verdict_is_rejected(tmp_path: Path) -> None:
    # WHY: a review-scoped proposal carrying a verdict that is neither canonical nor a known alias would
    # fall through _accept to a null action and freeze the orchestrator; reject it at the _rejection door
    # with a precise reason instead of letting it reach the generic null-action guard.
    _write_contract(tmp_path)
    decision = _writer(tmp_path).apply(
        StateTransitionProposal(
            proposal_id="bad-verdict-1",
            target_kind="small_chapter",
            target_id="slice-1",
            base_state_revision=0,
            requested_state="review_blocked",
            actor_role="codex_cc",
            evidence_refs=["evidence:demo"],
            review_scope="codex_cc",
            verdict="definitely_not_a_verdict",
        )
    )

    assert decision.decision == "rejected", decision
    assert decision.reason == "unsupported_review_verdict", decision


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
    # (escalated_review) must be SPARED even though it is past the orphan floor — those leases
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
            "actuator": _result("escalated_review"),
        },
    )

    rc = main(["reconcile-leases", "--root", str(tmp_path), "--apply"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["reclaimed"] == ["fast"]
    assert any(entry["proposal_id"] == "actuator" for entry in out["action_skipped"])
    assert set(json.loads(state_path.read_text())["dispatched_proposals"]) == {"actuator"}


def test_phantom_obligation_fails_closed_when_missing_from_existing_ledger(tmp_path: Path) -> None:
    # WHY: proposal_results is an audit history, not a queue; this gate decides whether an accepted
    # entry is still the LIVE obligation. C-FIX-9 refines the missing-from-EXISTING-ledger case:
    # that on-disk shape is a crash-split orphan (a legitimate accepted obligation whose ledger
    # append was lost to a kill), NOT a phantom — so it is recovered as current when it
    # EXACTLY/UNAMBIGUOUSLY matches its target, and stays fail-closed only when it cannot be
    # disambiguated (ambiguous, superseded, or malformed), preserving the original phantom
    # suppression for the cases it was really guarding. A legitimately-absent ledger FILE cannot
    # disambiguate, so it stays current (back-compat, unchanged).
    from ao_state_writer.cli import _is_current_actionable_obligation

    state = {"state_revision": 5, "targets": {"t1": {"state": "closure_candidate"}}}
    stored = {
        "decision": "accepted",  # every proposal_results entry is an accepted decision
        "next_required_action": "state_writer_closure",
        "state_revision": 5,
        "new_state": "closure_candidate",
    }

    existing_ledger = tmp_path / "ledger.jsonl"
    existing_ledger.write_text(
        json.dumps({"proposal": {"proposal_id": "other", "target_id": "t1"}}) + "\n",
        encoding="utf-8",
    )
    # legitimate: ledger FILE absent -> current (cannot disambiguate yet).
    assert _is_current_actionable_obligation(state, "phantom", stored, tmp_path / "absent.jsonl") is True
    # C-FIX-9: a proposal MISSING from an EXISTING ledger is a crash-split orphan. With a UNIQUE
    # target in the stored new_state and the stored revision == the global revision, the
    # conservative legacy fallback (this `stored` has no persisted target_id) recovers it as the
    # live obligation.
    assert _is_current_actionable_obligation(state, "phantom", stored, existing_ledger) is True
    # ...but the conservative fallback stays FAIL-CLOSED when it cannot disambiguate:
    # (a) more than one target sits in the stored new_state (ambiguous which is the orphan).
    ambiguous_state = {
        "state_revision": 5,
        "targets": {
            "t1": {"state": "closure_candidate"},
            "t2": {"state": "closure_candidate"},
        },
    }
    assert _is_current_actionable_obligation(ambiguous_state, "phantom", stored, existing_ledger) is False
    # (b) the stored revision is behind the global revision (something applied AFTER the crash,
    #     so this blind orphan is no longer the latest write -> cannot be safely resurrected).
    superseded_state = {"state_revision": 9, "targets": {"t1": {"state": "closure_candidate"}}}
    assert _is_current_actionable_obligation(superseded_state, "phantom", stored, existing_ledger) is False
    # (c) no target sits in the stored new_state at all (the alleged obligation matches nothing).
    gone_state = {"state_revision": 5, "targets": {}}
    assert _is_current_actionable_obligation(gone_state, "phantom", stored, existing_ledger) is False


def test_escalated_review_receipt_guard_accepts_pass_with_advisory_raw_artifact(tmp_path: Path) -> None:
    # WHY: the escalated review path is gated by an INDEPENDENT raw-artifact re-scan
    # (_escalated_review_closure_receipt_guard). The writer normalizes pass_with_advisory -> advisory in the
    # ledger, but the raw artifact still carries the reviewer's literal token. The re-scan must
    # recognize the alias and normalize it before the pass-family membership test, or a legitimate
    # pass receipt wedges major closure — the half-ported failure mode the alias fix must avoid.
    from ao_state_writer.cli import _escalated_review_closure_receipt_guard

    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "proposal": {
                    "proposal_id": "receipt-1",
                    "target_id": "chapter-1",
                    "verdict": "advisory",
                    "evidence_refs": ["artifact:reports/escalated-review-receipts/receipt-1.txt"],
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    artifact = tmp_path / "reports" / "escalated-review-receipts" / "receipt-1.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("verdict: pass_with_advisory\nsummary: ok\n", encoding="utf-8")

    state = {"targets": {"chapter-1": {"kind": "major_chapter", "state": "closure_candidate"}}}
    assert _escalated_review_closure_receipt_guard(tmp_path, state, ledger, "receipt-1") is None
