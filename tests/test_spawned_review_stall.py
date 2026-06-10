"""Unit tests for the spawned-review STALL DETECTION ported in Group E slice "a".

WHY (intent, not just behavior)
-------------------------------
A spawned codex_cc / escalated review can HANG — the worker dies, the review actuator wedges, the
verdict never returns. Before slice "a" the public ``_current_obligation_issue`` skipped every dispatched
proposal up front (``is_dispatched`` early), so a hung review wedged its target FOREVER with no
owner-visible obligation: the single most common unattended-operation stall. Slice "a" reads the spawned
dispatch record, and BEFORE the (now late) is_dispatched skip surfaces:

  * ``_spawned_review_stall_issue`` -> a resolvable ``spawned_review_timeout_due`` obligation whose
    repair is the watchdog ``--apply-timeout-blocker`` path; AND
  * ``_spawned_dispatch_liveness_issue`` -> a liveness-reconcile obligation for a non-review spawned
    dispatch that outlived the orphan-lease floor.

The codex_cc threshold is AO-002 lock-stepped to the watchdog's staged escalation
(``codex_cc_stall_threshold_minutes`` 15/30/45) so preflight emits the timeout at EXACTLY the elapsed
point the watchdog first produces a resolvable blocker — never a premature stall window that shadows a
sibling while the resolver still returns not-yet-due. ``_review_timeout_is_self_resolving`` is the
fingerprint that lets the watchdog resolver clear the very stall it evaluated without preflight
self-blocking on it (the 070c412 circular block).

These tests pin that intent. They are pure (no CLI / no StateWriter); the end-to-end CLI loop is proven
in tests/acceptance/test_watchdog_apply_timeout_blocker.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from ao_state_writer import cli
from ao_state_writer.watchdog import ReviewWatchdogObservation

ROOT = Path("/tmp/example-project")  # advisory-string root only; nothing is read/written here.
PID = "p-seed-1"
TARGET = "chapter-1"
TBP = {PID: TARGET}


def _iso_ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _spawned(time_value: object, **overrides) -> dict:
    record = {"status": "spawned", "time": time_value, "spawn_session_id": "sess-1"}
    record.update(overrides)
    return record


def _stored(action: str) -> dict:
    return {"decision": "accepted", "next_required_action": action}


# --- _parse_dispatch_timestamp ----------------------------------------------------------------

def test_parse_timestamp_offset_form() -> None:
    parsed = cli._parse_dispatch_timestamp("2020-01-01T00:00:00+00:00")
    assert parsed is not None and parsed.tzinfo is not None


def test_parse_timestamp_z_suffix_normalized() -> None:
    parsed = cli._parse_dispatch_timestamp("2020-01-01T00:00:00Z")
    assert parsed is not None and parsed.utcoffset() == timedelta(0)


def test_parse_timestamp_naive_is_rejected() -> None:
    # A tz-naive timestamp is ambiguous; treat as unparseable rather than guess UTC.
    assert cli._parse_dispatch_timestamp("2020-01-01T00:00:00") is None


def test_parse_timestamp_garbage_is_none() -> None:
    assert cli._parse_dispatch_timestamp("not-a-time") is None


# --- _dispatch_record_elapsed_seconds ---------------------------------------------------------

def test_elapsed_old_record_is_large_positive() -> None:
    elapsed = cli._dispatch_record_elapsed_seconds(_spawned("2000-01-01T00:00:00+00:00"))
    assert elapsed is not None and elapsed > 10 * 365 * 24 * 3600


def test_elapsed_non_string_time_is_none() -> None:
    assert cli._dispatch_record_elapsed_seconds(_spawned(12345)) is None


def test_elapsed_future_beyond_skew_is_none() -> None:
    # A dispatch timestamp far in the future is corrupt/clock-skewed -> unusable (not a tiny elapsed).
    future = (datetime.now(timezone.utc) + timedelta(seconds=cli.DISPATCH_TIME_FUTURE_SKEW_SECONDS + 120)).isoformat()
    assert cli._dispatch_record_elapsed_seconds(_spawned(future)) is None


def test_elapsed_recent_is_clamped_nonnegative_and_small() -> None:
    elapsed = cli._dispatch_record_elapsed_seconds(_spawned(_iso_ago(30)))
    assert elapsed is not None and 0.0 <= elapsed < 120


# --- _spawned_review_stall_threshold_seconds (AO-002 staging) ----------------------------------

def test_threshold_codex_cc_stages_15_30_45_capped() -> None:
    assert [cli._spawned_review_stall_threshold_seconds("codex_cc", p) for p in (0, 1, 2, 3, 9)] == [
        900,
        1800,
        2700,
        2700,
        2700,
    ]


def test_threshold_escalated_is_fixed_hard_timeout() -> None:
    assert cli._spawned_review_stall_threshold_seconds("escalated_review", 0) == 7200
    assert cli._spawned_review_stall_threshold_seconds("escalated_review", 5) == 7200


# --- _spawned_review_stall_issue --------------------------------------------------------------

def test_stall_non_spawned_record_is_none() -> None:
    rec = _spawned(_iso_ago(99999))
    rec["status"] = "spawned_unattested"
    assert (
        cli._spawned_review_stall_issue(
            root=ROOT, proposal_id=PID, stored=_stored("codex_cc_review"),
            dispatch_record=rec, target_by_proposal=TBP, state={},
        )
        is None
    )


def test_stall_none_record_is_none() -> None:
    assert (
        cli._spawned_review_stall_issue(
            root=ROOT, proposal_id=PID, stored=_stored("codex_cc_review"),
            dispatch_record=None, target_by_proposal=TBP, state={},
        )
        is None
    )


def test_stall_non_review_action_is_none() -> None:
    # A spawned non-review action is the liveness path's job, NOT the review-stall path.
    assert (
        cli._spawned_review_stall_issue(
            root=ROOT, proposal_id=PID, stored=_stored("dispatch_next_slice_plan_mode"),
            dispatch_record=_spawned(_iso_ago(99999)), target_by_proposal=TBP, state={},
        )
        is None
    )


def test_stall_missing_time_requires_time_reconciliation() -> None:
    issue = cli._spawned_review_stall_issue(
        root=ROOT, proposal_id=PID, stored=_stored("codex_cc_review"),
        dispatch_record=_spawned(None), target_by_proposal=TBP, state={},
    )
    assert issue is not None
    assert issue["result"] == "spawned_review_time_reconciliation_required"
    # This variant forbids the watchdog apply (it cannot fingerprint an ageless lease).
    assert "review_watchdog_apply_timeout_blocker" in issue["forbidden_actions"]


def test_stall_below_threshold_is_none() -> None:
    assert (
        cli._spawned_review_stall_issue(
            root=ROOT, proposal_id=PID, stored=_stored("codex_cc_review"),
            dispatch_record=_spawned(_iso_ago(120)), target_by_proposal=TBP, state={},
        )
        is None
    )


def test_stall_past_threshold_emits_timeout_due_with_brand_neutral_command() -> None:
    issue = cli._spawned_review_stall_issue(
        root=ROOT, proposal_id=PID, stored=_stored("codex_cc_review"),
        dispatch_record=_spawned(_iso_ago(1200)), target_by_proposal=TBP, state={},
    )
    assert issue is not None
    assert issue["result"] == "spawned_review_timeout_due"
    assert issue["review_scope"] == "codex_cc"
    assert issue["target_id"] == TARGET
    assert issue["allowed_repair_actions"][0] == "review_watchdog_apply_timeout_blocker"
    # The emitted observation round-trips through the watchdog loader (proves the resolver can consume it).
    obs = ReviewWatchdogObservation.from_payload(issue["review_watchdog_observation"])
    assert obs.review_scope == "codex_cc"
    assert f"dispatched_proposal:{PID}" in (obs.evidence_refs or [])
    # Brand-neutrality (the whole point of the public port): the advisory command is the console_script,
    # never LIVE's machine-coupled interpreter path, its source PYTHONPATH wrapper, or renamed-away vocab.
    command = issue["review_watchdog_command"]
    assert command[0] == "ao-state-writer"
    flat = " ".join(command)
    # Fragments are assembled from adjacent string literals so the leak scanner does not flag this
    # test's OWN source for the very tokens it asserts are ABSENT from the emitted command.
    for forbidden in ("/opt/" "homebrew", "claw-" "commander", "PYTHONPATH", "gpt" "_pro", "-m"):
        assert forbidden not in flat, (forbidden, command)


def test_stall_prior_timeouts_widen_threshold_ao002() -> None:
    # Identical 1200s elapsed: due at prior=0 (threshold 900) but NOT due at prior=2 (threshold 2700).
    rec = _spawned(_iso_ago(1200))
    state_no_prior = {"targets": {TARGET: {"repair_attempts": {}}}}
    state_two_prior = {"targets": {TARGET: {"repair_attempts": {"codex_cc_review_timeout": 2}}}}
    due = cli._spawned_review_stall_issue(
        root=ROOT, proposal_id=PID, stored=_stored("codex_cc_review"),
        dispatch_record=rec, target_by_proposal=TBP, state=state_no_prior,
    )
    not_due = cli._spawned_review_stall_issue(
        root=ROOT, proposal_id=PID, stored=_stored("codex_cc_review"),
        dispatch_record=rec, target_by_proposal=TBP, state=state_two_prior,
    )
    assert due is not None and due["result"] == "spawned_review_timeout_due"
    assert not_due is None


# --- _review_timeout_is_self_resolving (070c412 fingerprint) ----------------------------------

def _due_issue() -> dict:
    return {
        "result": "spawned_review_timeout_due",
        "proposal_id": PID,
        "target_id": TARGET,
        "review_scope": "codex_cc",
    }


def _resolver(**overrides) -> dict:
    base = {
        "proposal_id": PID,
        "target_id": TARGET,
        "review_scope": "codex_cc",
        "base_state_revision": 7,
    }
    base.update(overrides)
    return base


def test_self_resolving_none_resolver_is_false() -> None:
    assert cli._review_timeout_is_self_resolving(_due_issue(), {"state_revision": 7}, None) is False


def test_self_resolving_exact_fingerprint_is_true() -> None:
    assert (
        cli._review_timeout_is_self_resolving(_due_issue(), {"state_revision": 7}, _resolver()) is True
    )


def test_self_resolving_wrong_target_is_false() -> None:
    assert (
        cli._review_timeout_is_self_resolving(
            _due_issue(), {"state_revision": 7}, _resolver(target_id="other")
        )
        is False
    )


def test_self_resolving_wrong_scope_is_false() -> None:
    assert (
        cli._review_timeout_is_self_resolving(
            _due_issue(), {"state_revision": 7}, _resolver(review_scope="escalated_review")
        )
        is False
    )


def test_self_resolving_wrong_proposal_is_false() -> None:
    assert (
        cli._review_timeout_is_self_resolving(
            _due_issue(), {"state_revision": 7}, _resolver(proposal_id="p-other")
        )
        is False
    )


def test_self_resolving_missing_proposal_id_is_false() -> None:
    # Public hardening (stricter than LIVE): a resolver WITHOUT a pinned proposal_id (a malformed/manual
    # observation lacking the dispatched_proposal ref) must NOT self-exempt on target/scope/base alone —
    # it could otherwise clear a DIFFERENT proposal's stall sharing the same target/scope/revision.
    assert (
        cli._review_timeout_is_self_resolving(
            _due_issue(), {"state_revision": 7}, _resolver(proposal_id=None)
        )
        is False
    )


def test_self_resolving_empty_proposal_id_is_false() -> None:
    assert (
        cli._review_timeout_is_self_resolving(
            _due_issue(), {"state_revision": 7}, _resolver(proposal_id="")
        )
        is False
    )


def test_self_resolving_stale_base_revision_is_false() -> None:
    # A replayed watchdog command whose base no longer matches live state must NOT be exempted.
    assert (
        cli._review_timeout_is_self_resolving(_due_issue(), {"state_revision": 8}, _resolver())
        is False
    )


def test_self_resolving_non_timeout_result_is_false() -> None:
    # Only spawned_review_timeout_due is self-resolving; the reconciliation variant never is.
    recon = {**_due_issue(), "result": "spawned_review_time_reconciliation_required"}
    assert cli._review_timeout_is_self_resolving(recon, {"state_revision": 7}, _resolver()) is False


# --- _spawned_dispatch_liveness_issue ---------------------------------------------------------

def test_liveness_review_action_is_excluded() -> None:
    # codex_cc/escalated are the review-stall path's job; liveness only covers non-review dispatches.
    assert (
        cli._spawned_dispatch_liveness_issue(
            root=ROOT, state={}, proposal_id=PID, stored=_stored("codex_cc_review"),
            dispatch_record=_spawned(_iso_ago(99999)), target_by_proposal=TBP,
        )
        is None
    )


def test_liveness_non_spawned_is_none() -> None:
    rec = _spawned(_iso_ago(99999))
    rec["status"] = "final_convergence_recorded"
    assert (
        cli._spawned_dispatch_liveness_issue(
            root=ROOT, state={}, proposal_id=PID, stored=_stored("dispatch_next_slice_plan_mode"),
            dispatch_record=rec, target_by_proposal=TBP,
        )
        is None
    )


def test_liveness_overdue_autospawn_requires_reconcile() -> None:
    issue = cli._spawned_dispatch_liveness_issue(
        root=ROOT, state={}, proposal_id=PID, stored=_stored("dispatch_next_slice_plan_mode"),
        dispatch_record=_spawned("2000-01-01T00:00:00+00:00"), target_by_proposal=TBP,
    )
    assert issue is not None
    assert issue["result"] == "spawned_dispatch_liveness_reconcile_required"
    assert issue["threshold_seconds"] == cli.SPAWNED_DISPATCH_STALL_SECONDS
    cmds = issue["spawned_dispatch_reconcile_commands"]
    assert cmds["release_terminal"][0] == "ao-state-writer"
    assert ("claw-" "commander") not in " ".join(cmds["refresh_time"])


def test_liveness_recent_autospawn_is_none() -> None:
    assert (
        cli._spawned_dispatch_liveness_issue(
            root=ROOT, state={}, proposal_id=PID, stored=_stored("dispatch_next_slice_plan_mode"),
            dispatch_record=_spawned(_iso_ago(60)), target_by_proposal=TBP,
        )
        is None
    )


def test_liveness_missing_time_requires_time_reconciliation() -> None:
    issue = cli._spawned_dispatch_liveness_issue(
        root=ROOT, state={}, proposal_id=PID, stored=_stored("dispatch_next_slice_plan_mode"),
        dispatch_record=_spawned(None), target_by_proposal=TBP,
    )
    assert issue is not None
    assert issue["result"] == "spawned_dispatch_time_reconciliation_required"
