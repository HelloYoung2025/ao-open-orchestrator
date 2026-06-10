"""Audit-fix regression suite (forward-port slice M1-S1): escalated-review gate deadlock +
in-flight dispatch-lease undercount.

WHY THIS EXISTS (the two audit-confirmed bugs this suite pins shut):

BUG1 (CRITICAL) — a non-major ``escalated_review_pending`` permanently deadlocks the loop.
Accept/reject asymmetry: the rejection guard required the review package only for
target_kind==major_chapter, but the accept path routed ANY kind with that requested_state to
``escalated_review`` and set target.state=escalated_review_pending. A small_chapter mis-route
with no package was thus ACCEPTED, registered no gate metadata, yet was inferred a LIVE gate by
the legacy-state inference (no package check) — which suppresses ALL sibling work and can NEVER
be authorized (no package_sha256) => one schema-valid proposal bricks the whole dispatch loop.
The fix is two-layered and both layers are pinned here: (a) reject at the source
(``escalated_review_requires_major_chapter`` / ``missing_escalated_review_package_receipt``), and
(b) defense-in-depth — the inference only trusts proposal ids whose append-only ledger record is
a VALID gate request (major + complete package), so a pre-fix legacy/corrupt poison can no longer
be inferred live while a legitimate legacy major+package gate still is.

BUG2 (MEDIUM) — ``operator_paused.active_dispatch_leases`` false quiescence. Counting only
status=='pending' reports 0 active leases while a confirmed worker is actually running (it sits
in 'spawned'/'spawned_unattested') — exactly the false quiescence the diagnostic exists to
prevent. The count must cover IN_FLIGHT_DISPATCH_STATUSES (pending + spawned-family), EXCLUDING
``final_convergence_recorded`` (a parked terminal record, not an active lease).
"""

from __future__ import annotations

import json
from unittest import mock

from conftest import ORCHESTRATOR_ENV, make_proposal, seed_dispatchable_close

from ao_state_writer.cli import (
    _count_active_dispatch_leases,
    _inferred_live_escalated_review_gate_ids,
    _live_escalated_review_gate_ids,
    _ready_candidates,
    main as cli_main,
)

_AO_IDENTITY_KEYS = ("AO_CALLER_TYPE", "AO_SESSION_ID", "AO_SESSION", "AO_PROJECT_ID")


def _clear_ao_identity(monkeypatch) -> None:
    for key in _AO_IDENTITY_KEYS:
        monkeypatch.delenv(key, raising=False)


def _read_state_json(writer) -> dict:
    return json.loads(writer.state_path.read_text(encoding="utf-8"))


# --- BUG1: non-major escalated-review mis-route rejected at the source -----------------------------

def test_non_major_escalated_review_rejected_loop_stays_ready(lifecycle_project, monkeypatch):
    """A small_chapter requesting escalated_review_pending is a mis-routing that MUST be rejected —
    if accepted it becomes an unclearable inferred-live gate that suppresses ALL sibling work,
    permanently deadlocking the dispatch loop. A concurrently-ready evidence_pending candidate must
    remain ready, and no phantom live gate may appear. A legit major+package request must still be
    accepted (the guard is surgical, not a blanket ban)."""
    project = lifecycle_project
    _clear_ao_identity(monkeypatch)
    writer = project.writer()

    healthy = writer.apply(
        make_proposal(proposal_id="p-health", target_id="chapter-healthy")
    )
    assert healthy.decision == "accepted", healthy

    rev = _read_state_json(writer)["state_revision"]
    poison = writer.apply(
        make_proposal(
            proposal_id="p-poison",
            target_id="chapter-poison",
            base_state_revision=rev,
            requested_state="escalated_review_pending",
            actor_role="worker",
            evidence_refs=["session-log#poison"],
        )
    )
    assert poison.decision == "rejected", poison
    assert poison.reason == "escalated_review_requires_major_chapter", poison

    state = _read_state_json(writer)
    assert "p-health" in _ready_candidates(state, writer, writer.ledger_path)
    assert _live_escalated_review_gate_ids(state, writer.ledger_path) == set()

    # The legitimate major+package path is unaffected.
    rev = state["state_revision"]
    ok = writer.apply(
        make_proposal(
            proposal_id="g-major",
            target_kind="major_chapter",
            target_id="major-1",
            base_state_revision=rev,
            requested_state="escalated_review_pending",
            actor_role="state_writer",
            evidence_refs=["session-log#major"],
            package_path="reports/major-1.zip",
            prompt_path="reports/PROMPT.md",
            package_sha256="deadbeef",
        )
    )
    assert ok.decision == "accepted", ok
    assert ok.next_required_action == "escalated_review", ok


