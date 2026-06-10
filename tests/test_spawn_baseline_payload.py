"""Unit tests for cli._spawn_baseline_issue_payload (Group E slice "b2": spawn-baseline surface).

WHY (intent, not just behavior): the payload is what the orchestrator brain sees when a target is
wedged on a bad spawn baseline. It MUST (a) forbid a fresh spawn (a re-spawn would land on the same
bad baseline), (b) name the baseline-repair action, and (c) ESCALATE when the offending worker
session could not even be killed (a failed termination) — because then a plain baseline repair is
insufficient: the session must be terminated first, so the repair list leads with a termination
retry. The kill-failed escalation is gated on a PROVEN commit mismatch; an "unverified" lease never
escalates. Mirrors LIVE cli.py:378-404.
"""

from __future__ import annotations

from ao_state_writer import cli


def test_commit_mismatch_result_forbids_spawn_and_names_repair() -> None:
    payload = cli._spawn_baseline_issue_payload(
        "p1",
        {
            "status": "spawned_base_mismatch",
            "baseline_attestation_result": "spawn_base_commit_mismatch",
            "spawn_session_id": "s1",
            "required_source_commit": "aaaa",
            "worker_worktree": "/work/tree",
            "worker_head": "bbbb",
            "reason": "spawn_base_commit_mismatch",
        },
    )
    assert payload["result"] == "spawn_base_commit_mismatch"
    assert payload["proposal_id"] == "p1"
    assert payload["forbidden_actions"] == ["spawn"]
    assert payload["allowed_repair_actions"] == ["orchestrator_spawn_baseline_repair"]
    assert payload["spawn_session_id"] == "s1"
    assert payload["required_source_commit"] == "aaaa"
    assert payload["worker_worktree"] == "/work/tree"
    assert payload["worker_head"] == "bbbb"
    assert payload["reason"] == "spawn_base_commit_mismatch"


def test_unknown_result_normalizes_to_unverified() -> None:
    # Anything that is not the exact proven-mismatch token is surfaced as the conservative
    # "unverified" result (we could not prove the baseline, so we do not claim a mismatch).
    payload = cli._spawn_baseline_issue_payload(
        "p2",
        {"status": "spawned_base_unverified", "baseline_attestation_result": "worker_worktree_missing"},
    )
    assert payload["result"] == "spawn_baseline_unverified"
    assert payload["forbidden_actions"] == ["spawn"]


def test_failed_termination_escalates_to_kill_failed() -> None:
    payload = cli._spawn_baseline_issue_payload(
        "p3",
        {
            "status": "spawned_base_mismatch",
            "baseline_attestation_result": "spawn_base_commit_mismatch",
            "spawn_session_termination": {"result": "failed", "detail": "kill timed out"},
        },
    )
    assert payload["result"] == "spawn_base_commit_mismatch_kill_failed"
    assert payload["allowed_repair_actions"] == [
        "orchestrator_spawn_session_termination_retry",
        "orchestrator_spawn_baseline_repair",
    ]
    assert payload["spawn_session_termination"] == {"result": "failed", "detail": "kill timed out"}


def test_successful_termination_does_not_escalate_but_passes_through() -> None:
    payload = cli._spawn_baseline_issue_payload(
        "p4",
        {
            "status": "spawned_base_mismatch",
            "baseline_attestation_result": "spawn_base_commit_mismatch",
            "spawn_session_termination": {"result": "terminated"},
        },
    )
    assert payload["result"] == "spawn_base_commit_mismatch"
    assert payload["allowed_repair_actions"] == ["orchestrator_spawn_baseline_repair"]
    assert payload["spawn_session_termination"] == {"result": "terminated"}  # kept for audit


def test_unverified_with_failed_termination_does_not_escalate() -> None:
    # The kill-failed escalation is gated on the COMMIT-MISMATCH result. An unverified lease with a
    # failed termination stays unverified — escalation applies only to a proven mismatch.
    payload = cli._spawn_baseline_issue_payload(
        "p5",
        {
            "status": "spawned_base_unverified",
            "baseline_attestation_result": "worker_head_unreadable",
            "spawn_session_termination": {"result": "failed"},
        },
    )
    assert payload["result"] == "spawn_baseline_unverified"
    assert payload["allowed_repair_actions"] == ["orchestrator_spawn_baseline_repair"]
