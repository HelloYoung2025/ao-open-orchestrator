"""Unit tests for the read-time effective-action projection keystone ``project_effective_action``.

WHY this keystone exists (the false-exhaustion stall it un-sticks):
  Pre-projection, a target whose ``repair_attempts_total`` crossed the cap was recorded as
  ``repair_attempts_exhausted`` even when the overflow came from ENVIRONMENT faults (actuator
  unreachable / review timeout) that produced no content verdict. That target is actually still
  converging, but the raw exhausted obligation routes it to convergence/owner-escalation — a stall.
  ``project_effective_action`` re-derives, at READ time, whether the exhaustion is genuine: it
  projects back to ``repair_active`` ONLY when it can positively prove env-inflation (content within
  budget), and otherwise FAILS CLOSED to the raw stored action. It must never fabricate or tighten
  exhaustion. These tests pin both the relaxation and every fail-closed guard against LIVE parity.
"""

from __future__ import annotations

from ao_state_writer.writer import (
    ESCALATED_REVIEW_ACTUATOR_FAILURE_BLOCKER_CODE,
    MAX_REPAIR_ATTEMPTS,
    MAX_REPAIR_ATTEMPTS_TOTAL,
    StateWriter,
)


def _project(stored: dict, target: dict | None) -> str | None:
    return StateWriter.project_effective_action(stored, target)


def _exhausted(blocker_code: str, *, non_retryable: bool = False) -> dict:
    return {
        "next_required_action": "repair_attempts_exhausted",
        "blocker_code": blocker_code,
        "non_retryable": non_retryable,
    }


# --- relaxation: env-inflated exhaustion is un-stuck to repair_active ---


def test_env_inflated_content_within_budget_projects_repair_active() -> None:
    # 2 env faults + content_a at 2 (== cap, not over): repair_attempts_total inflated to 4+ but
    # content is still within budget, so the target is still converging.
    stored = _exhausted("content_a")
    target = {"repair_attempts": {ESCALATED_REVIEW_ACTUATOR_FAILURE_BLOCKER_CODE: 3, "content_a": MAX_REPAIR_ATTEMPTS}}
    assert _project(stored, target) == "repair_active"


def test_env_coded_exhaustion_with_no_real_content_projects_repair_active() -> None:
    # A stored exhaustion whose own blocker_code is environmental and with NO content overflow:
    # the env code is excluded from the per-code clause and content_total is 0, so un-stick.
    stored = _exhausted(ESCALATED_REVIEW_ACTUATOR_FAILURE_BLOCKER_CODE)
    target = {"repair_attempts": {ESCALATED_REVIEW_ACTUATOR_FAILURE_BLOCKER_CODE: MAX_REPAIR_ATTEMPTS_TOTAL + 5}}
    assert _project(stored, target) == "repair_active"


# --- fail-closed: genuine content exhaustion keeps the raw action ---


def test_genuine_per_code_overflow_keeps_raw() -> None:
    # codex parity nuance: content_a at 3 > MAX_REPAIR_ATTEMPTS(2) is a genuine per-code give-up.
    stored = _exhausted("content_a")
    target = {"repair_attempts": {"content_a": MAX_REPAIR_ATTEMPTS + 1}}
    assert _project(stored, target) == "repair_attempts_exhausted"


def test_genuine_content_total_overflow_keeps_raw() -> None:
    # Two content codes each within per-code cap but together overflowing the per-target total.
    stored = _exhausted("a")
    target = {"repair_attempts": {"a": MAX_REPAIR_ATTEMPTS, "b": MAX_REPAIR_ATTEMPTS_TOTAL}}
    assert _project(stored, target) == "repair_attempts_exhausted"


def test_non_retryable_keeps_raw() -> None:
    stored = _exhausted("content_a", non_retryable=True)
    target = {"repair_attempts": {"content_a": 1}}
    assert _project(stored, target) == "repair_attempts_exhausted"


# --- fail-closed: malformed inputs keep the raw action ---


def test_missing_target_keeps_raw() -> None:
    assert _project(_exhausted("content_a"), None) == "repair_attempts_exhausted"


def test_non_dict_attempts_keeps_raw() -> None:
    assert _project(_exhausted("content_a"), {"repair_attempts": ["not", "a", "dict"]}) == "repair_attempts_exhausted"


def test_negative_count_keeps_raw() -> None:
    assert _project(_exhausted("content_a"), {"repair_attempts": {"content_a": -1}}) == "repair_attempts_exhausted"


def test_non_numeric_count_keeps_raw() -> None:
    # int("x") raises ValueError → fail closed.
    assert _project(_exhausted("content_a"), {"repair_attempts": {"content_a": "x"}}) == "repair_attempts_exhausted"


# --- passthrough: a non-exhausted raw action is returned unchanged ---


def test_non_exhausted_action_returned_unchanged() -> None:
    stored = {"next_required_action": "dispatch_next_slice_plan_mode", "blocker_code": None}
    assert _project(stored, {"repair_attempts": {}}) == "dispatch_next_slice_plan_mode"
