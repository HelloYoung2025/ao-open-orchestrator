"""Recognizer parity for the persistently-broken-review-environment escalation action.

`review_environment_unavailable` is a distinct continuation-vocabulary action (never folded into
the non-executable/convergence tuple). These tests pin the RECOGNITION surface: an accepted
obligation carrying that action must be surfaced as a first-class owner-visible escalation by both
the continuation evaluator and the shared CLI preflight chokepoint — NOT mis-routed to the
`unsupported_*` fallthrough (which would read as an action-vocabulary gap and stall the orchestrator).

No production path emits this action yet, so these tests directly seed the obligation — exactly the
state a later write-path escalation will persist. The assertions therefore exercise the recognizer in
isolation from any producer.
"""

from __future__ import annotations

from pathlib import Path
import json

from ao_state_writer.cli import preflight_reconcile
from ao_state_writer.continuation import evaluate_continuation
from ao_state_writer.writer import StateTransitionDecision, StateWriter

ENV_ACTION = "review_environment_unavailable"


def _write_contract(root: Path) -> None:
    root.joinpath("DIRECT_PROJECT_CONTRACT.toml").write_text(
        f'''
version = 1

[ao_clone_isolation]
active_root = "{root.as_posix()}"

[owner_proxy]
project_id = "example-project"

[continuation_policy]
orchestrator_session = "example-orchestrator"
auto_spawn_actions = [
  "dispatch_next_slice_plan_mode",
  "codex_cc_review",
  "repair_active",
  "state_writer_closure",
  "major_closure_candidate",
]
gated_actions = ["escalated_review"]
non_executable_actions = ["repair_attempts_exhausted"]
'''.lstrip(),
        encoding="utf-8",
    )


def _state_paths(root: Path) -> tuple[Path, Path]:
    base = root / ".omx" / "state" / "ao-state-writer"
    return base / "state.json", base / "state-transitions.jsonl"


def test_continuation_surfaces_env_escalation_as_blocked(tmp_path: Path) -> None:
    """evaluate_continuation maps an env-escalation obligation to a blocked owner-visible result."""
    _write_contract(tmp_path)
    decision = StateTransitionDecision(
        decision="accepted",
        proposal_id="prop-env-1",
        reason="review_blocked",
        state_revision=1,
        next_required_action=ENV_ACTION,
    )

    result = evaluate_continuation(
        root=tmp_path, decision=decision, ao_project_id="example-project"
    )

    assert result.decision == "blocked"
    assert result.reason == ENV_ACTION
    assert ENV_ACTION in result.blockers
    # Recognizer, not fallthrough: it must NOT be mistaken for an unknown action.
    assert result.reason != "unsupported_next_required_action"


def test_preflight_surfaces_env_escalation_payload(tmp_path: Path) -> None:
    """preflight_reconcile returns the review_environment_unavailable payload (exit 3), not unsupported."""
    _write_contract(tmp_path)
    state_path, ledger_path = _state_paths(tmp_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    # Direct-seed the obligation a later write path will persist. No ledger file is written, so the
    # obligation is treated as the current actionable one (back-compat: absent ledger cannot
    # disambiguate, so it is not filtered out).
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "state_revision": 1,
                "targets": {"target-1": {"state": "review_blocked"}},
                "proposal_results": {
                    "prop-env-1": {
                        "decision": "accepted",
                        "next_required_action": ENV_ACTION,
                        "state_revision": 1,
                        "new_state": "review_blocked",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    writer = StateWriter(state_path=state_path, ledger_path=ledger_path)

    outcome = preflight_reconcile(
        root=tmp_path, writer=writer, ledger_path=ledger_path, dry_run=True
    )

    assert outcome is not None
    payload, exit_code = outcome
    assert exit_code == 3
    assert payload["result"] == ENV_ACTION
    assert payload["next_required_action"] == ENV_ACTION
    assert payload["blocked_action"] == ENV_ACTION
    assert payload["allowed_repair_actions"] == ["review_environment_repair"]
    # Recognizer, not fallthrough.
    assert payload["result"] != "unsupported_current_obligation"
