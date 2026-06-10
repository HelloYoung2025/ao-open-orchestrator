"""Tests for the reconcile-CLI liveness injection ported in Group E slice "b3c"
(cli.cmd_reconcile_spawned_dispatch).

WHY THIS MATTERS (intent, not just behavior)
b3a gave the WRITER the authority to release a bad-baseline (spawned_base_*) lease under a terminal
``terminal_readback``. b3c is the CLI half: for a base-status lease being released (--release-if-terminal),
it performs the live ao-session readback and feeds the result to the writer, so a release happens ONLY
when the worker is proven gone. The load-bearing properties this locks:

  * unverified releases on an ``absent`` readback; a still-``active`` or unverifiable worker fails closed
    (exit 2) and the lease is preserved;
  * mismatch short-circuits to the STORED termination proof when present (no extra shellout), else does the
    live readback with the same absent/active/unverified mapping;
  * the readback is computed ONLY for a base-status lease under --release-if-terminal — a normal ``spawned``
    lease and a base lease WITHOUT --release-if-terminal never shell out to ``ao session ls`` (the writer
    still owns the gating). We prove "no shellout" by monkeypatching ``_confirmed_absent_readback`` to RAISE.

The writer release/refresh authority itself is covered by test_spawn_baseline_reconcile.py; here we drive
through the CLI and monkeypatch the readback so no real ``ao`` engine is needed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from ao_state_writer import cli
from ao_state_writer.writer import StateWriter


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


def _seed(root: Path, *, pid: str, tid: str, status: str, action: str, **record_extra) -> None:
    base = root / ".omx" / "state" / "ao-state-writer"
    base.mkdir(parents=True, exist_ok=True)
    record = {"status": status, "spawn_session_id": "sess-1", "time": "2000-01-01T00:00:00+00:00"}
    record.update(record_extra)
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


def _args(root: Path, pid: str, *, release_if_terminal: bool = False, refresh_time: bool = False):
    return argparse.Namespace(
        root=root,
        proposal_id=pid,
        evidence=["orchestrator:reconcile"],
        release_if_terminal=release_if_terminal,
        refresh_time=refresh_time,
    )


def _dispatched(root: Path) -> dict:
    base = root / ".omx" / "state" / "ao-state-writer"
    return json.loads((base / "state.json").read_text(encoding="utf-8")).get("dispatched_proposals", {})


def _run(root: Path, args, capsys) -> tuple[int, dict]:
    code = cli.cmd_reconcile_spawned_dispatch(args)
    payload = json.loads(capsys.readouterr().out)
    return code, payload


def _boom(*_a, **_k):
    raise AssertionError("_confirmed_absent_readback must NOT be called on this path")


# --- unverified release path ----------------------------------------------------------------------

def test_unverified_released_when_readback_absent(tmp_path, monkeypatch, capsys) -> None:
    _write_contract(tmp_path)
    _orchestrator_env(monkeypatch)
    _seed(tmp_path, pid="p1", tid="t1", status="spawned_base_unverified", action="repair_active")
    monkeypatch.setattr(cli, "_confirmed_absent_readback", lambda sid, pid: {"result": "absent"})
    code, payload = _run(tmp_path, _args(tmp_path, "p1", release_if_terminal=True), capsys)
    assert code == 0, payload
    assert payload["result"] == "spawned_dispatch_terminal_released", payload
    assert "p1" not in _dispatched(tmp_path)  # lease deleted so dispatch retries on a good baseline


def test_unverified_rejected_when_worker_still_active(tmp_path, monkeypatch, capsys) -> None:
    _write_contract(tmp_path)
    _orchestrator_env(monkeypatch)
    _seed(tmp_path, pid="p1", tid="t1", status="spawned_base_unverified", action="repair_active")
    monkeypatch.setattr(cli, "_confirmed_absent_readback", lambda sid, pid: {"result": "active"})
    code, payload = _run(tmp_path, _args(tmp_path, "p1", release_if_terminal=True), capsys)
    assert code == 2, payload
    assert payload["reason"] == "spawn_baseline_unverified_worker_still_active", payload
    assert "p1" in _dispatched(tmp_path)  # preserved — never delete a live worker's lease


def test_unverified_rejected_when_liveness_unverifiable(tmp_path, monkeypatch, capsys) -> None:
    _write_contract(tmp_path)
    _orchestrator_env(monkeypatch)
    _seed(tmp_path, pid="p1", tid="t1", status="spawned_base_unverified", action="repair_active")
    monkeypatch.setattr(cli, "_confirmed_absent_readback", lambda sid, pid: {"result": "failed"})
    code, payload = _run(tmp_path, _args(tmp_path, "p1", release_if_terminal=True), capsys)
    assert code == 2, payload
    assert payload["reason"] == "spawn_baseline_unverified_liveness_unverified", payload
    assert "p1" in _dispatched(tmp_path)


def test_unverified_missing_session_id_rejected(tmp_path, monkeypatch, capsys) -> None:
    _write_contract(tmp_path)
    _orchestrator_env(monkeypatch)
    _seed(
        tmp_path, pid="p1", tid="t1", status="spawned_base_unverified", action="repair_active",
        spawn_session_id="",
    )
    monkeypatch.setattr(cli, "_confirmed_absent_readback", _boom)
    code, payload = _run(tmp_path, _args(tmp_path, "p1", release_if_terminal=True), capsys)
    assert code == 2, payload
    assert payload["reason"] == "spawn_baseline_unverified_missing_session_id", payload


def test_unverified_without_release_flag_does_not_shellout(tmp_path, monkeypatch, capsys) -> None:
    # No --release-if-terminal: the CLI must NOT read back (injection gated on release_if_terminal); the
    # writer then fails closed because unverified release requires the flag.
    _write_contract(tmp_path)
    _orchestrator_env(monkeypatch)
    _seed(tmp_path, pid="p1", tid="t1", status="spawned_base_unverified", action="repair_active")
    monkeypatch.setattr(cli, "_confirmed_absent_readback", _boom)
    code, payload = _run(tmp_path, _args(tmp_path, "p1", refresh_time=True), capsys)
    assert code == 2, payload
    assert payload["reason"] == "spawn_baseline_unverified_requires_release_repair", payload


# --- mismatch release path ------------------------------------------------------------------------

def test_mismatch_released_by_stored_termination_without_shellout(tmp_path, monkeypatch, capsys) -> None:
    # A stored terminated+absent termination is sufficient proof; the CLI must NOT shell out again.
    _write_contract(tmp_path)
    _orchestrator_env(monkeypatch)
    _seed(
        tmp_path, pid="p1", tid="t1", status="spawned_base_mismatch", action="repair_active",
        spawn_session_termination={"result": "terminated", "readback": {"result": "absent"}},
    )
    monkeypatch.setattr(cli, "_confirmed_absent_readback", _boom)
    code, payload = _run(tmp_path, _args(tmp_path, "p1", release_if_terminal=True), capsys)
    assert code == 0, payload
    assert payload["result"] == "spawned_dispatch_terminal_released", payload
    assert "p1" not in _dispatched(tmp_path)


def test_mismatch_released_by_live_readback(tmp_path, monkeypatch, capsys) -> None:
    _write_contract(tmp_path)
    _orchestrator_env(monkeypatch)
    _seed(tmp_path, pid="p1", tid="t1", status="spawned_base_mismatch", action="repair_active")
    monkeypatch.setattr(cli, "_confirmed_absent_readback", lambda sid, pid: {"result": "absent"})
    code, payload = _run(tmp_path, _args(tmp_path, "p1", release_if_terminal=True), capsys)
    assert code == 0, payload
    assert payload["result"] == "spawned_dispatch_terminal_released", payload
    assert "p1" not in _dispatched(tmp_path)


def test_mismatch_rejected_when_worker_still_active(tmp_path, monkeypatch, capsys) -> None:
    _write_contract(tmp_path)
    _orchestrator_env(monkeypatch)
    _seed(tmp_path, pid="p1", tid="t1", status="spawned_base_mismatch", action="repair_active")
    monkeypatch.setattr(cli, "_confirmed_absent_readback", lambda sid, pid: {"result": "active"})
    code, payload = _run(tmp_path, _args(tmp_path, "p1", release_if_terminal=True), capsys)
    assert code == 2, payload
    assert payload["reason"] == "spawn_baseline_mismatch_worker_still_active", payload
    assert "p1" in _dispatched(tmp_path)


# --- normal spawned lease unaffected --------------------------------------------------------------

def test_normal_spawned_lease_never_shells_out(tmp_path, monkeypatch, capsys) -> None:
    # The base-status injection must not intercept a normal 'spawned' lease: --refresh-time re-stamps it
    # via the existing writer path and NO readback is attempted.
    _write_contract(tmp_path)
    _orchestrator_env(monkeypatch)
    _seed(tmp_path, pid="p1", tid="t1", status="spawned", action="repair_active")
    monkeypatch.setattr(cli, "_confirmed_absent_readback", _boom)
    code, payload = _run(tmp_path, _args(tmp_path, "p1", refresh_time=True), capsys)
    assert code == 0, payload
    assert payload["result"] == "spawned_dispatch_time_refreshed", payload
    assert _dispatched(tmp_path)["p1"]["status"] == "spawned"
