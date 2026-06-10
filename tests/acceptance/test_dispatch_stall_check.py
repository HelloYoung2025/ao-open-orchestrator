"""V1 regression suite: read-only dispatch-stall escalation (forward-port slice M1-S2).

WHY THIS EXISTS (the spec, not just a check): the orchestrator LLM is the SOLE dispatcher — the
systemic "detector but no autonomous tier-1 executor" failure mode is a wedged/down orchestrator
leaving a READY dispatch obligation in place forever while the sidecar only pokes it.
``dispatch-stall-check`` makes that stall owner-VISIBLE without becoming an auto-dispatcher:

  * OBSERVABILITY ONLY — it never dispatches/spawns/authorizes and never writes canonical state
    (the escalation cycle leaves state.json byte-identical);
  * the no-advance watermark is CALLER-passed and the next watermark RETURNED (the engine persists
    nothing; sidecar wiring is a separate owner-gated change);
  * operator_pause is excluded FIRST (an intentional pause is never a stall — P5 landed before V1
    precisely so this exclusion exists), and a MALFORMED pause record fails closed through it;
  * no ready obligation => no stall, which makes it structurally DISJOINT from the
    final-convergence parked halt (that requires NO ready/gated candidates);
  * malformed/negative watermarks fail closed (exit 2), and a rewound revision gets its own
    distinct result instead of masquerading as a stall.
"""

from __future__ import annotations

import json
from unittest import mock

from conftest import ORCHESTRATOR_ENV, _state_paths, seed_dispatchable_close
from test_convergence_fail_closed import _drive_to_genuine_content_exhaustion_by_count

from ao_state_writer.cli import (
    PRECHECK_FORBIDDEN_ACTIONS,
    _gated_candidates,
    _ready_candidates,
    main as cli_main,
)

_AO_IDENTITY_KEYS = ("AO_CALLER_TYPE", "AO_SESSION_ID", "AO_SESSION", "AO_PROJECT_ID")


def _clear_ao_identity(monkeypatch) -> None:
    for key in _AO_IDENTITY_KEYS:
        monkeypatch.delenv(key, raising=False)


def _stall_check(project, capsys, *, last_seen=None, no_advance=0, threshold=3) -> tuple[int, dict]:
    """Invoke dispatch-stall-check, returning (exit_code, payload). No caller proof: it is a
    pure read-only observability command (like list-ready), not a canonical-write endpoint."""
    argv = ["dispatch-stall-check", "--root", str(project.root),
            "--no-advance-count", str(no_advance), "--threshold-checks", str(threshold)]
    if last_seen is not None:
        argv += ["--last-seen-revision", str(last_seen)]
    code = cli_main(argv)
    return code, json.loads(capsys.readouterr().out)


def test_dispatch_stall_escalates_after_threshold_no_advance(lifecycle_project, capsys, monkeypatch):
    """A ready dispatch obligation that does NOT advance state_revision across N consecutive
    checks escalates to an owner-visible dispatch_stalled_no_advance (exit 3) WHEN NOT paused.
    Threshold is count-of-checks (deterministic, clock-free), and the command MUST write nothing:
    the whole escalation cycle leaves state.json byte-identical (non-actuation invariant)."""
    project = lifecycle_project
    _clear_ao_identity(monkeypatch)
    seed_dispatchable_close(project, monkeypatch)
    _clear_ao_identity(monkeypatch)
    state_file = _state_paths(project.root)[0]
    before_bytes = state_file.read_bytes()

    # Check 1 (first-ever, no watermark): streak initializes to 1, not stalled.
    ec, p = _stall_check(project, capsys, last_seen=None, threshold=3)
    assert ec == 0, p
    assert p["result"] == "dispatch_progressing"
    assert p["dispatch_stalled"] is False
    assert p["next_watermark"] == {"last_seen_revision": 3, "no_advance_count": 1}

    # Check 2 (same revision, streak 2 < 3): below threshold, still not stalled.
    ec, p = _stall_check(project, capsys, last_seen=3, no_advance=1, threshold=3)
    assert ec == 0, p
    assert p["result"] == "dispatch_no_advance_below_threshold"
    assert p["dispatch_stalled"] is False
    assert p["next_watermark"] == {"last_seen_revision": 3, "no_advance_count": 2}

    # Check 3 (same revision, streak 3 >= 3): ESCALATE.
    ec, p = _stall_check(project, capsys, last_seen=3, no_advance=2, threshold=3)
    assert ec == 3, p
    assert p["result"] == "dispatch_stalled_no_advance"
    assert p["dispatch_stalled"] is True
    assert p["current_obligation"] is True
    assert p["allowed_repair_actions"] == ["orchestrator_session_rebind"]
    assert p["forbidden_actions"] == PRECHECK_FORBIDDEN_ACTIONS
    assert "p-close-1" in p["ready_candidates"]
    assert p["next_watermark"] == {"last_seen_revision": 3, "no_advance_count": 3}

    assert before_bytes == state_file.read_bytes(), (
        "dispatch-stall-check must NEVER mutate canonical state"
    )


