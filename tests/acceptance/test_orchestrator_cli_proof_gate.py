"""End-to-end CLI proof for the orchestrator-CLI-proof gate (Group E slice "f").

WHY THIS EXISTS (the spec, not just a check)
Three side-effectful entrypoints must fail closed unless the caller is a proven LIVE orchestrator:
  1. ``escalated-review-actuate`` (the command) — gates before reading/running the review bridge;
  2. ``_claim_and_run_escalated_review_actuator_cli`` (the receipt-minting helper) — re-asserts the gate
     itself (defense-in-depth), because it is ALSO reached from the authorized ``continue`` auto-spawn
     path, so a non-orchestrator caller cannot mint an actuator receipt even by bypassing the outer gate;
  3. ``watchdog --apply-timeout-blocker`` — gates AFTER preflight self-exempts the resolving stall and
     BEFORE the cli internally elevates to caller_type=watchdog for ``writer.apply``.

The proof gate (``missing_or_stale_orchestrator_proof``, exit 3) is the cli-preflight layer; it
complements — does not duplicate — the writer-level ``non_orchestrator_caller`` guard on state-write
methods.
"""

from __future__ import annotations

import json

from conftest import _state_paths

from ao_state_writer import cli
from ao_state_writer.writer import StateTransitionProposal

TARGET = "chapter-1"
ORCHESTRATOR_ENV = {"AO_CALLER_TYPE": "orchestrator", "AO_SESSION_ID": "example-orchestrator"}


def _proposal(**overrides) -> StateTransitionProposal:
    base = dict(
        proposal_id="p-default",
        target_kind="small_chapter",
        target_id=TARGET,
        base_state_revision=0,
        requested_state="evidence_pending",
        actor_role="implementer",
        evidence_refs=["evidence#1"],
    )
    base.update(overrides)
    return StateTransitionProposal(**base)


def _read_state(project) -> dict:
    state_path, _ = _state_paths(project.root)
    return json.loads(state_path.read_text(encoding="utf-8"))


def _write_state(project, state: dict) -> None:
    state_path, _ = _state_paths(project.root)
    state_path.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")


# --- site 1: the actuate command --------------------------------------------------------------

def test_escalated_actuate_without_orchestrator_fails_closed(lifecycle_project):
    project = lifecycle_project
    res = project.cli(
        "escalated-review-actuate", "--root", str(project.root), "--proposal-id", "p-x"
    )
    assert res.returncode == 3, (res.returncode, res.stdout, res.stderr)
    payload = json.loads(res.stdout)
    assert payload["result"] == "missing_or_stale_orchestrator_proof", payload
    assert payload["proposal_id"] == "p-x", payload


def test_escalated_actuate_with_orchestrator_passes_the_gate(lifecycle_project):
    # WITH the orchestrator identity the proof gate OPENS: the path proceeds past it (and then stops at
    # the next contract-driven check), so the result is NOT the proof failure — proving the gate passed.
    project = lifecycle_project
    res = project.cli(
        "escalated-review-actuate", "--root", str(project.root), "--proposal-id", "p-x",
        **ORCHESTRATOR_ENV,
    )
    payload = json.loads(res.stdout)
    assert payload.get("result") != "missing_or_stale_orchestrator_proof", payload


# --- site 2: the receipt-minting helper (defense-in-depth) ------------------------------------

def test_actuator_helper_defense_in_depth_fails_closed(lifecycle_project, monkeypatch, capsys):
    # Reach the receipt-minting helper directly (the path a `continue` auto-spawn also takes) with NO
    # orchestrator identity: the helper's own proof re-check must fail closed BEFORE it touches state.
    project = lifecycle_project
    monkeypatch.delenv("AO_CALLER_TYPE", raising=False)
    _, ledger_path = _state_paths(project.root)
    rc = cli._claim_and_run_escalated_review_actuator_cli(
        root=project.root,
        writer=project.writer(),
        ledger_path=ledger_path,
        proposal_id="p-x",
        bridge_command="echo hi",
        bridge_timeout_seconds=10,
    )
    assert rc == 3
    out = json.loads(capsys.readouterr().out)
    assert out["result"] == "missing_or_stale_orchestrator_proof", out
    assert out["proposal_id"] == "p-x", out


# --- site 3: the watchdog timeout-blocker apply -----------------------------------------------

def test_watchdog_apply_without_orchestrator_fails_closed(lifecycle_project, tmp_path):
    project = lifecycle_project
    # Stall a spawned codex_cc review (same setup as the slice-a loop test).
    writer = project.writer()
    seed = writer.apply(_proposal(proposal_id="p-seed-1"))
    assert seed.next_required_action == "codex_cc_review"
    spawned = project.cli("continue", "--root", str(project.root))
    assert json.loads(spawned.stdout)["result"] == "spawned"
    state = _read_state(project)
    state["dispatched_proposals"]["p-seed-1"]["time"] = "2000-01-01T00:00:00+00:00"
    _write_state(project, state)

    observation = json.loads(project.cli("list-ready", "--root", str(project.root)).stdout)[
        "review_watchdog_observation"
    ]
    obs_path = tmp_path / "obs.json"
    obs_path.write_text(json.dumps(observation), encoding="utf-8")

    # WITHOUT orchestrator identity: preflight self-exempts the resolving stall (so it does not block),
    # then the proof gate blocks the apply. Nested-payload shape (codex confirm criterion).
    applied = project.cli(
        "watchdog", "--root", str(project.root), "--observation", str(obs_path),
        "--apply-timeout-blocker",
    )
    assert applied.returncode == 3, (applied.returncode, applied.stdout, applied.stderr)
    payload = json.loads(applied.stdout)
    assert payload["state_write"] is False, payload
    assert payload["state_writer_preflight"]["result"] == "missing_or_stale_orchestrator_proof", payload
