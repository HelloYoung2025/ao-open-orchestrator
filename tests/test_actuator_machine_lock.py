"""M2-1 regression suite: machine-global escalated-review actuator mutex (multi-project).

WHY THIS EXISTS (the spec, not just a check): with MULTIPLE projects registered on one machine,
each project has its own per-project dispatch ledger — so per-project single-flight cannot stop
TWO projects' actuators from driving the ONE machine-global external review surface (one browser
endpoint, one reviewer account) at the same time. The mutex is a non-blocking machine-global
fcntl.flock (default ~/.agent-orchestrator/locks/escalated-review-actuator.lock, overridable via
AO_ESCALATED_REVIEW_ACTUATOR_LOCK_DIR), taken AFTER the per-project claim_dispatch succeeds and
held across the entire bridge run:

  * BUSY path: another holder (a second fd here stands in for another project's actuator) makes
    the attempt yield `escalated_review_actuator_busy` exit 0, RELEASE the per-project lease (the
    semantics of `in_flight`: this project's obligation is not consumed; a later actuate retries),
    and never run the bridge.
  * RELEASE paths: success, bridge failure, and a bridge exception ALL release the flock (kernel
    releases on fd close / process death too, so a SIGKILLed actuator cannot wedge the machine).
"""

from __future__ import annotations

import fcntl
import json
import os

import pytest

from test_public_core import _state_paths, _write_contract, _writer

from ao_state_writer import cli

ORCHESTRATOR_ENV = {"AO_CALLER_TYPE": "orchestrator", "AO_SESSION_ID": "example-orchestrator"}
LOCK_FILENAME = "escalated-review-actuator.lock"


def _orchestrator(monkeypatch) -> None:
    for key, value in ORCHESTRATOR_ENV.items():
        monkeypatch.setenv(key, value)


def _actuate(root, writer, proposal_id: str = "p-x") -> int:
    _, ledger_path = _state_paths(root)
    return cli._claim_and_run_escalated_review_actuator_cli(
        root=root,
        writer=writer,
        ledger_path=ledger_path,
        proposal_id=proposal_id,
        bridge_command="echo hi",
        bridge_timeout_seconds=10,
    )


def _flockable(lock_path) -> bool:
    """True iff the machine lock is currently free (a probe flock succeeds immediately)."""
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        os.close(fd)  # closing also drops the probe flock if it was granted
    return True


def test_busy_releases_lease_and_never_runs_bridge(tmp_path, monkeypatch, capsys) -> None:
    """While ANOTHER project's actuator holds the machine lock, this project's actuate must yield
    busy (exit 0), release its per-project lease (not consume the obligation), and never reach
    the bridge. Releasing the holder makes a retry proceed normally."""
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    monkeypatch.setenv("AO_ESCALATED_REVIEW_ACTUATOR_LOCK_DIR", str(lock_dir))
    root = tmp_path / "project-b"
    root.mkdir()
    _write_contract(root)
    writer = _writer(root)
    _orchestrator(monkeypatch)

    holder = os.open(lock_dir / LOCK_FILENAME, os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(holder, fcntl.LOCK_EX)  # another project's actuator is mid-bridge
    try:
        def _bridge_must_not_run(**_kw):
            raise AssertionError("bridge ran while the machine actuator lock was held elsewhere")

        monkeypatch.setattr(cli, "_run_escalated_review_actuator_cli", _bridge_must_not_run)
        rc = _actuate(root, writer)
        out = json.loads(capsys.readouterr().out)
        assert rc == 0, out
        assert out["result"] == "escalated_review_actuator_busy", out
        assert out["proposal_id"] == "p-x", out
        # The per-project lease was RELEASED (in_flight semantics): the obligation is not
        # consumed, and a later actuate can claim it again.
        record = writer.dispatch_record("p-x")
        assert not (isinstance(record, dict) and record.get("status")), record
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)

    # Holder gone -> the same actuate proceeds into the bridge (mocked success) and consumes.
    monkeypatch.setattr(cli, "_run_escalated_review_actuator_cli", lambda **_kw: 0)
    rc2 = _actuate(root, writer)
    assert rc2 == 0
    assert writer.is_dispatched("p-x")
    # ... and the machine lock was released after the successful run.
    assert _flockable(lock_dir / LOCK_FILENAME)


def test_success_and_failure_paths_release_the_machine_lock(tmp_path, monkeypatch, capsys) -> None:
    """The flock must be held across the bridge run and released on EVERY outcome — success
    (consumed), bridge failure (lease released), and a bridge exception (propagates)."""
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    monkeypatch.setenv("AO_ESCALATED_REVIEW_ACTUATOR_LOCK_DIR", str(lock_dir))
    root = tmp_path / "project"
    root.mkdir()
    _write_contract(root)
    writer = _writer(root)
    _orchestrator(monkeypatch)
    lock_path = lock_dir / LOCK_FILENAME

    # During the bridge run the machine lock must actually be HELD (a concurrent probe fails).
    seen_during_bridge: dict[str, bool] = {}

    def _probing_bridge(**_kw) -> int:
        seen_during_bridge["lock_held"] = not _flockable(lock_path)
        return 0

    monkeypatch.setattr(cli, "_run_escalated_review_actuator_cli", _probing_bridge)
    assert _actuate(root, writer, "p-ok") == 0
    assert seen_during_bridge["lock_held"] is True, "machine lock not held across the bridge run"
    assert _flockable(lock_path), "machine lock leaked after a successful run"

    # Bridge FAILURE (nonzero): lease released, machine lock released.
    monkeypatch.setattr(cli, "_run_escalated_review_actuator_cli", lambda **_kw: 3)
    assert _actuate(root, writer, "p-fail") == 3
    record = writer.dispatch_record("p-fail")
    assert not (isinstance(record, dict) and record.get("status")), record
    assert _flockable(lock_path), "machine lock leaked after a failed run"

    # Bridge EXCEPTION: propagates, but the machine lock must still be released (finally).
    def _boom(**_kw):
        raise RuntimeError("simulated bridge crash")

    monkeypatch.setattr(cli, "_run_escalated_review_actuator_cli", _boom)
    with pytest.raises(RuntimeError):
        _actuate(root, writer, "p-boom")
    assert _flockable(lock_path), "machine lock leaked after a bridge exception"
    capsys.readouterr()
