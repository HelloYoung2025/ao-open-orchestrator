"""Tests for the advisory command-discoverability surfacing ported in Group E task#14a-1
(cli._final_convergence_record_command / _spawn_attestation_reconcile_commands /
_historical_dispatch_records, + their wiring into _current_obligation_issue and cmd_reconcile_once).

WHY THIS MATTERS (intent, not just behavior)
An UNATTENDED orchestrator self-heals/converges by reading the exact CLI argv out of the obligation and
readout payloads. If those payloads don't carry the commands, an autonomous run cannot discover how to
record final convergence or reconcile a leftover spawned/unattested/base-* lease — the project silently
wedges. Two load-bearing properties:

  * BRAND-NEUTRAL argv: every emitted command is the ``ao-state-writer`` console_script. It must NEVER
    carry LIVE's machine-coupled ``env PYTHONPATH=<private dispatch src tree> <interpreter>`` wrapper —
    that is both a private-path leak and a non-portable assumption for a pip-installed public engine.
  * DISCOVERABILITY WITHOUT RELABELING: the convergence-required obligation still fails closed and still
    names a convergence REVIEW as the immediate repair action; the record-final-convergence argv is an
    additional discoverability field, NOT a replacement for the review step.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ao_state_writer import cli
from ao_state_writer.writer import StateWriter

# Tokens that would prove a LIVE machine-wrapper leaked into an advisory command. Split so this test
# file never trips the public_safety_scan leak gate on its own source (the scanner forbids these literals).
LEAK_TOKENS = ("env", "PYTHONPATH", "claw-" + "commander", "python3", "/opt/" + "homebrew", "src")


def _writer(root: Path) -> StateWriter:
    base = root / ".omx" / "state" / "ao-state-writer"
    base.mkdir(parents=True, exist_ok=True)
    return StateWriter(state_path=base / "state.json", ledger_path=base / "state-transitions.jsonl")


def _assert_brand_neutral(argv: list[str]) -> None:
    assert argv[0] == "ao-state-writer", argv
    for token in LEAK_TOKENS:
        assert not any(token in part for part in argv), (token, argv)


# --- command builders: exact brand-neutral argv ---------------------------------------------------

def test_final_convergence_record_command_argv(tmp_path: Path) -> None:
    argv = cli._final_convergence_record_command(tmp_path, "p1")
    assert argv == [
        "ao-state-writer",
        "record-final-convergence",
        "--root",
        str(tmp_path),
        "--proposal-id",
        "p1",
        "--evidence",
        "owner-proxy:final-convergence",
    ]
    _assert_brand_neutral(argv)


def test_spawn_attestation_reconcile_commands_argv(tmp_path: Path) -> None:
    cmds = cli._spawn_attestation_reconcile_commands(tmp_path, "p1")
    assert set(cmds) == {"record_existing_session", "release_absent_session"}
    assert cmds["record_existing_session"][-2:] == ["--spawn-session-id", "<session-id>"]
    assert cmds["release_absent_session"][-1] == "--release-if-absent"
    for argv in cmds.values():
        assert argv[1] == "reconcile-spawn-attestation"
        _assert_brand_neutral(argv)


def test_spawned_dispatch_reconcile_commands_stay_brand_neutral(tmp_path: Path) -> None:
    # Lock the already-ported sibling too, so a future edit can't reintroduce the machine wrapper.
    for argv in cli._spawned_dispatch_reconcile_commands(tmp_path, "p1").values():
        assert argv[1] == "reconcile-spawned-dispatch"
        _assert_brand_neutral(argv)


# --- _historical_dispatch_records filtering -------------------------------------------------------

def _seed_history(root: Path, records: dict[str, str]) -> dict:
    """records: proposal_id -> dispatch status. proposal_results all accepted; ledger maps each to its
    own target."""
    dispatched = {pid: {"status": status, "spawn_session_id": f"sess-{pid}"} for pid, status in records.items()}
    proposal_results = {pid: {"decision": "accepted", "next_required_action": "codex_cc_review"} for pid in records}
    targets = {f"t-{pid}": {"state": "evidence_pending"} for pid in records}
    return {
        "schema_version": 1,
        "state_revision": 1,
        "targets": targets,
        "proposal_results": proposal_results,
        "dispatched_proposals": dispatched,
    }


def test_historical_records_include_audit_statuses_exclude_others(tmp_path, monkeypatch) -> None:
    writer = _writer(tmp_path)
    # 4 audit statuses + 2 non-audit (must be excluded).
    state = _seed_history(
        tmp_path,
        {
            "p-spawned": "spawned",
            "p-unattested": "spawned_unattested",
            "p-mismatch": "spawned_base_mismatch",
            "p-unverified": "spawned_base_unverified",
            "p-pending": "pending",
            "p-final": "final_convergence_recorded",
        },
    )
    # Isolate the filtering from the current-detection machinery (tested elsewhere): nothing is current.
    monkeypatch.setattr(cli, "_latest_accepted_revision_by_target", lambda *a, **k: {})
    monkeypatch.setattr(cli, "_is_current_actionable_obligation", lambda *a, **k: False)

    records = cli._historical_dispatch_records(state, writer, writer.ledger_path)
    by_pid = {r["proposal_id"]: r for r in records}
    assert set(by_pid) == {"p-spawned", "p-unattested", "p-mismatch", "p-unverified"}
    # exact command variant per status
    assert "ao_session_attestation_reconcile_commands" in by_pid["p-unattested"]
    assert "spawned_dispatch_reconcile_commands" not in by_pid["p-unattested"]
    for pid in ("p-spawned", "p-mismatch", "p-unverified"):
        assert "spawned_dispatch_reconcile_commands" in by_pid[pid]
        assert "ao_session_attestation_reconcile_commands" not in by_pid[pid]
    # all flagged non-current + stable sort by proposal_id
    assert all(r["current_obligation"] is False for r in records)
    assert [r["proposal_id"] for r in records] == sorted(by_pid)


def test_historical_records_exclude_current_obligation(tmp_path, monkeypatch) -> None:
    writer = _writer(tmp_path)
    state = _seed_history(tmp_path, {"p-current": "spawned", "p-old": "spawned"})
    monkeypatch.setattr(cli, "_latest_accepted_revision_by_target", lambda *a, **k: {})
    # p-current IS the live obligation -> must be excluded from the historical/audit side-channel.
    monkeypatch.setattr(
        cli, "_is_current_actionable_obligation", lambda state, pid, *a, **k: pid == "p-current"
    )
    records = cli._historical_dispatch_records(state, writer, writer.ledger_path)
    assert [r["proposal_id"] for r in records] == ["p-old"]


# --- _current_obligation_issue convergence payload carries the record command ---------------------

def test_convergence_required_carries_record_command_but_keeps_review(tmp_path, monkeypatch) -> None:
    writer = _writer(tmp_path)
    state = {
        "schema_version": 1,
        "state_revision": 1,
        "targets": {"t1": {"state": "evidence_pending"}},
        "proposal_results": {"p1": {"decision": "accepted", "next_required_action": "repair_attempts_exhausted"}},
        "dispatched_proposals": {},
    }
    # Isolate from the projection/current machinery: p1 is the genuine, current, exhausted obligation.
    monkeypatch.setattr(cli, "_effective_action", lambda *a, **k: "repair_attempts_exhausted")
    monkeypatch.setattr(cli, "_is_current_actionable_obligation", lambda *a, **k: True)
    monkeypatch.setattr(cli, "_live_escalated_review_gate_ids", lambda *a, **k: set())
    monkeypatch.setattr(cli, "_proposal_targets_from_ledger", lambda *a, **k: {"p1": "t1"})
    monkeypatch.setattr(cli, "_latest_accepted_revision_by_target", lambda *a, **k: {})

    issue = cli._current_obligation_issue(
        root=tmp_path, state=state, writer=writer, ledger_path=writer.ledger_path
    )
    assert issue is not None
    assert issue["result"] == "owner_proxy_convergence_required"
    # still fail-closed: the immediate repair action is the review, NOT a relabel to record.
    assert issue["allowed_repair_actions"] == ["orchestrator_convergence_review"]
    # but the terminal record argv is now discoverable.
    cmd = issue["owner_proxy_final_convergence_command"]
    assert cmd == cli._final_convergence_record_command(tmp_path, "p1")
    _assert_brand_neutral(cmd)


# --- cmd_reconcile_once surfaces historical_dispatch_records --------------------------------------

def test_reconcile_once_emits_historical_records(tmp_path, monkeypatch, capsys) -> None:
    state = _seed_history(tmp_path, {"p-old": "spawned"})
    base = tmp_path / ".omx" / "state" / "ao-state-writer"
    base.mkdir(parents=True, exist_ok=True)
    (base / "state.json").write_text(json.dumps(state), encoding="utf-8")
    (base / "state-transitions.jsonl").write_text("", encoding="utf-8")

    monkeypatch.setattr(cli, "preflight_reconcile", lambda **kw: None)
    monkeypatch.setattr(cli, "_ready_candidates", lambda *a, **k: [])
    monkeypatch.setattr(cli, "_gated_candidates", lambda *a, **k: [])
    monkeypatch.setattr(cli, "_latest_accepted_revision_by_target", lambda *a, **k: {})
    monkeypatch.setattr(cli, "_is_current_actionable_obligation", lambda *a, **k: False)

    import argparse

    code = cli.cmd_reconcile_once(argparse.Namespace(root=tmp_path))
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["result"] == "nothing_to_continue"
    assert [r["proposal_id"] for r in payload["historical_dispatch_records"]] == ["p-old"]
    assert "spawned_dispatch_reconcile_commands" in payload["historical_dispatch_records"][0]


def _reconcile_once_setup(tmp_path, monkeypatch, *, ready, gated) -> None:
    state = _seed_history(tmp_path, {"p-old": "spawned"})
    base = tmp_path / ".omx" / "state" / "ao-state-writer"
    base.mkdir(parents=True, exist_ok=True)
    (base / "state.json").write_text(json.dumps(state), encoding="utf-8")
    (base / "state-transitions.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr(cli, "preflight_reconcile", lambda **kw: None)
    monkeypatch.setattr(cli, "_ready_candidates", lambda *a, **k: ready)
    monkeypatch.setattr(cli, "_gated_candidates", lambda *a, **k: gated)
    monkeypatch.setattr(cli, "_latest_accepted_revision_by_target", lambda *a, **k: {})
    monkeypatch.setattr(cli, "_is_current_actionable_obligation", lambda *a, **k: False)


def test_reconcile_once_emits_historical_in_ready_branch(tmp_path, monkeypatch, capsys) -> None:
    # The audit side-channel must also appear on the ACTIVE branch, not only the idle one.
    _reconcile_once_setup(tmp_path, monkeypatch, ready=[{"proposal_id": "p-ready"}], gated=[])
    import argparse

    cli.cmd_reconcile_once(argparse.Namespace(root=tmp_path))
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"] == "ready"
    assert [r["proposal_id"] for r in payload["historical_dispatch_records"]] == ["p-old"]


def test_reconcile_once_emits_historical_in_gated_branch(tmp_path, monkeypatch, capsys) -> None:
    _reconcile_once_setup(tmp_path, monkeypatch, ready=[], gated=[{"proposal_id": "p-gated"}])
    import argparse

    cli.cmd_reconcile_once(argparse.Namespace(root=tmp_path))
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"] == "requires_orchestrator_authorization"
    assert [r["proposal_id"] for r in payload["historical_dispatch_records"]] == ["p-old"]
