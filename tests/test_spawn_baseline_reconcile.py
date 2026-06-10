"""Unit tests for StateWriter.reconcile_spawned_dispatch spawn-baseline RELEASE (Group E slice "b3a").

WHY THIS MATTERS (intent, not just behavior)
b1+b2 made a bad-baseline spawn a CONSUMED + owner-surfaced obligation, but it was not yet
auto-releasable, so an unattended orchestrator would stay wedged on it. b3a makes the writer release
that lease under terminal proof so dispatch can retry on a CORRECT baseline:

  * spawned_base_mismatch (a PROVEN wrong-baseline worker) releases ONLY once the offending session is
    gone — either a stored ``terminated`` session whose kill readback is ``absent``, or a live
    ``terminal_readback`` of ``absent``. Otherwise it fails closed requiring a termination repair, so we
    never delete a lease while its worker may still be mutating the wrong tree.
  * spawned_base_unverified (baseline could not be verified) releases under --release-if-terminal plus an
    ``absent`` terminal_readback; otherwise it fails closed.

The release is action-gated by the SAME SPAWNED_DISPATCH_RECONCILE_ACTIONS set LIVE uses (via the
projected effective action): a review/actuator-class lease is NEVER released, because deleting one
could let a second actuator double-submit. The normal ``spawned`` path is deliberately left on the
public raw-action model and must keep working unchanged. Mirrors LIVE writer.py:1283-1374.

The live terminal_readback is produced by the CLI ao-session kill readback in a LATER slice; these
tests exercise the writer layer directly with synthetic readback / stored termination.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ao_state_writer.writer import StateWriter


def _writer(root: Path) -> StateWriter:
    base = root / ".omx" / "state" / "ao-state-writer"
    base.mkdir(parents=True, exist_ok=True)
    return StateWriter(state_path=base / "state.json", ledger_path=base / "state-transitions.jsonl")


def _write_contract(root: Path) -> None:
    root.joinpath("DIRECT_PROJECT_CONTRACT.toml").write_text(
        f"""
version = 1

[ao_clone_isolation]
active_root = "{root.as_posix()}"

[owner_proxy]
project_id = "example-project"

