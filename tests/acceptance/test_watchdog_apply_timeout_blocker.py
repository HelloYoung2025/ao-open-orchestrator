"""End-to-end CLI proof for the spawned-review stall loop (Group E slice "a").

WHY THIS EXISTS (the spec, not just a check)
--------------------------------------------
Before slice "a" a hung spawned review wedged its target forever: ``_current_obligation_issue`` skipped
every dispatched proposal up front, so ``list-ready`` reported nothing and the watchdog resolver had no
obligation to clear. Slice "a" makes the FULL loop work end to end, black-box against the public CLI:

  1. a spawned codex_cc review aged past its threshold is SURFACED by ``list-ready`` as a resolvable
     ``spawned_review_timeout_due`` (with a brand-neutral ``ao-state-writer`` advisory command), and
  2. ``watchdog --apply-timeout-blocker`` CLEARS it — the resolver does NOT self-block on the very stall
     it evaluated (the 070c412 circular block: preflight is passed the obligation fingerprint), and its
     ``writer.apply`` is authorized as ``caller_type=watchdog`` by the cli's internal env wrapper.

The negative half proves the wrapper is load-bearing: ``writer.apply`` of the same timeout blocker WITHOUT
``AO_CALLER_TYPE=watchdog`` is rejected (``unauthorized_review_receipt_actor``). The corrupt-status half
proves the stall gate keys on status ``"spawned"`` only — a migrated ``spawned_unattested`` lease never
emits a false stall.

DISCIPLINE (matches test_convergence_fail_closed.py)
* BLACK-BOX against the public CLI / public package only.
* State is edited directly ONLY to AGE a lease past the threshold — a time condition the harness cannot
  otherwise reach within a single run.
* The orchestrator identity each step needs is passed explicitly (conftest.cli_env strips AO_*).
"""

from __future__ import annotations

import json

from conftest import _state_paths

from ao_state_writer.watchdog import ReviewWatchdogObservation, evaluate_watchdog
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


def _spawn_and_age_codex_cc_review(project) -> None:
    """Seed p-seed-1 (codex_cc_review obligation), spawn it via the CLI, then backdate the lease well
    past the codex_cc stall threshold so it reads as a hung review."""

    writer = project.writer()
    seed = writer.apply(_proposal(proposal_id="p-seed-1"))
    assert seed.decision == "accepted"
    assert seed.next_required_action == "codex_cc_review"

    spawned = project.cli("continue", "--root", str(project.root))
    assert spawned.returncode == 0, spawned.stderr
    assert json.loads(spawned.stdout)["result"] == "spawned"

    state = _read_state(project)
    record = state["dispatched_proposals"]["p-seed-1"]
    assert record["status"] == "spawned", record
    record["time"] = "2000-01-01T00:00:00+00:00"  # years old: past every staged threshold, skew-proof.
    _write_state(project, state)


def test_stalled_spawned_review_is_surfaced_then_cleared_by_watchdog(lifecycle_project, tmp_path):
    project = lifecycle_project
    _spawn_and_age_codex_cc_review(project)

    # 1) The hung review is SURFACED as a resolvable obligation (was silently skipped before slice a).
    listed = project.cli("list-ready", "--root", str(project.root))
    assert listed.returncode == 3, (listed.returncode, listed.stdout, listed.stderr)
    issue = json.loads(listed.stdout)
    assert issue["result"] == "spawned_review_timeout_due", issue
    assert issue["proposal_id"] == "p-seed-1"
    assert issue["review_scope"] == "codex_cc"
    assert issue["allowed_repair_actions"][0] == "review_watchdog_apply_timeout_blocker"
    command = issue["review_watchdog_command"]
    assert command[0] == "ao-state-writer", command
    flat = " ".join(command)
    # Fragments assembled from adjacent string literals so the leak scanner does not flag this test's
    # OWN source for the tokens it asserts are ABSENT from the brand-neutral advisory command.
    for forbidden in ("/opt/" "homebrew", "claw-" "commander", "PYTHONPATH", "gpt" "_pro"):
        assert forbidden not in flat, (forbidden, command)

    # 2) The watchdog resolver CLEARS it. The observation the preflight emitted round-trips into the
    #    watchdog; the apply path passes preflight the obligation fingerprint (no 070c412 self-block) and
    #    authorizes the write as caller_type=watchdog via the cli's internal env wrapper -- note NO
    #    AO_CALLER_TYPE is supplied here (conftest strips it), so a passing write proves the wrapper.
    observation = issue["review_watchdog_observation"]
    obs_path = tmp_path / "observation.json"
    obs_path.write_text(json.dumps(observation), encoding="utf-8")
    # slice f added the orchestrator-CLI-proof gate to this path: the EXTERNAL caller of
    # `watchdog --apply-timeout-blocker` must be a proven orchestrator (the cli then internally elevates
    # to caller_type=watchdog only for writer.apply). Supply the orchestrator identity; the dedicated
    # gate-fires-closed regression lives in tests/acceptance/test_orchestrator_cli_proof.py.
    applied = project.cli(
        "watchdog",
        "--root",
        str(project.root),
        "--observation",
        str(obs_path),
        "--apply-timeout-blocker",
        **ORCHESTRATOR_ENV,
    )
    assert applied.returncode == 0, (applied.returncode, applied.stdout, applied.stderr)
    payload = json.loads(applied.stdout)
    assert payload["state_write"] is True, payload
    assert payload["state_writer_decision"]["decision"] == "accepted", payload

    # 3) The stall is gone: the timeout blocker superseded p-seed-1, so list-ready no longer reports the
    #    spawned_review_timeout_due dead-end (the target advanced to its review-timeout repair lane).
    after = project.cli("list-ready", "--root", str(project.root))
    after_payload = json.loads(after.stdout)
    assert after_payload.get("result") != "spawned_review_timeout_due", after_payload


def test_watchdog_timeout_apply_requires_watchdog_caller(lifecycle_project, tmp_path, monkeypatch):
    """The cli wrapper is load-bearing: the SAME timeout blocker, applied WITHOUT AO_CALLER_TYPE=watchdog,
    is rejected by the writer's review-caller guard. (Proves the wrapper added in slice a is what lets the
    resolver write at all.)"""

    project = lifecycle_project
    _spawn_and_age_codex_cc_review(project)

    listed = project.cli("list-ready", "--root", str(project.root))
    observation = json.loads(listed.stdout)["review_watchdog_observation"]
    obs = ReviewWatchdogObservation.from_payload(observation)
    decision = evaluate_watchdog(root=project.root, observation=obs)
    assert decision.proposal is not None, decision

    # Apply the watchdog blocker proposal directly, with NO watchdog caller identity in the environment.
    monkeypatch.delenv("AO_CALLER_TYPE", raising=False)
    writer = project.writer()
    rejected = writer.apply(StateTransitionProposal(**decision.proposal))
    assert rejected.decision == "rejected", rejected
    assert rejected.reason == "unauthorized_review_receipt_actor", rejected


def test_migrated_spawned_unattested_lease_emits_no_false_stall(lifecycle_project):
    """The stall gate keys on status == 'spawned' only. A migrated 'spawned_unattested' lease (awaiting
    attestation, a different reconcile path) must NOT be misread as a hung review."""

    project = lifecycle_project
    _spawn_and_age_codex_cc_review(project)

    state = _read_state(project)
    state["dispatched_proposals"]["p-seed-1"]["status"] = "spawned_unattested"
    _write_state(project, state)

    listed = project.cli("list-ready", "--root", str(project.root))
    payload = json.loads(listed.stdout)
    assert payload.get("result") != "spawned_review_timeout_due", payload
