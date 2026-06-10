"""Unit tests for the cli-side decision-projection wrappers (Group E sub-slice E2).

WHY (intent, not just behavior): writer.project_effective_action (tested in
test_writer_project_effective_action.py) is the keystone that re-derives, at READ time, whether a
``repair_attempts_exhausted`` is GENUINE or merely env-inflated (environment review faults inflated the
per-target content budget without ever consuming it). E2 wires that keystone into the cli so the
dispatch/continue/ready paths see a false exhaustion as the repair_active it really is — otherwise a
still-converging target dead-ends at owner_proxy_convergence_required and stalls unattended operation.
``_effective_action`` resolves the target and delegates; ``_projected_decision`` rebuilds the decision
with only next_required_action projected (every other field passes through). Both FAIL CLOSED: an
unresolved target, or a genuine content exhaustion, keeps the raw exhausted action.
"""

from __future__ import annotations

from pathlib import Path

from ao_state_writer import cli
from ao_state_writer.writer import (
    ESCALATED_REVIEW_ACTUATOR_FAILURE_BLOCKER_CODE,
    MAX_REPAIR_ATTEMPTS,
    MAX_REPAIR_ATTEMPTS_TOTAL,
)

ENV_CODE = ESCALATED_REVIEW_ACTUATOR_FAILURE_BLOCKER_CODE


def _exhausted_stored(blocker_code: str, *, non_retryable: bool = False) -> dict:
    return {
        "decision": "accepted",
        "proposal_id": "p1",
        "reason": "exhausted",
        "state_revision": 5,
        "new_state": "implemented",
        "next_required_action": "repair_attempts_exhausted",
        "blocker_code": blocker_code,
        "non_retryable": non_retryable,
    }


def _env_inflated_target() -> dict:
    # All attempts env-coded and over the total cap, but zero content overflow -> false exhaustion.
    return {"repair_attempts": {ENV_CODE: MAX_REPAIR_ATTEMPTS_TOTAL + 5}}


def _genuine_target() -> dict:
    # A content code over its per-code cap -> genuine give-up.
    return {"repair_attempts": {"content_a": MAX_REPAIR_ATTEMPTS + 1}}


# --- _effective_action: resolve target + delegate --------------------------------------------

def test_effective_action_env_inflated_projects_repair_active() -> None:
    state = {"targets": {"t1": _env_inflated_target()}}
    out = cli._effective_action(state, "p1", _exhausted_stored(ENV_CODE), {"p1": "t1"})
    assert out == "repair_active"


def test_effective_action_genuine_exhaustion_keeps_raw() -> None:
    state = {"targets": {"t1": _genuine_target()}}
    out = cli._effective_action(state, "p1", _exhausted_stored("content_a"), {"p1": "t1"})
    assert out == "repair_attempts_exhausted"


def test_effective_action_unresolved_proposal_fails_closed() -> None:
    # proposal_id absent from target_by_proposal -> target None -> raw action kept.
    state = {"targets": {"t1": _env_inflated_target()}}
    out = cli._effective_action(state, "p1", _exhausted_stored(ENV_CODE), {})
    assert out == "repair_attempts_exhausted"


def test_effective_action_target_id_missing_from_state_fails_closed() -> None:
    # target_by_proposal points at a target that is not in state["targets"] -> None -> raw action.
    state = {"targets": {}}
    out = cli._effective_action(state, "p1", _exhausted_stored(ENV_CODE), {"p1": "t1"})
    assert out == "repair_attempts_exhausted"


def test_effective_action_non_exhausted_passthrough() -> None:
    state = {"targets": {"t1": {"repair_attempts": {}}}}
    stored = {"next_required_action": "dispatch_next_slice_plan_mode", "blocker_code": None}
    out = cli._effective_action(state, "p1", stored, {"p1": "t1"})
    assert out == "dispatch_next_slice_plan_mode"


# --- _projected_decision: project only next_required_action, pass through the rest -------------

def test_projected_decision_env_inflated_becomes_repair_active(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_proposal_targets_from_ledger", lambda lp: {"p1": "t1"})
    state = {"targets": {"t1": _env_inflated_target()}}
    decision = cli._projected_decision(state, "p1", _exhausted_stored(ENV_CODE), Path("/nonexistent"))
    assert decision.next_required_action == "repair_active"
    # Every other field passes through verbatim:
    assert decision.decision == "accepted"
    assert decision.proposal_id == "p1"
    assert decision.state_revision == 5
    assert decision.new_state == "implemented"
    assert decision.blocker_code == ENV_CODE


def test_projected_decision_genuine_exhaustion_unchanged(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_proposal_targets_from_ledger", lambda lp: {"p1": "t1"})
    state = {"targets": {"t1": _genuine_target()}}
    decision = cli._projected_decision(state, "p1", _exhausted_stored("content_a"), Path("/nonexistent"))
    assert decision.next_required_action == "repair_attempts_exhausted"
    assert decision.new_state == "implemented"  # untouched


def test_projected_decision_non_retryable_unchanged(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_proposal_targets_from_ledger", lambda lp: {"p1": "t1"})
    # non_retryable exhaustion is a genuine give-up even with env-only attempts -> stays raw.
    state = {"targets": {"t1": _env_inflated_target()}}
    decision = cli._projected_decision(
        state, "p1", _exhausted_stored(ENV_CODE, non_retryable=True), Path("/nonexistent"))
    assert decision.next_required_action == "repair_attempts_exhausted"
    assert decision.non_retryable is True


def test_projected_decision_unresolved_target_keeps_raw(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_proposal_targets_from_ledger", lambda lp: {})
    state = {"targets": {"t1": _env_inflated_target()}}
    decision = cli._projected_decision(state, "p1", _exhausted_stored(ENV_CODE), Path("/nonexistent"))
    assert decision.next_required_action == "repair_attempts_exhausted"