def test_dispatch_stall_resets_on_revision_advance(lifecycle_project, capsys, monkeypatch):
    """If state_revision advanced since the caller's watermark, the obligation IS progressing:
    the no-advance streak resets to 1 and never escalates, even if the caller passed a high prior
    count. This is what makes a healthy dispatching project immune to a false stall alarm."""
    project = lifecycle_project
    _clear_ao_identity(monkeypatch)
    seed_dispatchable_close(project, monkeypatch)  # current revision 3
    _clear_ao_identity(monkeypatch)
    ec, p = _stall_check(project, capsys, last_seen=2, no_advance=2, threshold=3)
    assert ec == 0, p
    assert p["result"] == "dispatch_advanced"
    assert p["dispatch_stalled"] is False
    assert p["next_watermark"] == {"last_seen_revision": 3, "no_advance_count": 1}


def test_dispatch_stall_below_threshold_no_escalation(lifecycle_project, capsys, monkeypatch):
    """A no-advance streak strictly below the threshold is normal latency, not a stall: the
    orchestrator LLM is allowed a few poke cycles to actuate a dispatch before we alarm."""
    project = lifecycle_project
    _clear_ao_identity(monkeypatch)
    seed_dispatchable_close(project, monkeypatch)
    _clear_ao_identity(monkeypatch)
    ec, p = _stall_check(project, capsys, last_seen=3, no_advance=1, threshold=5)
    assert ec == 0, p
    assert p["result"] == "dispatch_no_advance_below_threshold"
    assert p["dispatch_stalled"] is False


def test_dispatch_stall_excluded_while_operator_paused(lifecycle_project, capsys, monkeypatch):
    """operator_pause is checked FIRST: an intentionally paused project is NEVER a stall, even with
    a watermark that would otherwise escalate. Reusing P5's _operator_pause_issue here is the whole
    reason P5 landed before V1 — without it, the intentional pause would trip a false
    dispatch_stalled_no_advance alarm forever."""
    project = lifecycle_project
    _clear_ao_identity(monkeypatch)
    seed_dispatchable_close(project, monkeypatch)
    _clear_ao_identity(monkeypatch)
    with mock.patch.dict("os.environ", ORCHESTRATOR_ENV):
        assert cli_main(["pause", "--root", str(project.root), "--reason", "migration freeze"]) == 0
    capsys.readouterr()
    # Watermark that WOULD escalate (streak 6 >= 3) if pause were not excluded first.
    ec, p = _stall_check(project, capsys, last_seen=3, no_advance=5, threshold=3)
    assert ec == 0, p
    assert p["result"] == "operator_paused"
    assert p["dispatch_stalled"] is False
    assert p["next_watermark"]["no_advance_count"] == 0  # streak reset under pause


def test_dispatch_stall_no_ready_obligation_not_stalled(lifecycle_project, capsys, monkeypatch):
    """No ready dispatch candidate => not a dispatch stall, regardless of watermark. Parking
    REQUIRES no ready/gated candidates, so a parked (or simply idle) target can never be reported
    as a dispatch stall — the two halts are structurally disjoint."""
    project = lifecycle_project  # contract only, empty state => no ready candidates
    _clear_ao_identity(monkeypatch)
    ec, p = _stall_check(project, capsys, last_seen=0, no_advance=9, threshold=3)
    assert ec == 0, p
    assert p["result"] == "no_ready_obligation"
    assert p["dispatch_stalled"] is False


