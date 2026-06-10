"""Regression tests for the stale-TODO-mirror guard + current_state override (Group C step 6).

WHY (intent, not just behavior): the repo-local TODO is a MIRROR of canonical state.json and can drift
stale. If a continuation dispatched off a stale TODO, the worker would act on outdated phase/action data
and mis-dispatch — a stall. The guard detects staleness by reading an explicit "state-writer rev: N"
token the TODO may carry in its compact current-state values and comparing it to the accepted decision
revision; when the TODO is older it FAILS CLOSED ("stale_compact_current_state") rather than dispatching
on stale data. The engine-side driver that reconciles a canonical override from state.json lands with
Group E; this slice installs the guard + the override-acceptance contract. The guard is opt-in: with no
rev token (the current public flow) it is skipped entirely — zero behavior change.
"""

from __future__ import annotations

from pathlib import Path

from ao_state_writer.continuation import (
    AUTO_SPAWN_ACTIONS,
    GATED_ACTIONS,
    NON_EXECUTABLE_ACTIONS,
    _compact_current_state_revision,
    _validate_current_state_override,
    evaluate_continuation,
)
from ao_state_writer.writer import StateTransitionDecision


def _action_array(values: tuple[str, ...]) -> str:
    return "[\n" + ",\n".join(f'  "{v}"' for v in values) + "\n]"


def _write_contract(root: Path) -> None:
    root.joinpath("DIRECT_PROJECT_CONTRACT.toml").write_text(
        "version = 1\n\n"
        "[ao_clone_isolation]\n"
        f'active_root = "{root.as_posix()}"\n\n'
        "[owner_proxy]\nproject_id = \"example-project\"\n\n"
        "[continuation_policy]\n"
        'orchestrator_session = "example-orchestrator"\n'
        f"auto_spawn_actions = {_action_array(AUTO_SPAWN_ACTIONS)}\n"
        f"gated_actions = {_action_array(GATED_ACTIONS)}\n"
        f"non_executable_actions = {_action_array(NON_EXECUTABLE_ACTIONS)}\n\n"
        "[dispatch_templates.next_step_plan_mode]\n"
        'template = """\nUse canonical next locked action. Do not modify MASTER_PLAN.md.\n"""\n',
        encoding="utf-8",
    )


def _write_todo(root: Path, *, current_phase: str = "phase-x") -> None:
    root.joinpath("TODO.md").write_text(
        "# TODO\n\n## Current Execution State\n\n"
        f"- current_phase: {current_phase}\n"
        "- next_locked_action: implement-slice\n"
        "- review_gate_state: none\n"
        "- latest_session_log_anchor: anchor-1\n",
        encoding="utf-8",
    )


def _decision(rev: int = 5) -> StateTransitionDecision:
    return StateTransitionDecision(
        decision="accepted",
        proposal_id="p-1",
        reason="ready",
        state_revision=rev,
        new_state="dispatch_ready",
        next_required_action="dispatch_next_slice_plan_mode",
    )


def _override(**extra: str) -> dict[str, str]:
    base = {
        "current_phase": "OVERRIDE_PHASE_TOKEN",
        "next_locked_action": "implement-slice",
        "review_gate_state": "none",
        "latest_session_log_anchor": "anchor-override",
    }
    base.update(extra)
    return base


# --- _compact_current_state_revision -----------------------------------------------------------

def test_revision_none_when_no_token() -> None:
    assert _compact_current_state_revision({"current_phase": "phase-x"}) is None


def test_revision_parses_token_variants() -> None:
    assert _compact_current_state_revision({"a": "state-writer rev: 7"}) == 7
    assert _compact_current_state_revision({"a": "State-Writer Revision 12"}) == 12
    assert _compact_current_state_revision({"a": "state-writer rev#3"}) == 3


def test_revision_takes_max_across_values() -> None:
    # Highest explicit token wins so a historical lower-rev mention cannot mask a current marker.
    assert _compact_current_state_revision({"a": "state-writer rev: 2", "b": "state-writer rev: 9"}) == 9


# --- _validate_current_state_override ----------------------------------------------------------

def test_override_validation_ok_with_required_fields() -> None:
    assert _validate_current_state_override(_override()) == []


def test_override_validation_allows_extra_keys() -> None:
    # The Group E driver's canonical override also carries todo_mirror_repair_note (step 5); an extra
    # key must NOT be rejected — the validator only requires the four compact fields.
    assert _validate_current_state_override(_override(todo_mirror_repair_note="stale->canonical")) == []


def test_override_validation_flags_missing_and_empty() -> None:
    missing = dict(_override())
    del missing["review_gate_state"]
    assert "missing_current_state_override_field:review_gate_state" in _validate_current_state_override(missing)
    empty = _override(current_phase="   ")
    assert "empty_current_state_override_field:current_phase" in _validate_current_state_override(empty)


# --- end-to-end through evaluate_continuation --------------------------------------------------

def test_no_rev_token_is_zero_behavior_change(tmp_path: Path) -> None:
    # The common public case: no rev token => guard skipped, no override => normal candidate.
    root = tmp_path.resolve()
    _write_contract(root)
    _write_todo(root)
    result = evaluate_continuation(root=root, decision=_decision(), ao_project_id="example-project")
    assert result.decision == "candidate", result.reason


def test_stale_todo_without_override_fails_closed(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    _write_contract(root)
    # Embed an OLD rev token in a captured field value; decision is at rev 5 > 1 => stale.
    _write_todo(root, current_phase="phase-x (state-writer rev: 1)")
    result = evaluate_continuation(root=root, decision=_decision(rev=5), ao_project_id="example-project")
    assert result.decision == "blocked"
    assert result.reason == "stale_compact_current_state"
    assert "todo_current_state_rev:1" in result.blockers
    assert "state_writer_rev:5" in result.blockers


def test_stale_todo_with_valid_override_proceeds(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    _write_contract(root)
    _write_todo(root, current_phase="phase-x (state-writer rev: 1)")
    result = evaluate_continuation(
        root=root,
        decision=_decision(rev=5),
        ao_project_id="example-project",
        current_state_override=_override(),
    )
    assert result.decision == "candidate", result.reason
    # The override replaced the stale current_state, so its phase token reaches the spawn prompt.
    assert "OVERRIDE_PHASE_TOKEN" in result.would_run[3]


def test_override_without_staleness_is_rejected(tmp_path: Path) -> None:
    # Passing an override when the TODO is NOT stale (no rev token) is a contract violation: the override
    # path must only be reached after a stale read was superseded.
    root = tmp_path.resolve()
    _write_contract(root)
    _write_todo(root)
    result = evaluate_continuation(
        root=root,
        decision=_decision(rev=5),
        ao_project_id="example-project",
        current_state_override=_override(),
    )
    assert result.decision == "blocked"
    assert result.reason == "unexpected_current_state_override"


def test_stale_todo_with_invalid_override_is_blocked(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    _write_contract(root)
    _write_todo(root, current_phase="phase-x (state-writer rev: 1)")
    bad = dict(_override())
    del bad["latest_session_log_anchor"]
    result = evaluate_continuation(
        root=root,
        decision=_decision(rev=5),
        ao_project_id="example-project",
        current_state_override=bad,
    )
    assert result.decision == "blocked"
    assert result.reason == "invalid_compact_current_state_override"
    assert "missing_current_state_override_field:latest_session_log_anchor" in result.blockers