[continuation_policy]
orchestrator_session = "example-orchestrator"
""".lstrip(),
        encoding="utf-8",
    )


def _orchestrator_env(monkeypatch) -> None:
    monkeypatch.setenv("AO_CALLER_TYPE", "orchestrator")
    monkeypatch.setenv("AO_SESSION_ID", "example-orchestrator")


def _seed(root: Path, *, pid: str, tid: str, status: str, action: str, **lease_extra) -> None:
    """Seed a base-status (or normal spawned) lease + its proposal_result + a ledger row mapping
    proposal_id -> target_id, so reconcile can resolve the effective action for its release gate."""
    base = root / ".omx" / "state" / "ao-state-writer"
    base.mkdir(parents=True, exist_ok=True)
    record = {"status": status, "spawn_session_id": "sess-1", "time": "2000-01-01T00:00:00+00:00"}
    record.update(lease_extra)
    state = {
        "schema_version": 1,
        "state_revision": 1,
        "targets": {tid: {"state": "evidence_pending", "repair_attempts": {}}},
        "proposal_results": {
            pid: {"decision": "accepted", "next_required_action": action, "new_state": "evidence_pending"}
        },
        "dispatched_proposals": {pid: record},
    }
    (base / "state.json").write_text(json.dumps(state), encoding="utf-8")
    (base / "state-transitions.jsonl").write_text(
        json.dumps({"proposal": {"proposal_id": pid, "target_id": tid}, "decision": {}}) + "\n",
        encoding="utf-8",
    )


def _dispatched(root: Path) -> dict:
    base = root / ".omx" / "state" / "ao-state-writer"
    return json.loads((base / "state.json").read_text(encoding="utf-8")).get("dispatched_proposals", {})


def _reconciliations(root: Path, pid: str) -> list:
    base = root / ".omx" / "state" / "ao-state-writer"
    state = json.loads((base / "state.json").read_text(encoding="utf-8"))
    return state.get("spawned_dispatch_reconciliations", {}).get(pid, [])


# --- mismatch -------------------------------------------------------------------------------------

def test_mismatch_released_by_live_terminal_readback(tmp_path: Path, monkeypatch) -> None:
    _write_contract(tmp_path)
    _orchestrator_env(monkeypatch)
    _seed(tmp_path, pid="p1", tid="t1", status="spawned_base_mismatch", action="repair_active")
    result = _writer(tmp_path).reconcile_spawned_dispatch(
        proposal_id="p1",
        evidence_refs=["orchestrator:reconcile"],
        release_if_terminal=True,
        terminal_readback={"result": "absent"},
    )
    assert result["decision"] == "released", result
    assert result["outcome"] == "released_spawn_baseline_mismatch_terminal_readback", result
    assert "p1" not in _dispatched(tmp_path)  # lease deleted so dispatch can retry
    audit = _reconciliations(tmp_path, "p1")[-1]
    assert audit["outcome"] == "released_spawn_baseline_mismatch_terminal_readback"
    assert audit["terminal_readback"] == {"result": "absent"}


def test_mismatch_released_by_stored_terminated_session(tmp_path: Path, monkeypatch) -> None:
    _write_contract(tmp_path)
    _orchestrator_env(monkeypatch)
    _seed(
        tmp_path,
        pid="p1",
        tid="t1",
        status="spawned_base_mismatch",
        action="repair_active",
        spawn_session_termination={"result": "terminated", "readback": {"result": "absent"}},
    )
    result = _writer(tmp_path).reconcile_spawned_dispatch(
        proposal_id="p1", evidence_refs=["e"], release_if_terminal=True
    )
    assert result["decision"] == "released", result
    assert result["outcome"] == "released_spawn_baseline_mismatch_terminated", result
    assert "p1" not in _dispatched(tmp_path)


def test_mismatch_rejected_without_terminal_proof(tmp_path: Path, monkeypatch) -> None:
    # No live readback and no stored terminated session => we cannot prove the worker is gone, so the
    # lease must NOT be deleted (the worker may still be mutating the wrong tree).
    _write_contract(tmp_path)
    _orchestrator_env(monkeypatch)
    _seed(tmp_path, pid="p1", tid="t1", status="spawned_base_mismatch", action="repair_active")
    result = _writer(tmp_path).reconcile_spawned_dispatch(
        proposal_id="p1", evidence_refs=["e"], release_if_terminal=True
    )
    assert result["decision"] == "rejected", result
    assert result["reason"] == "spawn_baseline_mismatch_requires_termination_repair", result
    assert "p1" in _dispatched(tmp_path)  # lease preserved


def test_mismatch_refresh_time_never_refreshes_base_lease(tmp_path: Path, monkeypatch) -> None:
    # A base lease is never re-stamped: refresh_time on a mismatch falls through to the termination
    # requirement (release_if_terminal is False), proving base leases never reach the refresh path.
    _write_contract(tmp_path)
    _orchestrator_env(monkeypatch)
    _seed(tmp_path, pid="p1", tid="t1", status="spawned_base_mismatch", action="repair_active")
    result = _writer(tmp_path).reconcile_spawned_dispatch(
        proposal_id="p1", evidence_refs=["e"], refresh_time=True
    )
    assert result["decision"] == "rejected", result
    assert result["reason"] == "spawn_baseline_mismatch_requires_termination_repair", result


# --- unverified -----------------------------------------------------------------------------------

def test_unverified_released_by_terminal_readback(tmp_path: Path, monkeypatch) -> None:
    _write_contract(tmp_path)
    _orchestrator_env(monkeypatch)
    _seed(tmp_path, pid="p1", tid="t1", status="spawned_base_unverified", action="repair_active")
    result = _writer(tmp_path).reconcile_spawned_dispatch(
        proposal_id="p1",
        evidence_refs=["e"],
        release_if_terminal=True,
        terminal_readback={"result": "absent"},
    )
    assert result["decision"] == "released", result
    assert result["outcome"] == "released_spawn_baseline_unverified", result
    assert "p1" not in _dispatched(tmp_path)


def test_unverified_rejected_without_release_flag(tmp_path: Path, monkeypatch) -> None:
    _write_contract(tmp_path)
    _orchestrator_env(monkeypatch)
    _seed(tmp_path, pid="p1", tid="t1", status="spawned_base_unverified", action="repair_active")
    result = _writer(tmp_path).reconcile_spawned_dispatch(
        proposal_id="p1", evidence_refs=["e"], refresh_time=True
    )
    assert result["decision"] == "rejected", result
    assert result["reason"] == "spawn_baseline_unverified_requires_release_repair", result


def test_unverified_rejected_when_readback_not_absent(tmp_path: Path, monkeypatch) -> None:
    _write_contract(tmp_path)
    _orchestrator_env(monkeypatch)
    _seed(tmp_path, pid="p1", tid="t1", status="spawned_base_unverified", action="repair_active")
    result = _writer(tmp_path).reconcile_spawned_dispatch(
        proposal_id="p1",
        evidence_refs=["e"],
        release_if_terminal=True,
        terminal_readback={"result": "active"},
    )
    assert result["decision"] == "rejected", result
    assert result["reason"] == "spawn_baseline_unverified_requires_terminal_readback", result
    assert "p1" in _dispatched(tmp_path)


# --- gates ----------------------------------------------------------------------------------------

def test_review_action_base_lease_is_not_released(tmp_path: Path, monkeypatch) -> None:
    # A base lease whose effective action is a review/actuator obligation (NOT in
    # SPAWNED_DISPATCH_RECONCILE_ACTIONS) is never released — deleting it could let a second actuator
    # double-submit. This is the same protection the normal spawned path applies.
    _write_contract(tmp_path)
    _orchestrator_env(monkeypatch)
    _seed(tmp_path, pid="p1", tid="t1", status="spawned_base_mismatch", action="codex_cc_review")
    result = _writer(tmp_path).reconcile_spawned_dispatch(
        proposal_id="p1",
        evidence_refs=["e"],
        release_if_terminal=True,
        terminal_readback={"result": "absent"},
    )
    assert result["decision"] == "rejected", result
    assert result["reason"] == "not_spawned_dispatch_reconcile_action", result
    assert result["next_required_action"] == "codex_cc_review", result
    assert "p1" in _dispatched(tmp_path)


def test_non_orchestrator_caller_rejected_for_base_lease(tmp_path: Path, monkeypatch) -> None:
    _write_contract(tmp_path)
    monkeypatch.delenv("AO_CALLER_TYPE", raising=False)
    monkeypatch.delenv("AO_SESSION_ID", raising=False)
    _seed(tmp_path, pid="p1", tid="t1", status="spawned_base_mismatch", action="repair_active")
    result = _writer(tmp_path).reconcile_spawned_dispatch(
        proposal_id="p1",
        evidence_refs=["e"],
        release_if_terminal=True,
        terminal_readback={"result": "absent"},
    )
    assert result["decision"] == "rejected", result
    assert result["reason"] == "non_orchestrator_caller", result
    assert "p1" in _dispatched(tmp_path)


def test_normal_spawned_lease_path_unaffected(tmp_path: Path, monkeypatch) -> None:
    # The widened status check + base block must NOT intercept a normal "spawned" lease: --refresh-time
    # still re-stamps it via the existing public path.
    _write_contract(tmp_path)
    _orchestrator_env(monkeypatch)
    _seed(tmp_path, pid="p1", tid="t1", status="spawned", action="repair_active")
    result = _writer(tmp_path).reconcile_spawned_dispatch(
        proposal_id="p1", evidence_refs=["e"], refresh_time=True
    )
    assert result["decision"] == "refreshed", result
    assert _dispatched(tmp_path)["p1"]["status"] == "spawned"  # still a live, refreshed lease