def test_dispatch_stall_no_ready_on_real_final_convergence_parked_state(
    lifecycle_project, capsys, monkeypatch
):
    """STRONGER than the empty-state proxy above: a GENUINE final_convergence_recorded parked
    target (ready AND gated both empty) must make dispatch-stall-check short-circuit to
    no_ready_obligation — even with a watermark that would otherwise escalate. This nails the
    disjointness invariant at the CLI level: dispatch-stall (requires a ready candidate) and the
    final-convergence parked halt (requires NO ready/gated candidates) are structurally disjoint,
    so a parked project can never raise a false dispatch-stall alarm."""
    project = lifecycle_project
    _clear_ao_identity(monkeypatch)
    exhausted_pid = _drive_to_genuine_content_exhaustion_by_count(project, monkeypatch)
    _clear_ao_identity(monkeypatch)
    with mock.patch.dict("os.environ", ORCHESTRATOR_ENV):
        assert cli_main([
            "record-final-convergence", "--root", str(project.root),
            "--proposal-id", exhausted_pid,
            "--evidence", "owner-proxy:final-convergence",
        ]) == 0
    capsys.readouterr()
    writer = project.writer()
    parked = json.loads(writer.state_path.read_text(encoding="utf-8"))
    # Precondition: a real parked state with NO ready/gated work (else this proves nothing).
    assert not _ready_candidates(parked, writer, writer.ledger_path)
    assert not _gated_candidates(parked, writer, writer.ledger_path)
    # Watermark that WOULD escalate (streak 10 >= 3) if no-ready did not short-circuit first.
    ec, p = _stall_check(
        project, capsys, last_seen=parked["state_revision"], no_advance=9, threshold=3
    )
    assert ec == 0, p
    assert p["result"] == "no_ready_obligation"
    assert p["dispatch_stalled"] is False


def test_dispatch_stall_rejects_malformed_watermark(lifecycle_project, capsys, monkeypatch):
    """A malformed/negative caller watermark fails closed (exit 2) WITHOUT any canonical write —
    a corrupt watermark must force the caller to reset/reconcile, never be coerced into a stall
    verdict (false escalation) or silently into 'progressing' (missed escalation)."""
    project = lifecycle_project
    _clear_ao_identity(monkeypatch)
    seed_dispatchable_close(project, monkeypatch)
    _clear_ao_identity(monkeypatch)
    state_file = _state_paths(project.root)[0]
    before_bytes = state_file.read_bytes()
    for kwargs in ({"no_advance": -1}, {"threshold": 0}, {"last_seen": -5}):
        ec, p = _stall_check(project, capsys, **kwargs)
        assert ec == 2, (kwargs, p)
        assert p["result"] == "invalid_dispatch_stall_watermark", (kwargs, p)
        assert p["dispatch_stalled"] is False, (kwargs, p)
    assert before_bytes == state_file.read_bytes(), (
        "rejecting a bad watermark must not mutate state"
    )


def test_dispatch_stall_watermark_regressed_is_distinct(lifecycle_project, capsys, monkeypatch):
    """A live state_revision BELOW the caller's watermark is a stale/cross-root watermark, not a
    no-advance stall: it gets its OWN result (dispatch_stall_watermark_regressed, exit 3) and
    resets the streak, so a rewound/mismatched watermark never masquerades as 'stalled'."""
    project = lifecycle_project
    _clear_ao_identity(monkeypatch)
    seed_dispatchable_close(project, monkeypatch)  # current revision 3
    _clear_ao_identity(monkeypatch)
    ec, p = _stall_check(project, capsys, last_seen=5, no_advance=1, threshold=3)
    assert ec == 3, p
    assert p["result"] == "dispatch_stall_watermark_regressed"
    assert p["dispatch_stalled"] is False
    assert p["next_watermark"] == {"last_seen_revision": 3, "no_advance_count": 1}


def test_dispatch_stall_malformed_operator_pause_fails_closed(lifecycle_project, capsys, monkeypatch):
    """A corrupt operator_pause record must fail closed THROUGH dispatch-stall-check (propagate
    operator_pause_malformed, exit 3), never be treated as 'not paused' — otherwise a paused
    project with a damaged pause record could trip a false stall escalation."""
    project = lifecycle_project
    _clear_ao_identity(monkeypatch)
    writer = seed_dispatchable_close(project, monkeypatch)
    _clear_ao_identity(monkeypatch)
    with writer._single_flight():
        state = writer._read_state()
        state["operator_pause"] = {"paused": "not-a-bool"}
        writer._write_state(state)
    ec, p = _stall_check(project, capsys, last_seen=3, no_advance=5, threshold=3)
    assert ec == 3, p
    assert p["result"] == "operator_pause_malformed"
