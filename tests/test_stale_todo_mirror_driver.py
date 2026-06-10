"""Regression tests for the stale-TODO self-heal DRIVER (Group E sub-slice E1).

WHY (intent, not just behavior): Group C installed the continuation.py rendering/guard layer for
stale-TODO self-heal but left it INERT — nothing built the canonical current_state override. This slice
ports the cli.py driver that, on a ``stale_compact_current_state`` continuation result, rebuilds the
override from the accepted proposal + decision and RE-EVALUATES, so a lagging TODO mirror self-heals
instead of stalling unattended operation. The driver also feeds the apply path: a repair that resolves
to blocked/non-candidate must short-circuit BEFORE the dispatch lease is claimed, so a single-flight
lease is never held for non-dispatchable work. The hardcoded master-plan filename is generalized to the
contract-canonical name (S-GAP1) so the engine carries no product token.
"""

from __future__ import annotations

from pathlib import Path

from ao_state_writer import cli
from ao_state_writer.continuation import ContinuationResult
from ao_state_writer.writer import StateTransitionDecision


def _decision(**over: object) -> StateTransitionDecision:
    base: dict[str, object] = dict(
        decision="accepted",
        proposal_id="p1",
        reason="ok",
        state_revision=7,
        new_state="implemented",
        next_required_action="dispatch_next_slice_plan_mode",
    )
    base.update(over)
    return StateTransitionDecision(**base)  # type: ignore[arg-type]


# --- _stale_todo_revision -----------------------------------------------------------------------

def test_stale_todo_revision_matches() -> None:
    assert cli._stale_todo_revision(["todo_current_state_rev:42"]) == 42


def test_stale_todo_revision_no_match() -> None:
    # fullmatch: a prefix/garbled token must not match.
    assert cli._stale_todo_revision(["something_else", "todo_current_state_revX", "todo_current_state_rev:"]) is None


def test_stale_todo_revision_first_match_wins() -> None:
    assert cli._stale_todo_revision(["todo_current_state_rev:3", "todo_current_state_rev:9"]) == 3


def test_stale_todo_revision_empty() -> None:
    assert cli._stale_todo_revision([]) is None


# --- _text_or_unknown ---------------------------------------------------------------------------

def test_text_or_unknown_returns_string() -> None:
    assert cli._text_or_unknown("value", "fallback") == "value"


def test_text_or_unknown_falls_back_on_empty_and_non_string() -> None:
    assert cli._text_or_unknown("", "fallback") == "fallback"
    assert cli._text_or_unknown(None, "fallback") == "fallback"
    assert cli._text_or_unknown(123, "fallback") == "fallback"


# --- _state_writer_current_state_context --------------------------------------------------------

def _ctx(*, proposal: dict[str, object] | None = None, decision: StateTransitionDecision | None = None,
         rev: int = 5, plan: str = "MASTER_PLAN.md") -> dict[str, str]:
    return cli._state_writer_current_state_context(
        proposal=proposal if proposal is not None else {
            "target_id": "t9", "target_kind": "code",
            "requested_state": "implemented", "evidence_refs": ["anchor-7"],
        },
        decision=decision or _decision(),
        stale_todo_revision=rev,
        master_plan_file=plan,
    )


def test_context_has_exactly_five_keys() -> None:
    assert set(_ctx()) == {
        "current_phase", "next_locked_action", "review_gate_state",
        "latest_session_log_anchor", "todo_mirror_repair_note",
    }


def test_context_generalized_master_plan_file_respected() -> None:
    # The hardcoded product master-plan filename is replaced by the contract-canonical name, so the
    # note carries the caller-supplied filename and no brand prefix.
    ctx = _ctx(plan="MY_PROJECT_PLAN.md")
    assert "Do not modify MY_PROJECT_PLAN.md." in ctx["todo_mirror_repair_note"]
    assert "CLAW" not in ctx["todo_mirror_repair_note"]
    assert "MASTER_PLAN.md" not in ctx["todo_mirror_repair_note"]  # default not hardcoded either


def test_context_plan_mode_action_branch() -> None:
    ctx = _ctx(decision=_decision(next_required_action="dispatch_next_slice_plan_mode"))
    assert "select the next unlocked Master Plan/TODO slice" in ctx["next_locked_action"]
    assert ctx["next_locked_action"].startswith("dispatch_next_slice_plan_mode")


def test_context_non_plan_mode_action_branch() -> None:
    ctx = _ctx(decision=_decision(next_required_action="codex_cc_review"))
    assert "execute the current state-writer obligation" in ctx["next_locked_action"]
    assert ctx["next_locked_action"].startswith("codex_cc_review")


