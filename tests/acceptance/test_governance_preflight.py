"""Acceptance regression for the shared-preflight governance gate (Group E sub-slice E3).

WHY (intent, not just behavior): an uncommitted change to a governance file makes the working tree
non-authoritative — dispatching off it risks acting on un-reviewed control-plane edits. Group C already
wired find_governance_blockers into evaluate_continuation, so ``continue``/``dispatch`` failed closed;
but ``list-ready``/``list-gated``/``reconcile-once`` reach the obligation state through
preflight_reconcile WITHOUT the continuation, so before E3 they silently ignored a dirty governance
tree and could surface a "ready" obligation a human had not reviewed. E3 moves the check into the
SHARED preflight gate so every entrypoint fails closed identically (parity with LIVE).
"""

from __future__ import annotations

import json


def _dirty_governance(project) -> None:
    # A TOML comment keeps DIRECT_PROJECT_CONTRACT.toml valid (so _validate_contract still passes and
    # we reach the governance check) while making the committed governance file git-dirty.
    contract = project.root / "DIRECT_PROJECT_CONTRACT.toml"
    contract.write_text(contract.read_text(encoding="utf-8") + "\n# uncommitted governance edit\n",
                        encoding="utf-8")


def test_e3_dirty_governance_blocks_all_shared_preflight_entrypoints(lifecycle_project):
    project = lifecycle_project

    # Baseline: a clean tree does NOT block list-ready (the gate is the dirty edit, not the command).
    clean = project.cli("list-ready", "--root", str(project.root))
    assert clean.returncode == 0, (clean.returncode, clean.stdout, clean.stderr)

    _dirty_governance(project)

    for sub in ("list-ready", "list-gated", "reconcile-once"):
        res = project.cli(sub, "--root", str(project.root))
        assert res.returncode == 3, (sub, res.returncode, res.stdout, res.stderr)
        payload = json.loads(res.stdout)
        assert payload["result"] == "blocked", (sub, payload)
        assert any("unrecognized_governance_dirty" in reason for reason in payload["reasons"]), (sub, payload)
        assert payload["allowed_repair_actions"] == ["orchestrator_governance_dirty_review"], (sub, payload)


def test_e3_dirty_governance_blocks_dispatch_before_spawn(lifecycle_project):
    # dispatch goes through the same shared preflight; a dirty governance tree must block it before any
    # claim/spawn side effect (forbidden_actions advertises that dispatch is precluded).
    project = lifecycle_project
    _dirty_governance(project)

    res = project.cli("dispatch", "--proposal-id", "anything", "--root", str(project.root))
    assert res.returncode == 3, (res.returncode, res.stdout, res.stderr)
    payload = json.loads(res.stdout)
    assert payload["result"] == "blocked", payload
    assert "dispatch" in payload["forbidden_actions"], payload
