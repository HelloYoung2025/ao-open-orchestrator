"""Direct unit tests for the B2 environment/content predicates.

WHY these predicates matter (the stall class they defend against):
  A flaky review ENVIRONMENT (actuator unreachable, review hard-timeout/uncertain) produces a
  blocker_code but NO content verdict. If such env faults were counted as content-repair attempts,
  a target whose CONTENT is still converging would be prematurely declared "exhausted" and pushed
  to convergence/escalation — a real stall class. These predicates draw the env-vs-content boundary:
  env faults are counted separately (``_environment_attempt_total``), never tip content exhaustion
  (``_content_exhausted``), only enter the env lane when paired with their LEGITIMATE producer
  (``_is_legitimate_environment_blocker`` — a forged (scope, mode) tuple is refused), and never
  consume a target's convergence budget unless genuinely a convergence-review spawn
  (``_dispatch_is_convergence_review``). The tests pin each boundary against LIVE's parity.
"""

from __future__ import annotations

from ao_state_writer.writer import (
    CODEX_CC_TIMEOUT_BLOCKER_CODE,
    ESCALATED_REVIEW_ACTUATOR_FAILURE_BLOCKER_CODE,
    MAX_REPAIR_ATTEMPTS,
    MAX_REPAIR_ATTEMPTS_TOTAL,
    StateTransitionProposal,
    StateWriter,
    _dispatch_is_convergence_review,
)


def _proposal(**kw) -> StateTransitionProposal:
    base = dict(
        proposal_id="p",
        target_kind="small_chapter",
        target_id="t",
        base_state_revision=0,
        requested_state="review_blocked",
        actor_role="worker",
    )
    base.update(kw)
    return StateTransitionProposal(**base)


# --- _is_legitimate_environment_blocker: env lane only for the reserved producer ---


def test_legit_codex_cc_timeout_enters_env_lane() -> None:
    p = _proposal(
        verdict="blocker",
        blocker_code=CODEX_CC_TIMEOUT_BLOCKER_CODE,
        review_scope="codex_cc",
        review_mode="watchdog_timeout",
    )
    assert StateWriter._is_legitimate_environment_blocker(p) is True


def test_forged_codex_cc_timeout_without_watchdog_mode_is_refused() -> None:
    # codex's boundary note: same env code WITHOUT review_mode="watchdog_timeout" is a forged tuple.
    p = _proposal(
        verdict="blocker",
        blocker_code=CODEX_CC_TIMEOUT_BLOCKER_CODE,
        review_scope="codex_cc",
        review_mode=None,
    )
    assert StateWriter._is_legitimate_environment_blocker(p) is False


def test_forged_actuator_code_from_wrong_scope_is_refused() -> None:
    # escalated-review actuator env code carried by a codex_cc-scope proposal must NOT enter the env lane.
    p = _proposal(
        verdict="blocker",
        blocker_code=ESCALATED_REVIEW_ACTUATOR_FAILURE_BLOCKER_CODE,
        review_scope="codex_cc",
        review_mode="actuator_failure",
    )
    assert StateWriter._is_legitimate_environment_blocker(p) is False


def test_legit_actuator_failure_enters_env_lane() -> None:
    p = _proposal(
        verdict="blocker",
        blocker_code=ESCALATED_REVIEW_ACTUATOR_FAILURE_BLOCKER_CODE,
        review_scope="escalated_review",
        review_mode="actuator_failure",
    )
    assert StateWriter._is_legitimate_environment_blocker(p) is True


def test_non_env_code_is_not_environment_blocker() -> None:
    p = _proposal(verdict="blocker", blocker_code="some_content_blocker", review_scope="none")
    assert StateWriter._is_legitimate_environment_blocker(p) is False


# --- _environment_attempt_total / _content_exhausted: the counting boundary ---


def test_environment_attempt_total_counts_only_env_codes() -> None:
    attempts = {CODEX_CC_TIMEOUT_BLOCKER_CODE: 3, "content_a": 5}
    assert StateWriter._environment_attempt_total(attempts) == 3


def test_env_only_attempts_never_content_exhausted() -> None:
    # Even a large pile of env faults must NOT exhaust content (they self-heal if the env recovers).
    attempts = {CODEX_CC_TIMEOUT_BLOCKER_CODE: MAX_REPAIR_ATTEMPTS_TOTAL + 10}
    assert StateWriter._content_exhausted(attempts) is False


def test_content_code_over_per_code_cap_is_exhausted() -> None:
    # Per-code overflow with content-total still within budget — the case a total-only check misses.
    attempts = {"content_a": MAX_REPAIR_ATTEMPTS + 1}
    assert StateWriter._content_exhausted(attempts) is True


def test_content_total_over_cap_is_exhausted() -> None:
    attempts = {"a": MAX_REPAIR_ATTEMPTS, "b": MAX_REPAIR_ATTEMPTS_TOTAL}
    assert StateWriter._content_exhausted(attempts) is True


def test_below_caps_not_exhausted() -> None:
    attempts = {"content_a": 1}
    assert StateWriter._content_exhausted(attempts) is False


# --- _dispatch_is_convergence_review: stamp dominates; env-coded legacy excluded ---


def test_dispatch_kind_stamp_dominates_raw_action() -> None:
    # A convergence_review stamp counts even though the stored raw action is repair_active.
    record = {"dispatch_kind": "convergence_review"}
    stored = {"next_required_action": "repair_active"}
    assert _dispatch_is_convergence_review(record, stored) is True
    # A non_convergence_spawn stamp never counts even if stored crossed the content threshold.
    record2 = {"dispatch_kind": "non_convergence_spawn"}
    stored2 = {"next_required_action": "repair_attempts_exhausted"}
    assert _dispatch_is_convergence_review(record2, stored2) is False


def test_legacy_record_raw_join_counts_content_exhaustion() -> None:
    record = {}  # no dispatch_kind → legacy fallback
    stored = {"next_required_action": "repair_attempts_exhausted", "blocker_code": "content_a"}
    assert _dispatch_is_convergence_review(record, stored) is True


def test_legacy_env_coded_record_never_consumes_convergence_budget() -> None:
    record = {}
    stored = {
        "next_required_action": "repair_attempts_exhausted",
        "blocker_code": CODEX_CC_TIMEOUT_BLOCKER_CODE,
    }
    assert _dispatch_is_convergence_review(record, stored) is False
