"""P5 regression suite: first-class, state-visible operator pause (forward-port slice M1-S1).

WHY THIS EXISTS (the spec, not just a check): a pause that lives only in the orchestrator LLM's
instruction context is invisible to state.json/list-ready, so the sidecar pokes a "ready"
obligation forever. P5 makes pause a first-class state record:

  * the PROJECTION itself reports ``operator_paused`` (exit 3, no candidates, no spawn) so a poke
    becomes an explicit no-op the state machine can reason about — and resume restores the SAME
    candidates from the CURRENT state;
  * a worker-type caller cannot pause/resume (orchestrator caller proof gates the flip);
  * pause is orthogonal metadata — no state_revision bump, and an intervening apply() must not
    drop it (it is a passthrough top-level key, not a hard freeze);
  * a corrupt ``operator_pause`` record fails CLOSED to a typed envelope, never silently ignored;
  * "no canonical mutation except resume": every canonical-write command that bypasses shared
    preflight (reconcile-leases --apply, record-final-convergence, reconcile-spawn-attestation,
    reconcile-spawned-dispatch) carries its OWN pause guard — without those guards a paused
    project could still promote/delete/refresh dispatch leases mid-pause.

Each test fails before the P5 port (the operator_pause primitive does not exist).
"""

from __future__ import annotations

import json
from unittest import mock

from conftest import ORCHESTRATOR_ENV, _state_paths, make_proposal, seed_dispatchable_close

from ao_state_writer.cli import main as cli_main

_AO_IDENTITY_KEYS = ("AO_CALLER_TYPE", "AO_SESSION_ID", "AO_SESSION", "AO_PROJECT_ID")


def _clear_ao_identity(monkeypatch) -> None:
    """Strip inherited AO_* identity so each in-process CLI call controls its own env."""
    for key in _AO_IDENTITY_KEYS:
        monkeypatch.delenv(key, raising=False)


def _run_cli(capsys, argv: list[str]) -> tuple[int, dict]:
    code = cli_main(argv)
    return code, json.loads(capsys.readouterr().out)


def _pause(project, capsys, monkeypatch, *extra: str) -> tuple[int, dict]:
    with mock.patch.dict("os.environ", ORCHESTRATOR_ENV):
        return _run_cli(capsys, ["pause", "--root", str(project.root), *extra])


def _resume(project, capsys) -> tuple[int, dict]:
    with mock.patch.dict("os.environ", ORCHESTRATOR_ENV):
        return _run_cli(capsys, ["resume", "--root", str(project.root)])


def test_pause_blocks_projection_and_resume_restores_ready(lifecycle_project, capsys, monkeypatch):
    """pause -> list-ready/list-gated/continue all emit operator_paused (no dispatch, no spawn);
    resume -> the same dispatch_next_slice_plan_mode candidate is ready again."""
    project = lifecycle_project
    _clear_ao_identity(monkeypatch)
    seed_dispatchable_close(project, monkeypatch)
    _clear_ao_identity(monkeypatch)

    code, before = _run_cli(capsys, ["list-ready", "--root", str(project.root)])
    assert code == 0, before
    assert before["result"] == "ready"
    assert "p-close-1" in before["candidates"]

    pexit, ppayload = _pause(
        project, capsys, monkeypatch, "--reason", "migration freeze", "--set-by", "operator"
    )
    assert pexit == 0, ppayload
    assert ppayload["result"] == "operator_paused"
    assert ppayload["operator_pause"]["paused"] is True

    for argv in (["list-ready"], ["list-gated"], ["continue", "--proposal-id", "p-close-1"]):
        with mock.patch("ao_state_writer.continuation.subprocess.run") as run_mock:
            code, payload = _run_cli(capsys, [argv[0], "--root", str(project.root)] + argv[1:])
        assert code == 3, (argv, payload)
        assert payload["result"] == "operator_paused", (argv, payload)
        assert "candidates" not in payload, (argv, payload)
        run_mock.assert_not_called()  # the pause gate sits UPSTREAM of any spawn

    rexit, rpayload = _resume(project, capsys)
    assert rexit == 0, rpayload
    assert rpayload["result"] == "operator_resumed"
    assert rpayload["was_paused"] is True

    code, after = _run_cli(capsys, ["list-ready", "--root", str(project.root)])
    assert code == 0, after
    assert after["result"] == "ready"
    assert after["candidates"] == before["candidates"]


def test_pause_resume_require_orchestrator_caller_proof(lifecycle_project, capsys, monkeypatch):
    """A worker-type caller (or missing proof) cannot pause/resume; fail closed, no state written.

    WHY: pausing/halting the whole project is an orchestrator-owner-proxy authority; a worker LLM
    must not be able to self-pause or self-resume.
    """
    project = lifecycle_project
    _clear_ao_identity(monkeypatch)
    seed_dispatchable_close(project, monkeypatch)
    _clear_ao_identity(monkeypatch)
    state_file = _state_paths(project.root)[0]
    state_before = state_file.read_bytes()

    for cmd in ("pause", "resume"):
        monkeypatch.setenv("AO_CALLER_TYPE", "agent")
        code, payload = _run_cli(capsys, [cmd, "--root", str(project.root)])
        monkeypatch.delenv("AO_CALLER_TYPE", raising=False)
        assert code == 3, (cmd, payload)
        assert payload["result"] == "missing_or_stale_orchestrator_proof", (cmd, payload)

    assert state_before == state_file.read_bytes(), (
        "worker-caller pause/resume must not mutate state"
    )


