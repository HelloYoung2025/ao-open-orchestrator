"""Tests for the dead-session reaper (state-writer maintenance reconcile).

These tests encode WHY the reaper must behave the way it does, not just WHAT it
outputs: every guard exists to avoid retiring a session that still holds value
(live runtime, fresh report, open PR) or to avoid clobbering a concurrent AO
write. The decisive correctness property — that a retired session is
indistinguishable to AO from one it killed itself — is asserted via a local port
of AO's ``deriveLegacyStatus`` mapping ``terminated/runtime_lost -> "killed"``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ao_state_writer import session_reaper
from ao_state_writer.cli import main


# Fixed "now": 2026-06-06T08:00:00Z. Fixtures default to year-old timestamps so
# they are comfortably stale relative to this.
NOW_EPOCH = datetime(2026, 6, 6, 8, 0, 0, tzinfo=timezone.utc).timestamp()
OLD_TS = "2026-01-01T00:00:00.000Z"
STAMP = "2026-06-06T08:00:00.000Z"


def derive_legacy_status(lifecycle: dict) -> str:
    """Local port of ao-core lifecycle-state.js deriveLegacyStatus (the branches
    the reaper can produce)."""
    session = lifecycle["session"]
    state = session["state"]
    if state == "terminated":
        reason = session["reason"]
        if reason in ("manually_killed", "runtime_lost"):
            return "killed"
        if reason in ("error_in_process", "probe_failure"):
            return "errored"
        return "terminated"
    if state == "done":
        return "done"
    return state


def make_session(**overrides) -> dict:
    """A valid lifecycle-v2 worker session that is, by default, a dead/stale
    retire candidate (runtime missing, year-old timestamps, no PR)."""
    payload = {
        "worktree": "/tmp/wt/example-orch-1-50",
        "branch": "session/example-orch-1-50",
        "tmuxName": "example-orch-1-50",
        "agent": "claude-code",
        "userPrompt": "Execute the current next_required_action now.",
        "agentReportedState": "needs_input",
        "lifecycle": {
            "version": 2,
            "session": {
                "kind": "worker",
                "state": "detecting",
                "reason": "runtime_lost",
                "startedAt": OLD_TS,
                "completedAt": None,
                "terminatedAt": None,
                "lastTransitionAt": OLD_TS,
            },
            "pr": {"state": "none", "reason": "not_created", "number": None, "url": None},
            "runtime": {
                "state": "missing",
                "reason": "tmux_missing",
                "lastObservedAt": OLD_TS,
                "tmuxName": "example-orch-1-50",
            },
        },
    }
    # Shallow-merge overrides into the nested lifecycle for convenience.
    for key, value in overrides.items():
        if key in ("session", "runtime", "pr"):
            payload["lifecycle"][key].update(value)
        else:
            payload[key] = value
    return payload


def classify(payload, *, live_tmux=frozenset(), grace=session_reaper.DEFAULT_GRACE_SECONDS):
    return session_reaper.classify(
        payload, now_epoch=NOW_EPOCH, grace_seconds=grace, live_tmux=set(live_tmux)
    )


# --- predicate edges ---------------------------------------------------------
def test_dead_stale_session_is_retire_candidate():
    assert classify(make_session()) == session_reaper.RETIRE


def test_alive_runtime_is_preserved():
    # A session whose runtime AO still considers alive must never be retired —
    # it may be a working/idle pane holding real work.
    payload = make_session(runtime={"state": "alive", "reason": "process_running"})
    assert classify(payload) == session_reaper.SKIP_RUNTIME_NOT_MISSING


def test_live_tmux_match_is_preserved():
    # Even with runtime flagged missing, if a tmux pane with that name is live,
    # fail closed: the flag may be stale and the pane may be doing work.
    assert classify(make_session(), live_tmux={"example-orch-1-50"}) == session_reaper.SKIP_TMUX_ALIVE


def test_already_terminal_is_skipped():
    payload = make_session(session={"state": "terminated", "reason": "manually_killed"})
    assert classify(payload) == session_reaper.SKIP_ALREADY_TERMINAL


def test_orchestrator_is_never_retired():
    payload = make_session(session={"kind": "orchestrator"})
    assert classify(payload) == session_reaper.SKIP_ORCHESTRATOR


def test_open_pr_is_preserved():
    payload = make_session(pr={"state": "open", "reason": "merge_ready", "number": 15})
    assert classify(payload) == session_reaper.SKIP_PR_VALUE_PRESENT


def test_pr_url_evidence_is_preserved_even_when_state_none():
    # codex #3: preserve on ANY PR evidence, not just lifecycle.pr.state.
    payload = make_session(pr={"state": "none", "url": "https://github.com/x/y/pull/9"})
    assert classify(payload) == session_reaper.SKIP_PR_VALUE_PRESENT


def test_recent_activity_is_not_stale_enough():
    # codex #1: runtime.state==missing alone is too eager; a recently-observed
    # session might still be reconciling. Require the grace window.
    recent = datetime(2026, 6, 6, 7, 59, 0, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    payload = make_session(runtime={"lastObservedAt": recent})
    assert classify(payload) == session_reaper.SKIP_NOT_STALE_ENOUGH


def test_missing_staleness_evidence_is_skipped():
    payload = make_session()
    del payload["lifecycle"]["runtime"]["lastObservedAt"]
    assert classify(payload) == session_reaper.SKIP_MISSING_STALENESS_EVIDENCE


def test_fresh_agent_report_blocks_retire():
    # If agentReportedAt is present and fresh, do not retire even when runtime
    # looks gone — the report may still be actionable.
    fresh = datetime(2026, 6, 6, 7, 59, 30, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    payload = make_session(agentReportedAt=fresh)
    assert classify(payload) == session_reaper.SKIP_NOT_STALE_ENOUGH


def test_missing_tmux_name_is_skipped():
    payload = make_session(runtime={"tmuxName": None})
    payload["tmuxName"] = None
    assert classify(payload) == session_reaper.SKIP_TMUX_NAME_UNKNOWN


def test_unavailable_tmux_snapshot_fails_closed():
    assert session_reaper.classify(
        make_session(), now_epoch=NOW_EPOCH, grace_seconds=900, live_tmux=None
    ) == session_reaper.SKIP_TMUX_SNAPSHOT_UNAVAILABLE


def test_unsupported_lifecycle_version_is_skipped():
    payload = make_session()
    payload["lifecycle"]["version"] = 1
    assert classify(payload) == session_reaper.SKIP_UNSUPPORTED_LIFECYCLE


# --- terminal mutation -------------------------------------------------------
def test_terminal_shape_maps_to_killed_and_preserves_other_keys():
    payload = make_session()
    session_reaper.apply_terminal_lifecycle(payload, stamp=STAMP)
    lc = payload["lifecycle"]
    assert lc["session"]["state"] == "terminated"
    assert lc["session"]["reason"] == "runtime_lost"
    assert lc["session"]["terminatedAt"] == STAMP
    assert lc["session"]["lastTransitionAt"] == STAMP
    assert lc["runtime"]["state"] == "missing"
    assert lc["runtime"]["reason"] == "tmux_missing"  # preserved
    assert lc["runtime"]["lastObservedAt"] == STAMP
    # The decisive interop property: AO reads this as the terminal "killed".
    assert derive_legacy_status(lc) == "killed"
    # Non-lifecycle payload keys are preserved verbatim.
    assert payload["userPrompt"].startswith("Execute the current")
    assert payload["branch"] == "session/example-orch-1-50"


def test_detecting_markers_are_cleared():
    payload = make_session()
    payload["lifecycle"]["session"]["detectingAttempts"] = 3
    payload["lifecycle"]["runtime"]["detectingStartedAt"] = OLD_TS
    session_reaper.apply_terminal_lifecycle(payload, stamp=STAMP)
    assert "detectingAttempts" not in payload["lifecycle"]["session"]
    assert "detectingStartedAt" not in payload["lifecycle"]["runtime"]


# --- apply over a sessions dir ----------------------------------------------
def _write(sessions_dir: Path, sid: str, payload: dict) -> Path:
    sessions_dir.mkdir(parents=True, exist_ok=True)
    path = sessions_dir / f"{sid}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def test_apply_retires_only_candidates(tmp_path):
    sessions = tmp_path / "sessions"
    candidate = _write(sessions, "example-orch-1-50", make_session())
    alive = _write(
        sessions,
        "example-orch-1-99",
        make_session(tmpName="x", runtime={"state": "alive", "reason": "process_running"}),
    )
    alive_before = alive.read_text(encoding="utf-8")

    report = session_reaper.apply_retirements(
        sessions, now_epoch=NOW_EPOCH, stamp=STAMP, grace_seconds=900, live_tmux=set()
    )
    assert report["applied"] == ["example-orch-1-50"]
    # Candidate is now terminal/killed.
    after = json.loads(candidate.read_text(encoding="utf-8"))
    assert derive_legacy_status(after["lifecycle"]) == "killed"
    # The alive session is byte-for-byte untouched.
    assert alive.read_text(encoding="utf-8") == alive_before


def test_apply_is_idempotent(tmp_path):
    sessions = tmp_path / "sessions"
    _write(sessions, "example-orch-1-50", make_session())
    first = session_reaper.apply_retirements(
        sessions, now_epoch=NOW_EPOCH, stamp=STAMP, grace_seconds=900, live_tmux=set()
    )
    assert first["applied"] == ["example-orch-1-50"]
    second = session_reaper.apply_retirements(
        sessions, now_epoch=NOW_EPOCH, stamp=STAMP, grace_seconds=900, live_tmux=set()
    )
    assert second["applied"] == []
    assert "example-orch-1-50" in second["skipped"].get(session_reaper.SKIP_ALREADY_TERMINAL, [])


def test_apply_skips_when_file_changed_during_gc(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    _write(sessions, "example-orch-1-50", make_session())

    # Simulate a concurrent AO write landing between the dry-run digest and the
    # in-lock digest: make the two _digest() reads differ.
    digests = iter(["pre-digest", "DIFFERENT-after-lock"])

    def fake_digest(_path):
        try:
            return next(digests)
        except StopIteration:
            return "stable"

    monkeypatch.setattr(session_reaper, "_digest", fake_digest)
    report = session_reaper.apply_retirements(
        sessions, now_epoch=NOW_EPOCH, stamp=STAMP, grace_seconds=900, live_tmux=set()
    )
    assert report["applied"] == []
    assert report["changed_during_gc"] == ["example-orch-1-50"]


def test_apply_writes_audit_row(tmp_path):
    sessions = tmp_path / "sessions"
    _write(sessions, "example-orch-1-50", make_session())
    audit = tmp_path / "audit.jsonl"
    session_reaper.apply_retirements(
        sessions, now_epoch=NOW_EPOCH, stamp=STAMP, grace_seconds=900, live_tmux=set(), audit_path=audit
    )
    rows = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["session_id"] == "example-orch-1-50"
    assert rows[0]["new_session_state"] == "terminated"
    assert rows[0]["decided_by"] == "state_writer_session_reaper"


def test_plan_breakdown(tmp_path):
    sessions = tmp_path / "sessions"
    _write(sessions, "example-orch-1-50", make_session())
    _write(sessions, "example-orch-1-99", make_session(runtime={"state": "alive"}))
    report = session_reaper.plan(sessions, now_epoch=NOW_EPOCH, grace_seconds=900, live_tmux=set())
    assert report["retire"] == ["example-orch-1-50"]
    assert report["scanned"] == 2
    assert "example-orch-1-99" in report["skipped"].get(session_reaper.SKIP_RUNTIME_NOT_MISSING, [])


# --- CLI wiring --------------------------------------------------------------
def test_cli_dry_run_is_default(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(session_reaper, "live_tmux_names", lambda: set())
    projects = tmp_path / "projects"
    _write(projects / "proj" / "sessions", "example-orch-1-50", make_session())
    monkeypatch.setenv("AO_PROJECTS_ROOT", str(projects))

    rc = main(["retire-dead-sessions", "--root", str(tmp_path), "--project-id", "proj"])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 0
    assert out["result"] == "retire_plan"
    assert out["retire"] == ["example-orch-1-50"]
    # Dry-run must NOT mutate the file.
    payload = json.loads((projects / "proj" / "sessions" / "example-orch-1-50.json").read_text())
    assert payload["lifecycle"]["session"]["state"] == "detecting"


def test_cli_apply_retires(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(session_reaper, "live_tmux_names", lambda: set())
    projects = tmp_path / "projects"
    path = _write(projects / "proj" / "sessions", "example-orch-1-50", make_session())
    monkeypatch.setenv("AO_PROJECTS_ROOT", str(projects))

    rc = main(["retire-dead-sessions", "--root", str(tmp_path), "--project-id", "proj", "--apply"])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 0
    assert out["result"] == "retire_applied"
    assert out["applied"] == ["example-orch-1-50"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert derive_legacy_status(payload["lifecycle"]) == "killed"


# --- V2: P5 operator_pause integration (codex V2_DESIGN: block --apply, allow valid dry-run) ------
def _seed_operator_pause(root: Path, *, malformed: bool = False) -> None:
    """Seed operator_pause into <root>/.omx/state/ao-state-writer/state.json (NOT the sessions dir),
    proving the reaper reads the pause from --root. Valid pause uses the real P5 writer primitive;
    malformed writes a directly-corrupt record."""
    from ao_state_writer.cli import _state_paths
    from ao_state_writer.writer import StateWriter

    state_path, ledger_path = _state_paths(root)
    writer = StateWriter(state_path=state_path, ledger_path=ledger_path)
    if malformed:
        with writer._single_flight():
            state = writer._read_state()
            state["operator_pause"] = {"paused": "not-a-bool"}
            writer._write_state(state)
    else:
        writer.set_operator_pause(reason="migration freeze", set_by="direct")


def test_cli_apply_blocked_while_operator_paused(tmp_path, monkeypatch, capsys):
    """--apply is a canonical-mutating, preflight-bypassing write, so a VALID operator_pause at
    --root blocks it (operator_paused exit 3) with ZERO session-file mutation — the same rule P5
    locks for reconcile-leases --apply and the other preflight-bypassing writers. Without this
    guard the reaper would be a pause-bypass mutation hole (the class P5's confirm REJECTED)."""
    monkeypatch.setattr(session_reaper, "live_tmux_names", lambda: set())
    projects = tmp_path / "projects"
    path = _write(projects / "proj" / "sessions", "example-orch-1-50", make_session())
    monkeypatch.setenv("AO_PROJECTS_ROOT", str(projects))
    before = path.read_bytes()
    _seed_operator_pause(tmp_path)

    rc = main(["retire-dead-sessions", "--root", str(tmp_path), "--project-id", "proj", "--apply"])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 3
    assert out["result"] == "operator_paused"
    assert path.read_bytes() == before  # zero session-file mutation while paused


def test_cli_dry_run_allowed_while_operator_paused(tmp_path, monkeypatch, capsys):
    """Dry-run is read-only, so a VALID pause does NOT block it: the owner can still inspect what
    WOULD be retired during a maintenance freeze. retire_plan is reported, session file untouched."""
    monkeypatch.setattr(session_reaper, "live_tmux_names", lambda: set())
    projects = tmp_path / "projects"
    path = _write(projects / "proj" / "sessions", "example-orch-1-50", make_session())
    monkeypatch.setenv("AO_PROJECTS_ROOT", str(projects))
    before = path.read_bytes()
    _seed_operator_pause(tmp_path)

    rc = main(["retire-dead-sessions", "--root", str(tmp_path), "--project-id", "proj"])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 0
    assert out["result"] == "retire_plan"
    assert out["retire"] == ["example-orch-1-50"]
    assert path.read_bytes() == before  # dry-run never mutates


def test_cli_malformed_operator_pause_fails_closed_both_modes(tmp_path, monkeypatch, capsys):
    """A corrupt operator_pause record fails closed for BOTH dry-run AND --apply (never silently
    treated as unpaused, which would let a damaged pause record permit a mutating reap). The
    session file stays byte-identical in both modes."""
    monkeypatch.setattr(session_reaper, "live_tmux_names", lambda: set())
    projects = tmp_path / "projects"
    path = _write(projects / "proj" / "sessions", "example-orch-1-50", make_session())
    monkeypatch.setenv("AO_PROJECTS_ROOT", str(projects))
    before = path.read_bytes()
    _seed_operator_pause(tmp_path, malformed=True)

    for extra in ([], ["--apply"]):
        rc = main(["retire-dead-sessions", "--root", str(tmp_path), "--project-id", "proj"] + extra)
        out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert rc == 3, extra
        assert out["result"] == "operator_pause_malformed", extra
        assert path.read_bytes() == before, extra