def test_context_evidence_anchor_from_first_string_ref() -> None:
    ctx = _ctx(proposal={"target_id": "t9", "evidence_refs": [123, "", "a1", "a2"]})
    assert ctx["latest_session_log_anchor"] == "a1"


def test_context_evidence_anchor_fallback_when_missing() -> None:
    ctx = _ctx(proposal={"target_id": "t9"}, decision=_decision(proposal_id="pZ"))
    assert ctx["latest_session_log_anchor"] == "state-writer:pZ"


def test_context_target_fallback_via_text_or_unknown() -> None:
    # Missing target_id/target_kind degrade to the "target" placeholder, not a crash.
    ctx = _ctx(proposal={})
    assert "target" in ctx["current_phase"]
    assert "target" in ctx["review_gate_state"]


def test_context_reflects_both_revisions() -> None:
    ctx = _ctx(decision=_decision(state_revision=11), rev=4)
    assert "rev11" in ctx["current_phase"]
    assert "rev4" in ctx["current_phase"]


# --- _evaluate_continuation_with_todo_mirror_context (driver e2e) -------------------------------

def test_driver_non_stale_passthrough(monkeypatch) -> None:
    result = ContinuationResult(decision="candidate", reason="ready")
    calls: list[dict] = []

    def fake_eval(**kw):
        calls.append(kw)
        return result

    monkeypatch.setattr(cli, "evaluate_continuation", fake_eval)
    out, override = cli._evaluate_continuation_with_todo_mirror_context(
        root=Path("/nonexistent"), ledger_path=Path("/nonexistent"),
        proposal_id="p1", decision=_decision(), ao_project_id=None,
    )
    assert out is result and override is None
    assert len(calls) == 1  # non-stale: only the bare eval, no re-evaluation


def test_driver_stale_repairs_to_candidate(monkeypatch) -> None:
    stale = ContinuationResult(decision="blocked", reason="stale_compact_current_state",
                               blockers=["todo_current_state_rev:5"])
    repaired = ContinuationResult(decision="candidate", reason="ready")
    seq = [stale, repaired]
    calls: list[dict] = []

    def fake_eval(**kw):
        calls.append(kw)
        return seq[len(calls) - 1]

    monkeypatch.setattr(cli, "evaluate_continuation", fake_eval)
    monkeypatch.setattr(cli, "_scan_ledger_for_proposal",
                        lambda lp, pid: {"target_id": "t1", "evidence_refs": ["a"]})
    out, override = cli._evaluate_continuation_with_todo_mirror_context(
        root=Path("/nonexistent"), ledger_path=Path("/nonexistent"),
        proposal_id="p1", decision=_decision(state_revision=9), ao_project_id=None,
    )
    assert out is repaired
    assert override is not None and "todo_mirror_repair_note" in override
    assert len(calls) == 2
    assert calls[1].get("current_state_override") == override  # re-eval consumed the override


def test_driver_stale_missing_revision_returns_original(monkeypatch) -> None:
    stale = ContinuationResult(decision="blocked", reason="stale_compact_current_state",
                               blockers=["no_revision_token_here"])
    monkeypatch.setattr(cli, "evaluate_continuation", lambda **kw: stale)
    monkeypatch.setattr(cli, "_scan_ledger_for_proposal", lambda lp, pid: {"target_id": "t1"})
    out, override = cli._evaluate_continuation_with_todo_mirror_context(
        root=Path("/nonexistent"), ledger_path=Path("/nonexistent"),
        proposal_id="p1", decision=_decision(), ao_project_id=None,
    )
    assert out is stale and override is None


def test_driver_stale_missing_proposal_returns_original(monkeypatch) -> None:
    stale = ContinuationResult(decision="blocked", reason="stale_compact_current_state",
                               blockers=["todo_current_state_rev:5"])
    monkeypatch.setattr(cli, "evaluate_continuation", lambda **kw: stale)
    monkeypatch.setattr(cli, "_scan_ledger_for_proposal", lambda lp, pid: None)
    out, override = cli._evaluate_continuation_with_todo_mirror_context(
        root=Path("/nonexistent"), ledger_path=Path("/nonexistent"),
        proposal_id="p1", decision=_decision(), ao_project_id=None,
    )
    assert out is stale and override is None


