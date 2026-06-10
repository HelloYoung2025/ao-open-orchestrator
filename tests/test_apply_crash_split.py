"""C-FIX-9 regression suite (forward-port slice M1-S4): heal apply() crash-split orphan obligations.

WHY THIS EXISTS (the spec, not just a check): apply() used to persist `_write_state(state)` THEN
`_append_ledger(...)`. A hard kill between the two durably advances state.json (proposal_results
gains the accepted entry, the target moves, state_revision bumps) but leaves the append-only
ledger MISSING that proposal. The obligation-currency check then fail-closed that exact on-disk
shape as a "phantom", so the accepted obligation silently vanished from list-ready / list-gated /
dispatch -> permanent unattended stall with no adversary (the replay short-circuit returns the
cached decision without re-appending the ledger, so a worker re-submit cannot self-heal it).

The fix is two-part: Part A reorders apply() so the ledger append runs BEFORE the state write
(a crash now leaves the ledger AHEAD of state — enumeration ignores the orphan ledger line and a
re-submit re-applies cleanly, a harmless duplicate ledger line). Part B persists the obligation's
target_id/target_kind alongside the stored decision so an EXISTING state-ahead orphan can be
resolved to its target WITHOUT the ledger, even when sibling targets share its state.
"""

from __future__ import annotations

import json

import pytest

from test_public_core import _writer

from ao_state_writer.cli import (
    _is_current_actionable_obligation,
    _latest_accepted_revision_by_target,
)
from ao_state_writer.writer import StateTransitionProposal


def _read_state(writer) -> dict:
    return json.loads(writer.state_path.read_text(encoding="utf-8"))


def _seed_ready_evidence_pending(writer, *pids_targets: tuple[str, str]) -> None:
    """Seed each (proposal_id, target_id) as an accepted evidence_pending transition whose
    next_required_action is the tier-1 AUTO_SPAWN action codex_cc_review (a 'ready' candidate).
    base_state_revision advances 0,1,2,... because each accept bumps the shared state_revision."""
    for rev, (pid, target_id) in enumerate(pids_targets):
        decision = writer.apply(
            StateTransitionProposal(
                proposal_id=pid,
                target_kind="small_chapter",
                target_id=target_id,
                base_state_revision=rev,
                requested_state="evidence_pending",
                actor_role="worker",
                evidence_refs=[f"session-log#{target_id}"],
            )
        )
        assert decision.decision == "accepted"
        assert decision.next_required_action == "codex_cc_review"


def test_apply_appends_ledger_before_persisting_state(tmp_path) -> None:
    """C-FIX-9 Part A (WHY): the ledger append must run BEFORE the state.json write so a hard kill
    between them leaves the ledger AHEAD of state (proposal in ledger, NOT in proposal_results).
    Enumeration iterates proposal_results, so it ignores the orphan ledger line; a worker
    re-submit then finds no proposal_results entry -> no replay short-circuit -> re-applies
    cleanly. The old order (state-then-ledger) stranded a legitimate accepted obligation with no
    self-heal (replay short-circuited every re-apply) -> permanent unattended stall."""
    writer = _writer(tmp_path)
    _seed_ready_evidence_pending(writer, ("seed", "chapter-0"))  # state.json now exists

    def boom(_state: dict) -> None:
        raise RuntimeError("simulated hard kill between ledger append and state write")

    writer._write_state = boom  # type: ignore[method-assign]
    new_obligation = StateTransitionProposal(
        proposal_id="p1",
        target_kind="small_chapter",
        target_id="chapter-1",
        base_state_revision=1,
        requested_state="evidence_pending",
        actor_role="worker",
        evidence_refs=["session-log#chapter-1"],
    )
    with pytest.raises(RuntimeError):
        writer.apply(new_obligation)

    # Ledger AHEAD of state: p1 IS in the ledger (the append ran first) ...
    ledger_pids = [
        json.loads(ln)["proposal"]["proposal_id"]
        for ln in writer.ledger_path.read_text(encoding="utf-8").splitlines()
    ]
    assert "p1" in ledger_pids
    # ... but NOT in proposal_results (the state write never committed).
    assert "p1" not in _read_state(writer)["proposal_results"]

    # Self-heal: a re-submit is NOT short-circuited by replay (proposal_results lacks p1), so
    # it re-applies cleanly -> state and ledger both carry p1 (a harmless duplicate ledger line).
    del writer._write_state  # restore the real bound _write_state method
    decision = writer.apply(new_obligation)
    assert decision.decision == "accepted"
    assert not decision.replayed
    assert "p1" in _read_state(writer)["proposal_results"]


def test_crash_split_orphan_remains_current_via_persisted_target(tmp_path) -> None:
    """C-FIX-9 Part B (WHY): apply() persists state.json (atomic) and a kill can lose the ledger
    append, leaving an accepted obligation in proposal_results but MISSING from the ledger. The
    obligation-currency check used to fail that exact on-disk shape CLOSED (phantom suppression),
    silently dropping a legitimate accepted obligation from list-ready/list-gated/dispatch ->
    unattended stall with no adversary. The fix persists the obligation's target_id in the stored
    decision so the dispatch chokepoint recognizes the crash-split orphan as the live obligation
    WITHOUT the ledger — even when sibling targets share its state (which the conservative legacy
    fallback cannot disambiguate). This is the EXACT (forward-record) recovery path."""
    writer = _writer(tmp_path)
    # Two sibling obligations in the SAME state (evidence_pending) so a target-agnostic match
    # is ambiguous; only the persisted target_id can disambiguate the orphan.
    _seed_ready_evidence_pending(writer, ("p1", "chapter-1"), ("p2", "chapter-2"))

    # Reproduce the crash-split on-disk shape: surgically drop p2's line from the ledger so
    # state.json still has proposal_results["p2"] but the ledger lacks it.
    ledger_lines = writer.ledger_path.read_text(encoding="utf-8").splitlines()
    kept = [ln for ln in ledger_lines if json.loads(ln)["proposal"]["proposal_id"] != "p2"]
    writer.ledger_path.write_text("\n".join(kept) + "\n", encoding="utf-8")

    state = _read_state(writer)
    stored = state["proposal_results"]["p2"]
    assert stored.get("target_id") == "chapter-2"  # C-FIX-9 persisted the target hint
    latest = _latest_accepted_revision_by_target(state, writer.ledger_path)

    # The crash-split orphan must STILL be the current obligation (exact target match), not
    # dropped as a phantom -> autonomous dispatch can re-surface it.
    assert _is_current_actionable_obligation(
        state, "p2", stored, writer.ledger_path, latest
    ), "crash-split orphan (accepted, target still matches) must remain current"
    # p1 (present in the ledger, normal path) stays current too -> the orphan does not shadow it.
    assert _is_current_actionable_obligation(
        state, "p1", state["proposal_results"]["p1"], writer.ledger_path, latest
    )

    # Fail-closed preservation: if the orphan's target has moved on from the stored new_state
    # (superseded), it must NOT be resurrected even though it still carries the target hint.
    moved = _read_state(writer)
    moved["targets"]["chapter-2"]["state"] = "closure_candidate"
    assert not _is_current_actionable_obligation(
        moved, "p2", moved["proposal_results"]["p2"], writer.ledger_path, latest
    ), "a crash-split orphan whose target moved on must stay fail-closed"