def test_inferred_live_gate_excludes_packageless_keeps_legacy_major(tmp_path):
    """BUG1 defense-in-depth: the legacy-state inference must NOT infer a live gate from a
    package-less or non-major accepted escalated_review_pending (a pre-fix legacy/corrupt poison
    that is unclearable and would deadlock the loop), but MUST still infer a legitimate legacy
    major gate whose package metadata lives in the append-only ledger (the documented path the
    inference exists for). Without the valid-gate guard the poison would be inferred live."""
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        json.dumps(
            {"proposal": {"proposal_id": "p-poison", "target_id": "chapter-poison",
                          "target_kind": "small_chapter"}}
        )
        + "\n"
        + json.dumps(
            {"proposal": {"proposal_id": "p-legacy", "target_id": "major-legacy",
                          "target_kind": "major_chapter", "package_path": "r/a.zip",
                          "prompt_path": "r/P.md", "package_sha256": "abc123"}}
        )
        + "\n",
        encoding="utf-8",
    )
    state = {
        "proposal_results": {
            "p-poison": {"decision": "accepted", "next_required_action": "escalated_review",
                         "state_revision": 5},
            "p-legacy": {"decision": "accepted", "next_required_action": "escalated_review",
                         "state_revision": 6},
        },
        "targets": {
            "chapter-poison": {"state": "escalated_review_pending"},
            "major-legacy": {"state": "escalated_review_pending"},
        },
    }
    inferred = _inferred_live_escalated_review_gate_ids(state, ledger)
    assert "p-poison" not in inferred  # package-less/non-major -> NOT a live gate
    assert "p-legacy" in inferred  # legitimate legacy major+package -> preserved


# --- BUG2: the paused active-lease diagnostic counts the in-flight family --------------------------

def test_operator_paused_active_dispatch_leases_counts_spawned_inflight(
    lifecycle_project, capsys, monkeypatch
):
    """The operator_paused active_dispatch_leases diagnostic must count IN-FLIGHT leases
    (pending + spawned*), not only 'pending'. A confirmed worker sits in 'spawned'; a
    'pending'-only count reports 0 under pause = FALSE QUIESCENCE exactly when a worker is
    running. The count must stay 1 across claim(pending) -> confirm(spawned)."""
    project = lifecycle_project
    _clear_ao_identity(monkeypatch)
    writer = seed_dispatchable_close(project, monkeypatch)
    _clear_ao_identity(monkeypatch)

    assert writer.claim_dispatch("p-close-1") is True
    with mock.patch.dict("os.environ", ORCHESTRATOR_ENV):
        assert cli_main(["pause", "--root", str(project.root)]) == 0
    capsys.readouterr()

    assert cli_main(["list-ready", "--root", str(project.root)]) == 3
    paused_pending = json.loads(capsys.readouterr().out)
    assert paused_pending["result"] == "operator_paused"
    assert paused_pending["active_dispatch_leases"] == 1  # pending lease counted

    writer.confirm_dispatch("p-close-1", spawn_session_id="sess-1")  # worker now spawned/running
    assert cli_main(["list-ready", "--root", str(project.root)]) == 3
    paused_spawned = json.loads(capsys.readouterr().out)
    assert paused_spawned["active_dispatch_leases"] == 1  # spawned STILL counted (was 0 pre-fix)


def test_active_dispatch_leases_excludes_final_convergence_recorded():
    """final_convergence_recorded is a parked terminal/owner-visible convergence record, NOT an
    active worker lease, so it must NOT inflate the active-lease count (only pending + spawned*
    are in-flight)."""
    state = {
        "dispatched_proposals": {
            "a": {"status": "spawned"},
            "b": {"status": "final_convergence_recorded"},
            "c": {"status": "pending"},
            "d": {"status": "spawned_unattested"},
        }
    }
    assert _count_active_dispatch_leases(state) == 3  # a, c, d — NOT b
