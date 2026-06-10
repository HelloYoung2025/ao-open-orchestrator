"""Regression tests for the staged codex_cc review-timeout threshold (Group D).

WHY (intent, not just behavior): a codex_cc review that times out used to be retried under the SAME
flat 15-min ceiling it just blew — a genuinely slow review would thrash (timeout -> retry -> timeout)
instead of being given more time. The staged threshold widens the wait 1x->2x->3x (15->30->45 min,
capped) with each PRIOR codex_cc_review_timeout on the target, so a slow review converges instead of
thrashing. The helper is SHARED (AO-002) so the watchdog and the cli.py preflight stall check agree on
the exact elapsed point a timeout is due; this slice ports the watchdog side (the cli.py preflight
alignment lands with Group E).
"""

from __future__ import annotations

import pytest

from ao_state_writer.watchdog import (
    ReviewWatchdogObservation,
    _evaluate_codex_cc,
    codex_cc_stall_threshold_minutes,
)


# --- the shared staged-threshold helper --------------------------------------------------------

@pytest.mark.parametrize(
    "prior,expected",
    [(0, 15.0), (1, 30.0), (2, 45.0), (3, 45.0), (99, 45.0)],
)
def test_threshold_widens_then_caps(prior: int, expected: float) -> None:
    assert codex_cc_stall_threshold_minutes(prior) == expected


@pytest.mark.parametrize(
    "prior,expected",
    [(0, 20.0), (1, 40.0), (2, 60.0), (3, 60.0)],
)
def test_threshold_diagnostic_base(prior: int, expected: float) -> None:
    assert codex_cc_stall_threshold_minutes(prior, is_diagnostic=True) == expected


# --- _evaluate_codex_cc end-to-end -------------------------------------------------------------

def _obs(target_id: str = "t1", *, kind: str = "ordinary") -> ReviewWatchdogObservation:
    return ReviewWatchdogObservation(
        target_id=target_id,
        review_scope="codex_cc",
        started_at="2026-01-01T00:00:00Z",
        last_activity_kind=kind,
    )


def _state(prior_timeouts: int, target_id: str = "t1") -> dict:
    return {"targets": {target_id: {"repair_attempts": {"codex_cc_review_timeout": prior_timeouts}}}}


def _eval(prior: int, elapsed: float, *, kind: str = "ordinary"):
    return _evaluate_codex_cc(_state(prior), _obs(kind=kind), "evidence_pending", [], elapsed)


def test_no_prior_timeout_boundary() -> None:
    # `if elapsed < threshold` stays in progress, so the timeout fires exactly AT the threshold.
    assert _eval(0, 14.999).decision == "in_progress"
    timed_out = _eval(0, 15.0)
    assert timed_out.decision == "timeout_candidate"
    assert timed_out.blocker_code == "codex_cc_review_timeout"
    assert timed_out.threshold_minutes == 15.0
    assert timed_out.proposal is not None


def test_one_prior_timeout_widens_to_30() -> None:
    # With one prior timeout the wait widens to 30 min: what would have timed out under the old flat
    # ceiling (e.g. 20 min) is now still in progress; the timeout only fires at 30.
    assert _eval(1, 20.0).decision == "in_progress"
    assert _eval(1, 29.999).decision == "in_progress"
    timed_out = _eval(1, 30.0)
    assert timed_out.decision == "timeout_candidate"
    assert timed_out.threshold_minutes == 30.0


def test_threshold_caps_at_45() -> None:
    assert _eval(2, 44.999).decision == "in_progress"
    assert _eval(2, 45.0).decision == "timeout_candidate"
    # Beyond the 3x cap the threshold does not grow further.
    assert _eval(9, 44.999).decision == "in_progress"
    assert _eval(9, 45.0).threshold_minutes == 45.0


def test_diagnostic_wait_uses_20_base() -> None:
    # A diagnostic/LSP wait uses the 20-min base; with no prior timeout it stays in progress at 19.999
    # and times out at 20.
    assert _eval(0, 19.999, kind="diagnostic_lsp").decision == "in_progress"
    timed_out = _eval(0, 20.0, kind="diagnostic_lsp")
    assert timed_out.decision == "timeout_candidate"
    assert timed_out.threshold_minutes == 20.0


def test_missing_target_defaults_to_zero_prior() -> None:
    # A target with no recorded repair_attempts is treated as zero prior timeouts (threshold 15).
    result = _evaluate_codex_cc({"targets": {}}, _obs(), "evidence_pending", [], 15.0)
    assert result.decision == "timeout_candidate"
    assert result.threshold_minutes == 15.0


def test_diagnostic_detected_via_last_function_call() -> None:
    # Diagnostic detection has TWO triggers (watchdog._is_diagnostic_wait): last_activity_kind in the
    # diagnostic set, OR an "lsp_"/"lsp."/"diagnostic" substring in last_function_call. The SECOND path
    # must also widen to the 20-min base — otherwise an LSP/diagnostic wait surfaced only via the
    # function name would be (wrongly) held to the 15-min ordinary ceiling.
    obs = ReviewWatchdogObservation(
        target_id="t1",
        review_scope="codex_cc",
        started_at="2026-01-01T00:00:00Z",
        last_activity_kind="ordinary",
        last_function_call="lsp_hover",
    )
    assert _evaluate_codex_cc(_state(0), obs, "evidence_pending", [], 19.999).decision == "in_progress"
    timed_out = _evaluate_codex_cc(_state(0), obs, "evidence_pending", [], 20.0)
    assert timed_out.decision == "timeout_candidate"
    assert timed_out.threshold_minutes == 20.0


def test_other_blocker_codes_do_not_widen_the_timeout() -> None:
    # The anti-thrash widening is SPECIFIC to codex_cc, not generic repair pressure: a target that
    # accrued OTHER repair attempts (content blocker, spawn-base mismatch) but never a codex_cc timeout
    # must still use the base 15-min threshold. This guards the intent — only a prior codex_cc_review_
    # timeout buys a slow review more time; unrelated blockers do not silently inflate the ceiling.
    state = {"targets": {"t1": {"repair_attempts": {"spawn_base_mismatch": 5, "content_blocker": 3}}}}
    assert _evaluate_codex_cc(state, _obs(), "evidence_pending", [], 14.999).decision == "in_progress"
    timed_out = _evaluate_codex_cc(state, _obs(), "evidence_pending", [], 15.0)
    assert timed_out.decision == "timeout_candidate"
    assert timed_out.threshold_minutes == 15.0
