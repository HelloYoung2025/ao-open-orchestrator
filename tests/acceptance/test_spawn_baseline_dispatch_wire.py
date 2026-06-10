"""Integration test for the spawn-flow WIRING ported in Group E slice "b3c"
(cli._dispatch_one: pre-spawn source check + opt-in-gated post-spawn baseline check).

WHY THIS MATTERS (intent, not just behavior)
b3b made the detector; b3c wires it into the actual dispatch path. The wiring must:

  1. (opt-in) After a successful spawn, run _spawn_baseline_issue. If it finds the worker on a BAD
     baseline, CONSUME the lease as a spawned_base_* obligation (so the target is never re-spawned on a
     bad baseline) and surface it at exit 3 instead of confirming the dispatch.
  2. (opt-in) If the worker IS on a good baseline (no issue), confirm the dispatch normally -> "spawned".
  3. (NOT opted in) NEVER run the baseline check at all — the contract token
     spawned_worker_requires_current_source_baseline governs the whole feature, so a generic adopter
     spawns unencumbered. This is the codex Q2-reversal OPTION-B decision (always-on would otherwise make
     AO session metadata a hidden requirement of every git-source spawn). We prove "not run" by making
     _spawn_baseline_issue RAISE if called.
  4. (opt-in) The PRE-spawn source check fails fast WITHOUT spawning when the source itself cannot provide
     a baseline (opted-in + source not git). We prove "did not spawn" by making continue_after_apply RAISE.

The spawn machinery (continue_after_apply) and the git detector (_spawn_baseline_issue) are monkeypatched
seams here so the test exercises ONLY the _dispatch_one wiring; the real git path is covered by
test_spawn_baseline_detect.py and the real no-op spawn by test_project_lifecycle.py::test_step2.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ao_state_writer import cli
from ao_state_writer.continuation import ContinuationResult
from ao_state_writer.writer import StateWriter

PID = "p1"
TID = "t1"


def _writer(root: Path) -> StateWriter:
    base = root / ".omx" / "state" / "ao-state-writer"
    base.mkdir(parents=True, exist_ok=True)
    state = {
        "schema_version": 1,
        "state_revision": 1,
        "targets": {TID: {"state": "evidence_pending", "repair_attempts": {}}},
        "proposal_results": {
            PID: {"decision": "accepted", "next_required_action": "codex_cc_review", "new_state": "evidence_pending"}
        },
        "dispatched_proposals": {},
    }
    (base / "state.json").write_text(json.dumps(state), encoding="utf-8")
    (base / "state-transitions.jsonl").write_text(
        json.dumps({"proposal": {"proposal_id": PID, "target_id": TID}, "decision": {}}) + "\n",
        encoding="utf-8",
    )
    return StateWriter(state_path=base / "state.json", ledger_path=base / "state-transitions.jsonl")


def _write_contract(root: Path, *, opted_in: bool) -> None:
    section = (
        '[state_writer.preflight]\nspawned_worker_requires_current_source_baseline = "required"\n'
        if opted_in
        else ""
    )
    root.joinpath("DIRECT_PROJECT_CONTRACT.toml").write_text(
        '[owner_proxy]\nproject_id = "example-project"\n\n' + section, encoding="utf-8"
    )


def _patch_path_to_spawn(monkeypatch) -> None:
    """Monkeypatch the upstream gates so _dispatch_one reaches the spawn tail with a 'spawned' result.
    The actual baseline wiring (pre-spawn source check, post-spawn issue) is left REAL — each test
    overrides _source_baseline_commit_for_spawn / _spawn_baseline_issue / continue_after_apply as needed.
    """
    monkeypatch.setattr(cli, "preflight_reconcile", lambda **kw: None)
    monkeypatch.setattr(cli, "_validate_contract", lambda root: None)
    monkeypatch.setattr(
        cli,
        "_projected_decision",
        lambda *a, **k: SimpleNamespace(decision="accepted", next_required_action="codex_cc_review"),
    )
    monkeypatch.setattr(
        cli,
        "_evaluate_continuation_with_todo_mirror_context",
        lambda **kw: (ContinuationResult(decision="candidate", reason="ok", would_run=["ao", "spawn"]), None),
    )


def _spawned_result(*_a, **_k) -> ContinuationResult:
    return ContinuationResult(decision="spawned", reason="ok", spawn_session_id="sid-x")


def _dispatch(root: Path, writer: StateWriter, capsys) -> tuple[int, dict]:
    code = cli._dispatch_one(
        root=root,
        writer=writer,
        ledger_path=writer.ledger_path,
        proposal_id=PID,
        ao_project_id="example-project",
        dry_run=False,
    )
    return code, json.loads(capsys.readouterr().out)


def _dispatched_status(writer: StateWriter) -> str | None:
    record = writer.dispatch_record(PID)
    return record.get("status") if isinstance(record, dict) else None


# --- opted-in: bad baseline -> consume + surface --------------------------------------------------

def test_opted_in_bad_baseline_records_and_surfaces(tmp_path, monkeypatch, capsys) -> None:
    _write_contract(tmp_path, opted_in=True)
    writer = _writer(tmp_path)
    _patch_path_to_spawn(monkeypatch)
    monkeypatch.setattr(cli, "_source_baseline_commit_for_spawn", lambda root: ("deadbeef", None))
    monkeypatch.setattr(cli, "continue_after_apply", _spawned_result)
    monkeypatch.setattr(
        cli,
        "_spawn_baseline_issue",
        lambda **kw: {
            "result": "spawn_base_commit_mismatch",
            "reason": "spawn_base_commit_mismatch",
            "spawn_session_id": "sid-x",
            "required_source_commit": "deadbeef",
            "worker_worktree": "/w",
            "worker_head": "cafe",
        },
    )
    code, payload = _dispatch(tmp_path, writer, capsys)
    assert code == 3, payload
    # The lease is CONSUMED as a base-status obligation, NOT confirmed as a live 'spawned' lease.
    assert _dispatched_status(writer) == "spawned_base_mismatch"


# --- opted-in: good baseline -> confirm 'spawned' -------------------------------------------------

def test_opted_in_good_baseline_confirms_spawned(tmp_path, monkeypatch, capsys) -> None:
    _write_contract(tmp_path, opted_in=True)
    writer = _writer(tmp_path)
    _patch_path_to_spawn(monkeypatch)
    monkeypatch.setattr(cli, "_source_baseline_commit_for_spawn", lambda root: ("deadbeef", None))
    monkeypatch.setattr(cli, "continue_after_apply", _spawned_result)
    monkeypatch.setattr(cli, "_spawn_baseline_issue", lambda **kw: None)  # worker on a good baseline
    code, payload = _dispatch(tmp_path, writer, capsys)
    assert code == 0, payload
    assert payload["result"] == "spawned", payload
    assert _dispatched_status(writer) == "spawned"


# --- NOT opted in: baseline check must not run at all (codex Q2 OPTION-B) --------------------------

def test_not_opted_in_skips_baseline_check_entirely(tmp_path, monkeypatch, capsys) -> None:
    _write_contract(tmp_path, opted_in=False)
    writer = _writer(tmp_path)
    _patch_path_to_spawn(monkeypatch)
    # When the contract does not opt in, NEITHER the pre-spawn source check NOR the post-spawn detector
    # may run (the whole feature is gated). Both seams RAISE so re-ungating EITHER 5a or 5b fails here
    # locally (not only via the /nonexistent stale-todo regression test).
    def _src_must_not_run(*a, **k):
        raise AssertionError("_source_baseline_commit_for_spawn must NOT run when the contract does not opt in")

    def _issue_must_not_run(**kw):
        raise AssertionError("_spawn_baseline_issue must NOT run when the contract does not opt in")

    monkeypatch.setattr(cli, "_source_baseline_commit_for_spawn", _src_must_not_run)
    monkeypatch.setattr(cli, "continue_after_apply", _spawned_result)
    monkeypatch.setattr(cli, "_spawn_baseline_issue", _issue_must_not_run)
    code, payload = _dispatch(tmp_path, writer, capsys)
    assert code == 0, payload
    assert payload["result"] == "spawned", payload
    assert _dispatched_status(writer) == "spawned"


# --- opted-in: pre-spawn source issue fails fast WITHOUT spawning ----------------------------------

def test_opted_in_source_not_git_fails_before_spawn(tmp_path, monkeypatch, capsys) -> None:
    _write_contract(tmp_path, opted_in=True)
    writer = _writer(tmp_path)
    _patch_path_to_spawn(monkeypatch)
    monkeypatch.setattr(
        cli,
        "_source_baseline_commit_for_spawn",
        lambda root: (None, {"result": "spawn_baseline_unverified", "reason": "source_not_git", "spawn_session_id": ""}),
    )

    def _must_not_spawn(*a, **k):
        raise AssertionError("continue_after_apply must NOT run when the source baseline check fails first")

    monkeypatch.setattr(cli, "continue_after_apply", _must_not_spawn)
    code, payload = _dispatch(tmp_path, writer, capsys)
    assert code == 3, payload
    # The dispatch lease was released (fail-fast before spawn): no live lease remains.
    assert _dispatched_status(writer) is None


# --- C-FIX-11: 'spawned' persists BEFORE baseline, so a baseline-time crash cannot double-spawn ----

def test_dispatch_persists_spawned_before_baseline_so_crash_cannot_double_spawn(
    tmp_path, monkeypatch, capsys
) -> None:
    """C-FIX-11: a SIGKILL DURING the git-bound baseline verification — after `ao spawn` already
    created the worker and continue_after_apply returned its session id — must NOT leave the
    dispatch lease at 'pending'.

    WHY: `ao spawn` is NOT idempotent. The proposal_id dedupe that prevents a double-spawn lives
    ONLY in the state-writer's claim/confirm/release on dispatched_proposals, not in the spawn
    runtime (continuation builds the spawn argv with no idempotency key). If confirm_dispatch ran
    only AFTER baseline verification, a crash mid-baseline would persist nothing past the 'pending'
    claim. is_dispatched() does NOT count 'pending', so after STALE_PENDING_DISPATCH_SECONDS the
    stale-lease reclaim (claim_dispatch fall-through / reconcile-leases, which give ordinary fast
    spawn actions the 600s floor — C-FIX-10's 7800s floor only covers codex_cc_review /
    escalated_review) re-dispatches the SAME proposal and spawns a SECOND worker for one
    obligation -> conflicting branches/worktrees/evidence, no adversary, no state corruption. The
    fix persists the consume-once 'spawned' record (carrying spawn_session_id, so
    reconcile-spawned-dispatch can later prove worker liveness) BEFORE baseline verification, so
    the worker we already created is durably consumed even if the parent dies before baseline
    completes.

    Fails before C-FIX-11: confirm_dispatch ran after _spawn_baseline_issue, so a baseline-time
    crash leaves status='pending', is_dispatched False, and the projection treats the proposal as
    re-dispatchable once the lease ages out == double-spawn."""
    from ao_state_writer.cli import _ready_candidates

    _write_contract(tmp_path, opted_in=True)
    writer = _writer(tmp_path)
    _patch_path_to_spawn(monkeypatch)
    monkeypatch.setattr(cli, "_source_baseline_commit_for_spawn", lambda root: ("deadbeef", None))
    monkeypatch.setattr(cli, "continue_after_apply", _spawned_result)

    # Simulate the SIGKILL: the spawn succeeded (session id captured), then the parent dies the
    # instant baseline verification begins. _dispatch_one has no try/except around the baseline
    # call, so the raise propagates exactly like a hard kill — only what was already persisted to
    # state.json survives.
    def _simulated_kill(**kw):
        raise RuntimeError("simulated SIGKILL during baseline verification")

    monkeypatch.setattr(cli, "_spawn_baseline_issue", _simulated_kill)
    with pytest.raises(RuntimeError):
        cli._dispatch_one(
            root=tmp_path,
            writer=writer,
            ledger_path=writer.ledger_path,
            proposal_id=PID,
            ao_project_id="example-project",
            dry_run=False,
        )
    capsys.readouterr()  # drain any pre-crash output

    # The consume-once 'spawned' record (with the worker's session id) survived the crash.
    record = writer.dispatch_record(PID)
    assert record["status"] == "spawned"
    assert record["spawn_session_id"] == "sid-x"
    # is_dispatched now counts it -> a re-run cannot blindly re-spawn the same proposal.
    assert writer.is_dispatched(PID)
    # Re-dispatch is refused (no second spawn for the same obligation) and the projection no
    # longer offers the proposal as a ready candidate.
    assert not writer.claim_dispatch(PID)
    state = json.loads(writer.state_path.read_text(encoding="utf-8"))
    assert PID not in _ready_candidates(state, writer, writer.ledger_path)