def test_operator_pause_survives_apply(lifecycle_project, capsys, monkeypatch):
    """operator_pause is a passthrough top-level key: an intervening apply() (which advances
    state_revision) must NOT drop it. No-bump pause is orthogonal metadata, not a hard freeze."""
    project = lifecycle_project
    _clear_ao_identity(monkeypatch)
    writer = seed_dispatchable_close(project, monkeypatch)
    _clear_ao_identity(monkeypatch)

    rev_pre_pause = writer._read_state().get("state_revision")
    _pause(project, capsys, monkeypatch, "--reason", "freeze")
    rev_before = writer._read_state().get("state_revision")
    assert rev_before == rev_pre_pause, "pause must NOT bump state_revision"

    after = writer.apply(
        make_proposal(
            proposal_id="p-after-pause",
            target_id="chapter-2",
            base_state_revision=rev_before,
            actor_role="worker",
            evidence_refs=["session-log#chapter-2"],
        )
    )
    assert after.decision == "accepted", after
    state = writer._read_state()
    assert state.get("state_revision") > rev_before
    assert state.get("operator_pause", {}).get("paused") is True


def test_operator_paused_is_a_distinct_typed_envelope(lifecycle_project, capsys, monkeypatch):
    """operator_paused must be its OWN result code with the full diagnostic envelope — NOT folded
    into the content-exhaustion final-convergence parking results: it is operator-invokable, not
    content-exhaustion-gated, and its only repair action is operator_resume."""
    project = lifecycle_project
    _clear_ao_identity(monkeypatch)
    seed_dispatchable_close(project, monkeypatch)
    _clear_ao_identity(monkeypatch)
    _pause(project, capsys, monkeypatch)

    code, payload = _run_cli(capsys, ["list-ready", "--root", str(project.root)])
    assert code == 3, payload
    assert payload["result"] == "operator_paused"
    assert payload["allowed_repair_actions"] == ["operator_resume"]
    assert "active_dispatch_leases" in payload  # in-flight lease diagnostic present
    assert "forbidden_actions" in payload


def test_malformed_operator_pause_fails_closed(lifecycle_project, capsys, monkeypatch):
    """A corrupt operator_pause record fails closed to a typed envelope, never silently ignored
    (which would let a paused project be treated as ready)."""
    project = lifecycle_project
    _clear_ao_identity(monkeypatch)
    writer = seed_dispatchable_close(project, monkeypatch)
    _clear_ao_identity(monkeypatch)

    with writer._single_flight():
        state = writer._read_state()
        state["operator_pause"] = {"paused": "yes-but-not-a-bool"}
        writer._write_state(state)

    code, payload = _run_cli(capsys, ["list-ready", "--root", str(project.root)])
    assert code == 3, payload
    assert payload["result"] == "operator_pause_malformed"


def test_pause_blocks_side_effectful_reconcile_leases(lifecycle_project, capsys, monkeypatch):
    """Pause = no canonical mutation except resume: reconcile-leases --apply (which bypasses
    shared preflight) carries its own operator_pause guard and is blocked while paused."""
    project = lifecycle_project
    _clear_ao_identity(monkeypatch)
    seed_dispatchable_close(project, monkeypatch)
    _clear_ao_identity(monkeypatch)
    _pause(project, capsys, monkeypatch)

    code, payload = _run_cli(capsys, ["reconcile-leases", "--root", str(project.root), "--apply"])
    assert code == 3, payload
    assert payload["result"] == "operator_paused"


def test_pause_blocks_all_preflight_bypassing_canonical_writes(lifecycle_project, capsys, monkeypatch):
    """The canonical-write commands that bypass shared preflight (record-final-convergence,
    reconcile-spawn-attestation, reconcile-spawned-dispatch) each carry their OWN operator_pause
    guard, so while paused they fail closed to operator_paused with NO state mutation. Without the
    guard these would still promote spawned_unattested->spawned, delete dispatch leases, or
    refresh dispatch time mid-pause (the two spawn-reconcile bypass holes the LIVE review caught)."""
    project = lifecycle_project
    _clear_ao_identity(monkeypatch)
    seed_dispatchable_close(project, monkeypatch)
    _clear_ao_identity(monkeypatch)
    state_file = _state_paths(project.root)[0]
    _pause(project, capsys, monkeypatch)
    paused_bytes = state_file.read_bytes()

    for argv in (
        ["record-final-convergence", "--proposal-id", "p-close-1", "--evidence", "e#1"],
        ["reconcile-spawn-attestation", "--proposal-id", "p-close-1", "--evidence", "e#1"],
        ["reconcile-spawned-dispatch", "--proposal-id", "p-close-1", "--evidence", "e#1"],
    ):
        code, payload = _run_cli(capsys, [argv[0], "--root", str(project.root)] + argv[1:])
        assert code == 3, (argv, payload)
        assert payload["result"] == "operator_paused", (argv, payload)

    assert paused_bytes == state_file.read_bytes(), (
        "paused canonical-write commands must not mutate state"
    )


def test_resume_when_not_paused_is_noop(lifecycle_project, capsys, monkeypatch):
    """resume on an unpaused project is a safe no-op (was_paused False); projection still works."""
    project = lifecycle_project
    _clear_ao_identity(monkeypatch)
    seed_dispatchable_close(project, monkeypatch)
    _clear_ao_identity(monkeypatch)

    code, payload = _resume(project, capsys)
    assert code == 0, payload
    assert payload["result"] == "operator_resumed"
    assert payload["was_paused"] is False

    code, ready = _run_cli(capsys, ["list-ready", "--root", str(project.root)])
    assert code == 0, ready
    assert ready["result"] == "ready"
