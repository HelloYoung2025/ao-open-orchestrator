"""C-FIX-10/11/12 regression suite (forward-port slice M1-S4): dispatch-lease & actuator integrity.

WHY THIS EXISTS (the spec, not just a check): the dispatch ledger's consume-once guarantee is only
as strong as its orphan-reclaim heuristics. Three sibling holes let a reconcile/re-dispatch path
run an external side effect TWICE:

- C-FIX-10: claim_dispatch reclaimed ANY 'pending' lease older than the 600s fast-spawn floor,
  ACTION-BLIND — but an escalated-review actuator legitimately holds 'pending' for hours while its
  bridge runs, so the live lease was reclaimed and a SECOND external review submission launched
  (the external side effect is NOT consume-once). The fix is a per-action orphan floor.
- C-FIX-11: a kill during baseline verification — after a successful fast spawn but before
  confirm_dispatch — left a 'pending' lease whose worker was ALREADY RUNNING; the 600s reclaim
  then spawned a duplicate worker. The fix persists the consume-once 'spawned' record BEFORE
  baseline verification (its regression test lives with the baseline-wire suite in
  tests/acceptance/test_spawn_baseline_dispatch_wire.py).
- C-FIX-12: the escalated-review actuator could submit a review job built from a STALE gate
  (authorized, then superseded) — the payload builder must re-check the gate is still the live one.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from test_public_core import _writer


def _aged(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S%z"
    )


def test_claim_dispatch_keeps_live_long_running_actuator_lease(tmp_path) -> None:
    """C-FIX-10 (WHY): claim_dispatch reclaimed ANY 'pending' lease older than the 600s fast-spawn
    orphan floor, action-blind. An escalated_review actuator legitimately holds 'pending' up to the
    7200s bridge timeout; a reconcile re-dispatch in the 600..7200s window would reclaim the LIVE
    lease and launch a SECOND external review submission (double-submit — the external side effect
    is NOT consume-once). The lease's action must raise its orphan floor; a fast AO-spawn lease at
    the same age still reclaims (no regression). Mirrors the actuator/review exclusion the
    reconcile-leases janitor already enforces via _is_spawned_dispatch_reconcile_action."""
    writer = _writer(tmp_path)
    writer.state_path.parent.mkdir(parents=True, exist_ok=True)
    aged = _aged(1800)
    # Past the long actuator floor (7800s): the bridge is provably dead (the watchdog hard
    # timeout fired at 7200s), so a still-pending lease here is a true orphan, reclaimable.
    aged_orphan = _aged(7801)
    escalated_result = {
        "decision": "accepted",
        "reason": "accepted",
        "new_state": "escalated_review_pending",
        "next_required_action": "escalated_review",
    }
    state = {
        "schema_version": 1,
        "state_revision": 3,
        "targets": {},
        "proposal_results": {
            "g1": {**escalated_result, "proposal_id": "g1", "state_revision": 1},
            "f1": {
                "decision": "accepted",
                "proposal_id": "f1",
                "reason": "accepted",
                "state_revision": 2,
                "new_state": "evidence_pending",
                "next_required_action": "repair_active",
            },
            "g2": {**escalated_result, "proposal_id": "g2", "state_revision": 3},
        },
        "dispatched_proposals": {
            "g1": {"status": "pending", "time": aged},
            "f1": {"status": "pending", "time": aged},
            "g2": {"status": "pending", "time": aged_orphan},
        },
    }
    writer.state_path.write_text(json.dumps(state), encoding="utf-8")

    # The live actuator lease (1800s < 7200s bridge) must NOT be reclaimed -> no second actuator.
    assert not writer.claim_dispatch(
        "g1"
    ), "a live escalated-review actuator lease (<7200s) must not be reclaimed -> no double-submit"
    # A fast AO-spawn lease at the same 1800s age is a real orphan -> still reclaimed (True).
    assert writer.claim_dispatch(
        "f1"
    ), "a fast AO-spawn lease past the 600s floor stays reclaimable (no regression)"
    # Anti-deadlock preserved: an actuator lease past the 7800s floor IS reclaimed, so a
    # SIGKILL mid-bridge can never wedge the gate forever.
    assert writer.claim_dispatch(
        "g2"
    ), "an escalated-review actuator lease past the long floor is a true orphan -> reclaimable"


def test_review_job_refuses_stale_gate_after_target_leaves_escalated_review_pending(
    tmp_path, monkeypatch, capsys
) -> None:
    """C-FIX-12: once the target has LEFT escalated_review_pending (a watchdog hard-timeout or a
    review blocker moved it to review_blocked), review-job / the actuator MUST refuse the old gate
    as stale BEFORE building a job or SENDing to the external reviewer — even though the sticky
    active_escalated_review_gate_proposal_id still points at this proposal and the orchestrator
    authorization (bound to the proposal's immutable accepted revision) still validates.

    WHY: the external review bridge is NOT consume-once; the only thing stopping a SECOND external
    submission for one obligation is the state-writer gate check. The dispatch live-gate and the
    receipt-apply rejection both require target.state == escalated_review_pending, but
    _build_review_job_payload only checked the sticky pointer (never cleared). So after the 7200s
    watchdog hard-timeout moves the target out of pending, a crash-reclaimed (>=7800s, C-FIX-10)
    or stale direct actuate would still pass the actuator-time check and re-SEND, with only the
    LATER receipt-apply rejecting it — too late, the external side effect already happened. A
    content blocker is the simplest transition out of escalated_review_pending here; the guard is
    state-based and identical for the watchdog-timeout path.

    Fails before C-FIX-12: review-job returns 'review_job' (a SENDable job) for a target that has
    already moved to review_blocked."""
    from test_public_core import _apply_package_gate, _write_contract, _writer
    from test_review_receipt_freshness import (
        _authorize_gate,
        _escalated_receipt,
        _read_state,
        _write_receipt_artifact,
    )

    from ao_state_writer.cli import main as cli_main

    _write_contract(tmp_path)
    gate = _apply_package_gate(tmp_path)
    writer = _writer(tmp_path)
    nonce = _authorize_gate(writer, monkeypatch, "gate-1")
    monkeypatch.delenv("AO_CALLER_TYPE", raising=False)
    monkeypatch.delenv("AO_SESSION_ID", raising=False)

    # Sanity: while the target IS escalated_review_pending, the authorized gate yields a job.
    live_exit = cli_main(["review-job", "--root", str(tmp_path), "--proposal-id", "gate-1"])
    assert live_exit == 0
    assert json.loads(capsys.readouterr().out)["result"] == "review_job"

    # A review blocker moves the target OUT of escalated_review_pending -> review_blocked.
    monkeypatch.setenv("AO_CALLER_TYPE", "escalated_review_actuator")
    blocker_sha, blocker_ref = _write_receipt_artifact(
        tmp_path, "escalated-review-receipts/c-fix-12-blocker.json"
    )
    blocked = writer.apply(
        _escalated_receipt(
            proposal_id="er-blocker", base_state_revision=1, verdict="blocker",
            nonce=nonce, receipt_sha=blocker_sha, artifact_ref=blocker_ref,
            package_sha256=gate.package_sha256, blocker_code="fresh_blocker",
        )
    )
    assert blocked.decision == "accepted", blocked
    monkeypatch.delenv("AO_CALLER_TYPE", raising=False)
    state = _read_state(writer)
    assert state["targets"]["chapter-1"]["state"] == "review_blocked"
    # The bug's preconditions still hold: the sticky pointer is unchanged (never cleared) and
    # the authorization (bound to the proposal's immutable accepted revision) still validates,
    # so the OLD pointer-only check would have passed and built a SENDable job.
    assert state["targets"]["chapter-1"]["active_escalated_review_gate_proposal_id"] == "gate-1"
    assert writer.is_authorized("gate-1")

    # The state-aware actuator-time gate check must now REFUSE the stale gate -> no SEND.
    stale_exit = cli_main(["review-job", "--root", str(tmp_path), "--proposal-id", "gate-1"])
    payload = json.loads(capsys.readouterr().out)
    assert stale_exit == 3
    assert payload["result"] == "stale_escalated_review_gate"
