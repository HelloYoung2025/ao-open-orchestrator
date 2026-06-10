"""Unit tests for StateWriter.record_spawn_baseline_issue + spawn-baseline CONSUMED recognition
(Group E slice "b1": spawn-baseline record + status recognition).

WHY THIS MATTERS (intent, not just behavior)
A worker spawned on a worktree that cannot prove the required source baseline produced no review
receipt — the lease is real but the work sits on the wrong code. ``record_spawn_baseline_issue``
CONSUMES that lease as ``spawned_base_mismatch`` (a proven commit mismatch) or
``spawned_base_unverified`` (baseline could not be verified at all) so the global preflight NEVER
re-spawns the target on a known-bad baseline. The two statuses being in
``CONSUMED_DISPATCH_STATUSES`` is the exact seam that stops a re-spawn loop: ``is_dispatched`` must
return True and ``claim_dispatch`` must refuse. A regression that dropped either status from CONSUMED
would let the preflight reclaim the lease and re-dispatch the bad baseline forever. Mirrors LIVE
writer.py:962-1000 verbatim (behavior); the public engine adds only brand-neutral docstrings.
"""

from __future__ import annotations

import json
from pathlib import Path

from ao_state_writer.writer import CONSUMED_DISPATCH_STATUSES, StateWriter


def _writer(root: Path) -> StateWriter:
    base = root / ".omx" / "state" / "ao-state-writer"
    base.mkdir(parents=True, exist_ok=True)  # the single-flight lock is created before the first write
    return StateWriter(state_path=base / "state.json", ledger_path=base / "state-transitions.jsonl")


def _record(root: Path, proposal_id: str) -> dict:
    base = root / ".omx" / "state" / "ao-state-writer"
    state = json.loads((base / "state.json").read_text(encoding="utf-8"))
    return state["dispatched_proposals"][proposal_id]


def test_commit_mismatch_reason_maps_to_spawned_base_mismatch(tmp_path: Path) -> None:
    w = _writer(tmp_path)
    w.record_spawn_baseline_issue(
        "p-1",
        spawn_session_id="sess-1",
        reason="spawn_base_commit_mismatch",
        required_source_commit="aaaa111",
        worker_worktree="/work/tree",
        worker_head="bbbb222",
    )
    rec = _record(tmp_path, "p-1")
    assert rec["status"] == "spawned_base_mismatch"
    assert rec["reason"] == "spawn_base_commit_mismatch"
    assert rec["baseline_attestation_result"] == "spawn_base_commit_mismatch"
    assert rec["spawn_session_id"] == "sess-1"
    assert rec["spawn_attestation"] == "session"
    assert rec["authorized_by"] == "orchestrator_policy"
    assert rec["required_source_commit"] == "aaaa111"
    assert rec["worker_worktree"] == "/work/tree"
    assert rec["worker_head"] == "bbbb222"
    assert "time" in rec


def test_any_other_reason_maps_to_spawned_base_unverified(tmp_path: Path) -> None:
    # The status is a binary: ONLY the exact "spawn_base_commit_mismatch" reason is a proven mismatch;
    # every other reason (worktree missing, head unreadable, ...) is "could not verify" => unverified.
    w = _writer(tmp_path)
    w.record_spawn_baseline_issue("p-2", spawn_session_id="s", reason="worker_worktree_missing")
    rec = _record(tmp_path, "p-2")
    assert rec["status"] == "spawned_base_unverified"
    assert rec["reason"] == "worker_worktree_missing"
    assert rec["baseline_attestation_result"] == "worker_worktree_missing"


def test_optional_fields_are_omitted_when_falsy(tmp_path: Path) -> None:
    w = _writer(tmp_path)
    w.record_spawn_baseline_issue("p-3", spawn_session_id="s", reason="x")
    rec = _record(tmp_path, "p-3")
    for key in (
        "required_source_commit",
        "worker_worktree",
        "worker_head",
        "ao_project_id",
        "spawn_session_termination",
    ):
        assert key not in rec, key


def test_project_id_and_termination_pass_through(tmp_path: Path) -> None:
    w = _writer(tmp_path)
    termination = {"result": "failed", "detail": "kill timed out"}
    w.record_spawn_baseline_issue(
        "p-4",
        spawn_session_id="s",
        reason="spawn_base_commit_mismatch",
        ao_project_id="example-project",
        spawn_session_termination=termination,
    )
    rec = _record(tmp_path, "p-4")
    assert rec["ao_project_id"] == "example-project"
    assert rec["spawn_session_termination"] == termination


def test_recorded_lease_is_consumed_and_not_reclaimable(tmp_path: Path) -> None:
    # The load-bearing behavior: once recorded, the preflight must treat the lease as CONSUMED so the
    # target is never re-spawned on the known-bad baseline. This is the anti-respawn-loop seam.
    w = _writer(tmp_path)
    w.record_spawn_baseline_issue("p-5", spawn_session_id="s", reason="spawn_base_commit_mismatch")
    assert w.is_dispatched("p-5") is True
    assert w.claim_dispatch("p-5") is False  # a consumed baseline lease cannot be re-claimed

    w.record_spawn_baseline_issue("p-6", spawn_session_id="s", reason="unverifiable")
    assert w.is_dispatched("p-6") is True
    assert w.claim_dispatch("p-6") is False


def test_both_statuses_are_members_of_consumed_set() -> None:
    # Pin the seam directly: dropping either from CONSUMED_DISPATCH_STATUSES re-enables a re-spawn loop.
    assert "spawned_base_mismatch" in CONSUMED_DISPATCH_STATUSES
    assert "spawned_base_unverified" in CONSUMED_DISPATCH_STATUSES
