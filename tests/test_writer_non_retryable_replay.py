"""Regression tests for the load-bearing ``StateTransitionDecision.non_retryable`` field (B1).

WHY this field is not cosmetic (the bug it fixes):
  A LIVE-written ``state.json`` stores each decision via ``asdict(decision)``, so its
  ``proposal_results`` entries carry ``non_retryable``. ``StateWriter.apply`` replays a
  previously-seen proposal_id by splatting that stored dict back into the dataclass —
  ``StateTransitionDecision(**replay, replayed=True)``. Before this field existed, that
  splat raised ``TypeError: ... unexpected keyword argument 'non_retryable'``, so the
  public engine could not read a live state file AT ALL (every already-applied proposal
  crashed replay). The field (default ``False``) makes the splat accept the key, and the
  one-line ``_accept`` setter makes a freshly-accepted decision carry the proposal's real
  value so it matches LIVE on disk. These tests pin both halves; a regression to "no field"
  would re-break cross-engine state compatibility and the shadow-replay parity gate.
"""

from __future__ import annotations

from pathlib import Path
import json

from ao_state_writer.writer import (
    CODEX_CC_MODEL,
    CODEX_CC_REASONING_EFFORT,
    StateTransitionProposal,
    StateWriter,
)


def _writer(root: Path) -> StateWriter:
    base = root / ".omx" / "state" / "ao-state-writer"
    return StateWriter(state_path=base / "state.json", ledger_path=base / "state-transitions.jsonl")


def _seed_state(root: Path, proposal_results: dict) -> None:
    base = root / ".omx" / "state" / "ao-state-writer"
    base.mkdir(parents=True, exist_ok=True)
    (base / "state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "state_revision": 1,
                "targets": {},
                "proposal_results": proposal_results,
            }
        ),
        encoding="utf-8",
    )


def test_replays_live_written_decision_carrying_non_retryable(tmp_path: Path) -> None:
    """The load-bearing pin: a stored decision dict that carries ``non_retryable: true`` (as LIVE
    writes it) must replay WITHOUT TypeError and preserve the True value."""
    stored = {
        "decision": "accepted",
        "proposal_id": "p1",
        "reason": "accepted",
        "state_revision": 1,
        "new_state": "repair_attempts_exhausted",
        "next_required_action": "repair_attempts_exhausted",
        "blocker_code": "some_blocker",
        "non_retryable": True,
    }
    _seed_state(tmp_path, {"p1": stored})
    proposal = StateTransitionProposal(
        proposal_id="p1",
        target_kind="small_chapter",
        target_id="t1",
        base_state_revision=1,
        requested_state="review_blocked",
        actor_role="worker",
        verdict="blocker",
        blocker_code="some_blocker",
        non_retryable=True,
    )
    decision = _writer(tmp_path).apply(proposal)  # must not raise TypeError
    assert decision.replayed is True
    assert decision.non_retryable is True


def test_fresh_accept_propagates_proposal_non_retryable(tmp_path: Path, monkeypatch) -> None:
    """A non_retryable=True blocker accepted fresh must serialize non_retryable=True (matches LIVE
    _accept), so a single non-retryable CONTENT blocker exhausts immediately rather than looping."""
    _seed_state(tmp_path, {})
    # A blocker must carry a real reviewer scope (unscoped blockers are refused); a CONTENT blocker
    # comes from a codex_cc review, and AO_CALLER_TYPE mirrors that caller.
    monkeypatch.setenv("AO_CALLER_TYPE", "codex_cc")
    proposal = StateTransitionProposal(
        proposal_id="p2",
        target_kind="small_chapter",
        target_id="t1",
        base_state_revision=1,
        requested_state="review_blocked",
        actor_role="codex_cc",
        evidence_refs=["evidence:demo"],
        review_scope="codex_cc",
        verdict="blocker",
        blocker_code="b",
        model=CODEX_CC_MODEL,
        reasoning_effort=CODEX_CC_REASONING_EFFORT,
        non_retryable=True,
    )
    decision = _writer(tmp_path).apply(proposal)
    assert decision.decision == "accepted", decision.reason
    assert decision.non_retryable is True


def test_fresh_accept_defaults_non_retryable_false(tmp_path: Path, monkeypatch) -> None:
    """A retryable blocker (non_retryable unset) keeps the default False — the field never
    spuriously flips a normal blocker to non-retryable."""
    _seed_state(tmp_path, {})
    # A blocker must carry a real reviewer scope (unscoped blockers are refused); a CONTENT blocker
    # comes from a codex_cc review, and AO_CALLER_TYPE mirrors that caller.
    monkeypatch.setenv("AO_CALLER_TYPE", "codex_cc")
    proposal = StateTransitionProposal(
        proposal_id="p3",
        target_kind="small_chapter",
        target_id="t1",
        base_state_revision=1,
        requested_state="review_blocked",
        actor_role="codex_cc",
        evidence_refs=["evidence:demo"],
        review_scope="codex_cc",
        verdict="blocker",
        blocker_code="b",
        model=CODEX_CC_MODEL,
        reasoning_effort=CODEX_CC_REASONING_EFFORT,
    )
    decision = _writer(tmp_path).apply(proposal)
    assert decision.decision == "accepted", decision.reason
    assert decision.non_retryable is False
