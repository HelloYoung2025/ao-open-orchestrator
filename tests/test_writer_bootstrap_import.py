"""Bootstrap-import lane (`historical_closed`) contract suite.

WHY THIS EXISTS: the first slice of a run must enter the state machine, or every
worker is spawned with an ad-hoc prompt that never carries the apply 工序 (observed
live 2026-06-12: a worker finished a whole slice with EMPTY canonical state and the
loop stalled at every handoff). Normal `closed` is receipt-gated by design — it must
NEVER become a forgery path for unreviewed work. `historical_closed` exists solely to
record an ALREADY-completed slice as a historical fact on an idle machine, seeding the
same dispatch_next_slice_plan_mode obligation a receipted close would. These tests pin
both sides: the import lane works under its gates, and every gate fails closed.
"""

from __future__ import annotations

import json

import pytest

from test_public_core import _write_contract, _writer

from ao_state_writer.writer import StateTransitionProposal


@pytest.fixture()
def orch_env(tmp_path, monkeypatch):
    """The import lane is orchestrator-caller-only (same AO_CALLER_TYPE + contract-proof
    standard as record_authorization): a real contract in the root plus the AO-injected
    env pair. Tests that exercise the lane's OTHER gates run under this identity."""
    _write_contract(tmp_path)
    monkeypatch.setenv("AO_CALLER_TYPE", "orchestrator")
    monkeypatch.setenv("AO_SESSION_ID", "example-orchestrator")


def _import_proposal(**kw) -> StateTransitionProposal:
    base = dict(
        proposal_id="boot-1",
        target_kind="small_chapter",
        target_id="c0",
        base_state_revision=0,
        requested_state="historical_closed",
        actor_role="orchestrator",
        evidence_refs=["PLAN_TODO#current-execution-state", "PR#41"],
    )
    base.update(kw)
    return StateTransitionProposal(**base)


def _read_state(writer) -> dict:
    return json.loads(writer.state_path.read_text(encoding="utf-8"))


def test_import_accepted_on_fresh_state_seeds_dispatch(tmp_path, orch_env) -> None:
    writer = _writer(tmp_path)
    decision = writer.apply(_import_proposal())
    assert decision.decision == "accepted"
    assert decision.new_state == "closed"
    assert decision.next_required_action == "dispatch_next_slice_plan_mode"
    target = _read_state(writer)["targets"]["c0"]
    assert target["state"] == "closed"
    # An import records a fact, never a receipt: the receipt chain stays empty.
    assert target.get("review_receipts", []) == []


def test_import_rejected_when_carrying_review_fields(tmp_path, orch_env) -> None:
    writer = _writer(tmp_path)
    # A verdict smuggled onto an import hits the lane's own gate.
    d1 = writer.apply(_import_proposal(verdict="pass"))
    assert d1.decision == "rejected"
    assert d1.reason == "historical_closed_carries_review_fields"
    # A review_scope smuggled on hits the upstream scope/actor gate first
    # (defense in depth — the lane gate still backstops actor-matched scopes).
    d2 = writer.apply(_import_proposal(proposal_id="boot-2", review_scope="codex_cc"))
    assert d2.decision == "rejected"
    assert d2.reason == "invalid_review_actor"


def test_import_rejected_without_evidence(tmp_path, orch_env) -> None:
    writer = _writer(tmp_path)
    # None/[] are caught by the generic upstream evidence gate; blank-only strings
    # would slip through it, so the lane's own gate catches those.
    for pid, refs, reason in (
        ("e1", None, "missing_evidence"),
        ("e2", [], "missing_evidence"),
        ("e3", ["  "], "historical_closed_requires_evidence"),
    ):
        d = writer.apply(_import_proposal(proposal_id=pid, evidence_refs=refs))
        assert d.decision == "rejected"
        assert d.reason == reason


def test_import_rejected_when_target_exists(tmp_path, orch_env) -> None:
    writer = _writer(tmp_path)
    assert writer.apply(_import_proposal()).decision == "accepted"
    d = writer.apply(_import_proposal(proposal_id="boot-dup", base_state_revision=1))
    assert d.decision == "rejected"
    assert d.reason == "historical_closed_target_exists"


def test_import_rejected_while_machine_mid_flight(tmp_path, orch_env) -> None:
    writer = _writer(tmp_path)
    live = writer.apply(
        StateTransitionProposal(
            proposal_id="work-1",
            target_kind="small_chapter",
            target_id="c2",
            base_state_revision=0,
            requested_state="evidence_pending",
            actor_role="worker",
            evidence_refs=["session-log#c2"],
        )
    )
    assert live.decision == "accepted"
    d = writer.apply(_import_proposal(target_id="c0", base_state_revision=1))
    assert d.decision == "rejected"
    assert d.reason == "historical_closed_requires_idle_machine"


def test_sequential_imports_allowed_while_all_closed(tmp_path, orch_env) -> None:
    # Importing several truly-completed slices one after another (e.g. c0 then c2)
    # must work: closed targets do not count as mid-flight.
    writer = _writer(tmp_path)
    assert writer.apply(_import_proposal()).decision == "accepted"
    d = writer.apply(
        _import_proposal(proposal_id="boot-2", target_id="c2", base_state_revision=1)
    )
    assert d.decision == "accepted"
    assert d.next_required_action == "dispatch_next_slice_plan_mode"


def test_import_rejected_for_unmodeled_kind(tmp_path, orch_env) -> None:
    writer = _writer(tmp_path)
    d = writer.apply(_import_proposal(target_kind="epic"))
    assert d.decision == "rejected"
    assert d.reason == "historical_closed_kind_invalid"


def test_normal_closed_still_receipt_gated(tmp_path) -> None:
    # Regression pin: the import lane must NOT loosen the real closure gate.
    writer = _writer(tmp_path)
    d = writer.apply(
        StateTransitionProposal(
            proposal_id="sneak-close",
            target_kind="small_chapter",
            target_id="c9",
            base_state_revision=0,
            requested_state="closed",
            actor_role="worker",
            evidence_refs=["session-log#c9"],
        )
    )
    assert d.decision == "rejected"
    assert d.reason == "missing_codex_cc_receipt"


def test_import_rejected_for_non_orchestrator_caller(tmp_path, monkeypatch) -> None:
    # actor_role is self-asserted and proves nothing — only the AO-injected caller
    # identity counts. A worker (or a bare shell with no AO env) must never mint a
    # closed chapter + dispatch obligation (cross-review HIGH).
    _write_contract(tmp_path)
    writer = _writer(tmp_path)
    monkeypatch.setenv("AO_SESSION_ID", "example-orchestrator")
    for caller in ("worker", None):
        if caller is None:
            monkeypatch.delenv("AO_CALLER_TYPE", raising=False)
        else:
            monkeypatch.setenv("AO_CALLER_TYPE", caller)
        d = writer.apply(_import_proposal(proposal_id=f"c-{caller}"))
        assert d.decision == "rejected"
        assert d.reason == "historical_closed_requires_orchestrator_caller"


def test_import_rejected_for_stale_session_proof(tmp_path, monkeypatch) -> None:
    _write_contract(tmp_path)
    writer = _writer(tmp_path)
    monkeypatch.setenv("AO_CALLER_TYPE", "orchestrator")
    for sid in ("some-other-session", None):
        if sid is None:
            monkeypatch.delenv("AO_SESSION_ID", raising=False)
        else:
            monkeypatch.setenv("AO_SESSION_ID", sid)
        d = writer.apply(_import_proposal(proposal_id=f"s-{sid}"))
        assert d.decision == "rejected"
        assert d.reason == "missing_or_stale_orchestrator_proof"
