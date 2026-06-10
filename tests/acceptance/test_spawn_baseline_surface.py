"""End-to-end CLI proof for the spawn-baseline obligation surface (Group E slice "b1+b2").

WHY THIS EXISTS (the spec, not just a check)
A worker spawned on a worktree that cannot prove the required source baseline leaves a real dispatch
lease whose work is on the wrong code. ``record_spawn_baseline_issue`` consumes that lease
(``spawned_base_mismatch`` / ``spawned_base_unverified``); ``_current_obligation_issue`` must then
SURFACE it as an owner-visible obligation (forbidden: spawn) instead of either (a) silently skipping
it via the ``is_dispatched`` consume-skip, or (b) re-spawning it on the known-bad baseline.

The masking test pins the ORDERING fix from the codex pre-implementation consult: the baseline branch
runs BEFORE the action/env-escalation routing. Without that ordering, a baseline lease whose stored
action is ``review_environment_unavailable`` would be MASKED by the environment-escalation payload and
the baseline obligation would never surface.

DISCIPLINE (matches the other acceptance suites)
* BLACK-BOX against the public CLI / public package only.
* State is edited directly ONLY to put the lease into the spawn-baseline status a single harness run
  cannot otherwise reach (the b3 detection call site is not wired yet) — the same "reach a state the
  harness cannot otherwise produce" allowance the slice-a stall suite uses to age a lease.
"""

from __future__ import annotations

import json

from conftest import _state_paths

from ao_state_writer.writer import StateTransitionProposal

TARGET = "chapter-1"


def _proposal(**overrides) -> StateTransitionProposal:
    base = dict(
        proposal_id="p-seed-1",
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


def _spawn_then_force_baseline_mismatch(project) -> None:
    """Seed p-seed-1, spawn it via the CLI, then rewrite its dispatch lease into a proven
    spawn-baseline commit mismatch (the status the b3 detection call site will write once wired)."""
    writer = project.writer()
    seed = writer.apply(_proposal())
    assert seed.decision == "accepted"
    assert seed.next_required_action == "codex_cc_review"

    spawned = project.cli("continue", "--root", str(project.root))
    assert spawned.returncode == 0, spawned.stderr
    assert json.loads(spawned.stdout)["result"] == "spawned"

    state = _read_state(project)
    record = state["dispatched_proposals"]["p-seed-1"]
    assert record["status"] == "spawned"
    record["status"] = "spawned_base_mismatch"
    record["baseline_attestation_result"] = "spawn_base_commit_mismatch"
    record["reason"] = "spawn_base_commit_mismatch"
    record["required_source_commit"] = "aaaa111"
    record["worker_head"] = "bbbb222"
    _write_state(project, state)


def test_recorded_spawn_baseline_lease_is_surfaced_as_obligation(lifecycle_project):
    project = lifecycle_project
    _spawn_then_force_baseline_mismatch(project)

    # The consumed baseline lease is SURFACED (not silently skipped by the is_dispatched consume-skip).
    listed = project.cli("list-ready", "--root", str(project.root))
    assert listed.returncode == 3, (listed.returncode, listed.stdout, listed.stderr)
    issue = json.loads(listed.stdout)
    assert issue["result"] == "spawn_base_commit_mismatch", issue
    assert issue["proposal_id"] == "p-seed-1", issue
    assert issue["current_obligation"] is True, issue
    assert issue["forbidden_actions"] == ["spawn"], issue
    assert issue["allowed_repair_actions"] == ["orchestrator_spawn_baseline_repair"], issue

    # And it is NOT re-spawned: the lease is consumed, so a fresh claim is refused.
    assert project.writer().claim_dispatch("p-seed-1") is False


def test_spawn_baseline_branch_precedes_env_escalation_masking(lifecycle_project):
    # The ordering regression test: a baseline lease whose stored action is an environment-escalation
    # action MUST still surface the baseline obligation (the baseline branch runs before the env
    # branch). If the branch were placed after env routing, list-ready would return
    # review_environment_unavailable and the baseline obligation would be masked.
    project = lifecycle_project
    _spawn_then_force_baseline_mismatch(project)

    state = _read_state(project)
    state["proposal_results"]["p-seed-1"]["next_required_action"] = "review_environment_unavailable"
    _write_state(project, state)

    listed = project.cli("list-ready", "--root", str(project.root))
    assert listed.returncode == 3, (listed.returncode, listed.stdout, listed.stderr)
    issue = json.loads(listed.stdout)
    assert issue["result"] == "spawn_base_commit_mismatch", issue
    assert issue["result"] != "review_environment_unavailable", issue


def test_unverified_baseline_lease_surfaces_as_unverified(lifecycle_project):
    # A non-mismatch reason surfaces conservatively as spawn_baseline_unverified (still forbidden spawn).
    project = lifecycle_project
    writer = project.writer()
    writer.apply(_proposal())
    project.cli("continue", "--root", str(project.root))

    state = _read_state(project)
    record = state["dispatched_proposals"]["p-seed-1"]
    record["status"] = "spawned_base_unverified"
    record["baseline_attestation_result"] = "worker_worktree_missing"
    record["reason"] = "worker_worktree_missing"
    _write_state(project, state)

    listed = project.cli("list-ready", "--root", str(project.root))
    assert listed.returncode == 3, (listed.returncode, listed.stdout, listed.stderr)
    issue = json.loads(listed.stdout)
    assert issue["result"] == "spawn_baseline_unverified", issue
    assert issue["forbidden_actions"] == ["spawn"], issue