def test_driver_repaired_non_candidate_discards_override(monkeypatch) -> None:
    stale = ContinuationResult(decision="blocked", reason="stale_compact_current_state",
                               blockers=["todo_current_state_rev:5"])
    repaired = ContinuationResult(decision="blocked", reason="still_blocked")
    seq = [stale, repaired]
    calls: list[dict] = []

    def fake_eval(**kw):
        calls.append(kw)
        return seq[len(calls) - 1]

    monkeypatch.setattr(cli, "evaluate_continuation", fake_eval)
    monkeypatch.setattr(cli, "_scan_ledger_for_proposal", lambda lp, pid: {"target_id": "t1"})
    out, override = cli._evaluate_continuation_with_todo_mirror_context(
        root=Path("/nonexistent"), ledger_path=Path("/nonexistent"),
        proposal_id="p1", decision=_decision(), ao_project_id=None,
    )
    # Re-evaluated but did not reach candidate => override is discarded (caller must not dispatch it).
    assert out is repaired and override is None


# --- apply-path pre-claim guard: lease must NOT be claimed for non-dispatchable work -------------

class _FakeWriter:
    def __init__(self) -> None:
        self.state_path = Path("/nonexistent")
        self.claim_called = False

    def is_dispatched(self, pid: str) -> bool:
        return False

    def is_authorized(self, pid: str) -> bool:
        return False

    def claim_dispatch(self, pid: str) -> bool:
        self.claim_called = True
        return True

    def release_dispatch(self, pid: str) -> None:
        self.released = True


def _wire_dispatch_one(monkeypatch, driver_result: ContinuationResult,
                       override: dict[str, str] | None = None) -> None:
    monkeypatch.setattr(cli, "preflight_reconcile", lambda **kw: None)
    monkeypatch.setattr(cli, "_validate_contract", lambda root: None)
    stored = {
        "decision": "accepted", "proposal_id": "p1", "reason": "ok",
        "state_revision": 1, "next_required_action": "dispatch_next_slice_plan_mode",
    }
    monkeypatch.setattr(cli, "_read_state", lambda path: {"proposal_results": {"p1": stored}})
    monkeypatch.setattr(cli, "_evaluate_continuation_with_todo_mirror_context",
                        lambda **kw: (driver_result, override))


def test_apply_blocked_does_not_claim_lease(monkeypatch, capsys) -> None:
    writer = _FakeWriter()
    _wire_dispatch_one(monkeypatch, ContinuationResult(decision="blocked", reason="x", blockers=["b"]))
    rc = cli._dispatch_one(
        root=Path("/nonexistent"), writer=writer, ledger_path=Path("/nonexistent"),
        proposal_id="p1", ao_project_id=None, dry_run=False,
    )
    assert rc == 3
    assert writer.claim_called is False


def test_apply_non_candidate_does_not_claim_lease(monkeypatch, capsys) -> None:
    writer = _FakeWriter()
    _wire_dispatch_one(monkeypatch, ContinuationResult(decision="nothing_to_continue", reason="x"))
    rc = cli._dispatch_one(
        root=Path("/nonexistent"), writer=writer, ledger_path=Path("/nonexistent"),
        proposal_id="p1", ao_project_id=None, dry_run=False,
    )
    assert rc == 0
    assert writer.claim_called is False


def test_apply_candidate_threads_override_into_continue_after_apply(monkeypatch, capsys) -> None:
    # The whole point of E1's apply path: when the stale-mirror repair produced a non-None override,
    # that override MUST reach continue_after_apply (so the spawn is built from canonical state, not the
    # stale mirror). This is the mutation-catching assertion — deleting the
    # `current_state_override=current_state_override` wiring makes this test fail while the others pass.
    writer = _FakeWriter()
    override = {
        "current_phase": "t1 implemented (state-writer rev9; TODO mirror lagged rev5)",
        "next_locked_action": "dispatch_next_slice_plan_mode — ...",
        "review_gate_state": "state-writer proposal p1 is the current code obligation; ...",
        "latest_session_log_anchor": "anchor-1",
        "todo_mirror_repair_note": "Do not modify MASTER_PLAN.md. ...",
    }
    _wire_dispatch_one(monkeypatch, ContinuationResult(decision="candidate", reason="ready"), override=override)
    captured: dict = {}

    def fake_continue(**kw):
        captured.update(kw)
        return ContinuationResult(decision="spawn_failed", reason="test-stop")

    monkeypatch.setattr(cli, "continue_after_apply", fake_continue)
    rc = cli._dispatch_one(
        root=Path("/nonexistent"), writer=writer, ledger_path=Path("/nonexistent"),
        proposal_id="p1", ao_project_id=None, dry_run=False,
    )
    assert rc == 3  # spawn_failed terminal
    assert writer.claim_called is True  # candidate path DOES claim the lease
    assert captured.get("current_state_override") == override  # the override reached continue_after_apply
