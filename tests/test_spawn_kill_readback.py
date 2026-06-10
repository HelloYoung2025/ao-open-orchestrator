"""Unit tests for the ao-session liveness readback ported in Group E slice "b3c"
(cli._kill_readback + cli._confirmed_absent_readback).

WHY THIS MATTERS (intent, not just behavior)
The reconcile CLI releases a bad-baseline (spawned_base_*) lease ONLY once the offending worker is PROVEN
gone. That proof is a live ``ao session ls -p <project> --json`` read mapped to absent/active. The mapping
must be conservative: a worker still listed with a non-terminal status is ``active`` (do NOT release); a
worker listed with a terminal status, or absent from the list, is ``absent`` (gone). Any probe we cannot
complete (no ``ao`` binary, timeout, non-zero exit, unparseable/oddly-shaped JSON, missing project id)
is ``failed``/``skipped`` — never silently treated as absent, because that would let us delete a lease
while the worker may still be mutating the wrong tree. ``_confirmed_absent_readback`` additionally requires
TWO consecutive absent reads so a single racy snapshot cannot trigger a release.

These shell out to ``ao``; here we monkeypatch ``subprocess.run`` (and, for the double-read wrapper,
``_kill_readback``) so the mapping is tested in isolation with no real engine.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from ao_state_writer import cli

PID = "proj-x"
SID = "sess-1"


def _fake_run(*, stdout: str = "[]", returncode: int = 0, stderr: str = "", raise_exc=None):
    def _run(command, capture_output=True, text=True, check=False, timeout=None):  # noqa: ANN001
        if raise_exc is not None:
            raise raise_exc
        return subprocess.CompletedProcess(args=command, returncode=returncode, stdout=stdout, stderr=stderr)

    return _run


# --- _kill_readback mapping ------------------------------------------------------------------------

def test_missing_project_id_is_skipped() -> None:
    # No subprocess at all: a missing project id cannot address `ao session ls`, so it fails closed as
    # skipped (NOT absent — we must never infer "gone" from an un-runnable probe).
    assert cli._kill_readback(SID, None) == {"result": "skipped", "reason": "missing_ao_project_id"}


def test_active_session_is_active_with_workspace(monkeypatch) -> None:
    payload = json.dumps([{"id": SID, "status": "running", "worktree": "/w/tree"}])
    monkeypatch.setattr(cli.subprocess, "run", _fake_run(stdout=payload))
    result = cli._kill_readback(SID, PID)
    assert result["result"] == "active"
    assert result["status"] == "running"
    assert result["workspacePath"] == "/w/tree"


def test_terminal_status_session_is_absent(monkeypatch) -> None:
    # The session is still LISTED but its status is terminal (killed) -> the worker is gone.
    payload = json.dumps([{"id": SID, "status": "KILLED"}])  # case-insensitive against TERMINAL set
    monkeypatch.setattr(cli.subprocess, "run", _fake_run(stdout=payload))
    result = cli._kill_readback(SID, PID)
    assert result["result"] == "absent"
    assert result["status"] == "KILLED"


def test_session_not_listed_is_absent(monkeypatch) -> None:
    payload = json.dumps([{"id": "other", "status": "running"}])
    monkeypatch.setattr(cli.subprocess, "run", _fake_run(stdout=payload))
    assert cli._kill_readback(SID, PID)["result"] == "absent"


def test_dict_sessions_shape_is_handled(monkeypatch) -> None:
    payload = json.dumps({"sessions": [{"id": SID, "status": "active"}]})
    monkeypatch.setattr(cli.subprocess, "run", _fake_run(stdout=payload))
    assert cli._kill_readback(SID, PID)["result"] == "active"


def test_dict_data_shape_is_handled(monkeypatch) -> None:
    payload = json.dumps({"data": [{"id": SID, "status": "done"}]})  # terminal -> absent
    monkeypatch.setattr(cli.subprocess, "run", _fake_run(stdout=payload))
    assert cli._kill_readback(SID, PID)["result"] == "absent"


def test_nonzero_exit_is_failed(monkeypatch) -> None:
    monkeypatch.setattr(cli.subprocess, "run", _fake_run(returncode=2, stdout="", stderr="boom"))
    result = cli._kill_readback(SID, PID)
    assert result["result"] == "failed"
    assert result["reason"] == "ao_session_ls_failed"
    assert result["returncode"] == 2


def test_invalid_json_is_failed(monkeypatch) -> None:
    monkeypatch.setattr(cli.subprocess, "run", _fake_run(stdout="not json"))
    result = cli._kill_readback(SID, PID)
    assert result["result"] == "failed"
    assert result["reason"] == "invalid_ao_session_ls_json"


def test_unexpected_json_shape_is_failed(monkeypatch) -> None:
    # A dict whose "sessions" is present but NOT a list is an unparseable shape -> failed. (A bare
    # non-list/non-dict top-level value instead degrades to an empty session list -> absent, so the
    # explicit shape-failure path is specifically the dict-with-bad-sessions case.)
    monkeypatch.setattr(cli.subprocess, "run", _fake_run(stdout=json.dumps({"sessions": "nope"})))
    result = cli._kill_readback(SID, PID)
    assert result["result"] == "failed"
    assert result["reason"] == "invalid_ao_session_ls_shape"


def test_non_list_non_dict_payload_degrades_to_absent(monkeypatch) -> None:
    # Documents the OTHER branch: a bare int/str top-level payload -> sessions=[] -> session not found
    # -> absent (NOT a shape failure). Locks the verbatim-LIVE distinction the test above relies on.
    monkeypatch.setattr(cli.subprocess, "run", _fake_run(stdout=json.dumps(42)))
    assert cli._kill_readback(SID, PID)["result"] == "absent"


def test_timeout_is_failed(monkeypatch) -> None:
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        _fake_run(raise_exc=subprocess.TimeoutExpired(cmd="ao", timeout=30)),
    )
    result = cli._kill_readback(SID, PID)
    assert result["result"] == "failed"
    assert result["reason"] == "ao_session_ls_timeout"


def test_missing_binary_is_failed(monkeypatch) -> None:
    monkeypatch.setattr(cli.subprocess, "run", _fake_run(raise_exc=FileNotFoundError("no ao")))
    result = cli._kill_readback(SID, PID)
    assert result["result"] == "failed"
    assert result["reason"] == "ao_command_not_found"


# --- _confirmed_absent_readback (two-read confirmation) -------------------------------------------

def test_confirmed_absent_requires_two_absent_reads(monkeypatch) -> None:
    calls = {"n": 0}

    def _two_absent(sid, pid):  # noqa: ANN001
        calls["n"] += 1
        return {"result": "absent", "command": ["ao", "session", "ls"]}

    monkeypatch.setattr(cli, "_kill_readback", _two_absent)
    result = cli._confirmed_absent_readback(SID, PID)
    assert result["result"] == "absent"
    assert calls["n"] == 2  # it actually read twice
    assert isinstance(result["confirmations"], list) and len(result["confirmations"]) == 2


def test_confirmed_absent_returns_first_non_absent(monkeypatch) -> None:
    # The first read is active -> short-circuit, do NOT release on a single absent later.
    monkeypatch.setattr(cli, "_kill_readback", lambda sid, pid: {"result": "active", "id": sid})
    result = cli._confirmed_absent_readback(SID, PID)
    assert result["result"] == "active"


def test_confirmed_absent_returns_second_when_it_flips_active(monkeypatch) -> None:
    seq = [{"result": "absent"}, {"result": "active", "id": SID}]
    monkeypatch.setattr(cli, "_kill_readback", lambda sid, pid: seq.pop(0))
    result = cli._confirmed_absent_readback(SID, PID)
    assert result["result"] == "active"  # the racy second read prevents a false release
