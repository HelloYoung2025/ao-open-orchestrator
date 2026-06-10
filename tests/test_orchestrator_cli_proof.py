"""Unit tests for the orchestrator-CLI-proof gate ported in Group E slice "f".

WHY (intent, not just behavior): the escalated-review actuate and watchdog timeout-blocker CLI paths run
side effects (executing a review bridge; minting a watchdog receipt) that are NOT themselves the
writer methods carrying the inline ``non_orchestrator_caller`` guard. ``_orchestrator_cli_proof_issue``
is the cli-preflight layer that fails those paths closed unless the caller presents a LIVE orchestrator
identity (``AO_CALLER_TYPE=orchestrator`` AND a writer proof whose session matches the contract). A
non-orchestrator or stale-session caller must not be able to actuate or mint a receipt by invoking the
subcommand directly.
"""

from __future__ import annotations

from ao_state_writer import cli


class _StubWriter:
    """Minimal stand-in exposing only the one method the gate calls."""

    def __init__(self, metadata: dict) -> None:
        self._metadata = dict(metadata)

    def _orchestrator_proof_metadata(self) -> dict:
        return dict(self._metadata)


def test_missing_caller_fails_closed(monkeypatch) -> None:
    monkeypatch.delenv(cli.CALLER_TYPE_ENV, raising=False)
    issue = cli._orchestrator_cli_proof_issue(writer=_StubWriter({}), proposal_id="p1")
    assert issue is not None
    assert issue["result"] == "missing_or_stale_orchestrator_proof"
    assert issue["caller_type"] is None
    assert issue["proposal_id"] == "p1"
    assert issue["forbidden_actions"] == ["actuate", "canonical_write"]
    assert issue["allowed_repair_actions"] == ["orchestrator_session_rebind"]


def test_wrong_caller_type_fails_closed(monkeypatch) -> None:
    # A worker (or any non-orchestrator) caller is rejected BEFORE the writer proof is even consulted.
    monkeypatch.setenv(cli.CALLER_TYPE_ENV, "worker")
    issue = cli._orchestrator_cli_proof_issue(writer=_StubWriter({"session_id": "s"}))
    assert issue is not None
    assert issue["result"] == "missing_or_stale_orchestrator_proof"
    assert issue["caller_type"] == "worker"
    # A None proposal_id is omitted from the payload (not stamped as null).
    assert "proposal_id" not in issue


def test_orchestrator_with_stale_proof_surfaces_writer_reason(monkeypatch) -> None:
    # Caller is orchestrator, but the writer proof (session/active_root/project_id) is stale/mismatched:
    # surface the writer's reason, still fail closed.
    monkeypatch.setenv(cli.CALLER_TYPE_ENV, "orchestrator")
    issue = cli._orchestrator_cli_proof_issue(
        writer=_StubWriter({"reason": "missing_or_stale_orchestrator_proof"}), proposal_id="p2"
    )
    assert issue is not None
    assert issue["result"] == "missing_or_stale_orchestrator_proof"
    assert issue["caller_type"] == "orchestrator"
    assert issue["proposal_id"] == "p2"


def test_proven_orchestrator_passes(monkeypatch) -> None:
    # Caller is orchestrator AND the writer proof is clean (no reason) -> gate opens (None).
    monkeypatch.setenv(cli.CALLER_TYPE_ENV, "orchestrator")
    issue = cli._orchestrator_cli_proof_issue(
        writer=_StubWriter({"session_id": "example-orchestrator", "project_id": "proj", "active_root": "/x"})
    )
    assert issue is None
