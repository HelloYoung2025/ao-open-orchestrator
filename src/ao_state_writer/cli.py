from __future__ import annotations

from pathlib import Path
import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9 compatibility for the project test runner.
    tomllib = None

from .compat import (
    CALLER_TYPE_ENV,
    UnsupportedStateSchemaVersion,
    empty_state,
    normalize_state,
    read_contract_active_root,
    validate_contract_compat,
)
from .continuation import (
    AUTO_SPAWN_ACTIONS,
    GATED_ACTIONS,
    NON_EXECUTABLE_ACTIONS,
    continue_after_apply,
    evaluate_continuation,
)
from .preflight import check_governance_dirty
from .todo import repair_compact_current_state
from .watchdog import evaluate_watchdog, load_observation, write_proposal
from .writer import (
    GPT_PRO_ACTUATOR_FAILURE_BLOCKER_CODE,
    StateTransitionDecision,
    StateTransitionProposal,
    StateWriter,
)


def _state_paths(root: Path) -> tuple[Path, Path]:
    base = root / ".omx" / "state" / "ao-state-writer"
    return base / "state.json", base / "state-transitions.jsonl"


PRECHECK_FORBIDDEN_ACTIONS = ["dispatch", "authorize", "spawn", "canonical_write", "allowlist_add"]


def _validate_contract(root: Path) -> str | None:
    return validate_contract_compat(
        root,
        auto_spawn_actions=AUTO_SPAWN_ACTIONS,
        gated_actions=GATED_ACTIONS,
        non_executable_actions=NON_EXECUTABLE_ACTIONS,
    )


def _non_canonical_root_payload(root: Path) -> dict[str, object] | None:
    active_root = read_contract_active_root(root)
    provided_root = root.expanduser().resolve()
    if active_root is None:
        return {
            "result": "missing_ao_active_root",
            "provided_root": str(provided_root),
            "forbidden_actions": PRECHECK_FORBIDDEN_ACTIONS,
            "allowed_repair_actions": ["orchestrator_root_review"],
        }
    if provided_root == active_root:
        return None
    return {
        "result": "non_canonical_root",
        "provided_root": str(provided_root),
        "active_root": str(active_root),
        "forbidden_actions": PRECHECK_FORBIDDEN_ACTIONS,
        "allowed_repair_actions": ["orchestrator_root_review"],
    }


def preflight_reconcile(
    *,
    root: Path,
    writer: StateWriter,
    ledger_path: Path,
    dry_run: bool = True,
) -> tuple[dict[str, object], int] | None:
    """Shared read-only guard for orchestration entrypoints.

    It does not authorize, claim, release, spawn, dispatch, enqueue, or persist a
    reconcile result.  It only converts unsafe current obligations into typed
    fail-closed envelopes before a CLI entrypoint can silently return an empty list.
    """
    root_payload = _non_canonical_root_payload(root)
    if root_payload is not None:
        return root_payload, 3

    contract_error = _validate_contract(root)
    if contract_error is not None:
        return {"result": contract_error}, 3

    try:
        state = _read_state(writer.state_path)
    except UnsupportedStateSchemaVersion:
        return {"result": "unsupported_state_schema_version"}, 3

    issue = _current_obligation_issue(state=state, writer=writer, ledger_path=ledger_path)
    if issue is not None:
        return issue, 3

    return None


def _current_obligation_issue(
    *,
    state: dict[str, object],
    writer: StateWriter,
    ledger_path: Path,
) -> dict[str, object] | None:
    proposal_results = state.get("proposal_results", {})
    if not isinstance(proposal_results, dict):
        return None
    latest_by_target = _latest_accepted_revision_by_target(state, ledger_path)
    live_gpt_pro_gates = _live_gpt_pro_gate_ids(state, ledger_path)
    supported = set(AUTO_SPAWN_ACTIONS) | set(GATED_ACTIONS) | set(NON_EXECUTABLE_ACTIONS)
    for proposal_id, stored in sorted(proposal_results.items()):
        if not isinstance(proposal_id, str) or not isinstance(stored, dict):
            continue
        if stored.get("decision") != "accepted":
            continue
        if writer.is_dispatched(proposal_id):
            continue
        if not _is_current_actionable_obligation(
            state,
            proposal_id,
            stored,
            ledger_path,
            latest_by_target,
        ):
            continue
        action = stored.get("next_required_action")
        if live_gpt_pro_gates and action != "gpt_pro_desktop_review":
            continue
        if action in NON_EXECUTABLE_ACTIONS:
            return {
                "result": "owner_proxy_convergence_required",
                "proposal_id": proposal_id,
                "next_required_action": action,
                "blocked_action": action,
                "forbidden_actions": PRECHECK_FORBIDDEN_ACTIONS,
                "allowed_repair_actions": ["orchestrator_convergence_review"],
            }
        if action not in supported:
            return {
                "result": "unsupported_current_obligation",
                "proposal_id": proposal_id,
                "next_required_action": action,
                "blocked_action": action,
                "forbidden_actions": PRECHECK_FORBIDDEN_ACTIONS,
                "allowed_repair_actions": ["orchestrator_vocab_review"],
            }
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ao-state-writer")
    subparsers = parser.add_subparsers(dest="command", required=True)

    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--root", type=Path, required=True)
    apply_parser.add_argument("--proposal", type=Path, required=True)

    continue_parser = subparsers.add_parser("continue")
    continue_parser.add_argument("--root", type=Path, required=True)
    continue_parser.add_argument("--proposal-id")
    continue_parser.add_argument("--ao-project-id")
    continue_parser.add_argument("--dry-run-continuation", action="store_true")

    list_ready_parser = subparsers.add_parser("list-ready")
    list_ready_parser.add_argument("--root", type=Path, required=True)

    list_gated_parser = subparsers.add_parser("list-gated")
    list_gated_parser.add_argument("--root", type=Path, required=True)

    dispatch_parser = subparsers.add_parser("dispatch")
    dispatch_parser.add_argument("--root", type=Path, required=True)
    dispatch_parser.add_argument("--proposal-id", required=True)
    dispatch_parser.add_argument("--ao-project-id")

    review_job_parser = subparsers.add_parser("review-job")
    review_job_parser.add_argument("--root", type=Path, required=True)
    review_job_parser.add_argument("--proposal-id", required=True)

    gpt_pro_parser = subparsers.add_parser("gpt-pro-actuate")
    gpt_pro_parser.add_argument("--root", type=Path, required=True)
    gpt_pro_parser.add_argument("--proposal-id", required=True)
    gpt_pro_parser.add_argument("--bridge-command")
    gpt_pro_parser.add_argument("--bridge-timeout-seconds", type=int, default=7200)

    reconcile_parser = subparsers.add_parser("reconcile-once")
    reconcile_parser.add_argument("--root", type=Path, required=True)
    reconcile_parser.add_argument("--dry-run", action="store_true")

    preflight_parser = subparsers.add_parser("preflight")
    preflight_parser.add_argument("--status-file", type=Path, required=True)
    preflight_parser.add_argument("--recognized", action="append", default=[])

    repair_parser = subparsers.add_parser("repair-todo")
    repair_parser.add_argument("--todo-file", type=Path, required=True)
    repair_parser.add_argument("--current-phase", required=True)
    repair_parser.add_argument("--next-locked-action", required=True)
    repair_parser.add_argument("--review-gate-state", required=True)
    repair_parser.add_argument("--latest-session-log-anchor", required=True)

    watchdog_parser = subparsers.add_parser("watchdog")
    watchdog_parser.add_argument("--root", type=Path, required=True)
    watchdog_parser.add_argument("--observation", type=Path, required=True)
    watchdog_parser.add_argument("--dry-run", action="store_true")
    watchdog_parser.add_argument("--write-proposal", type=Path)
    watchdog_parser.add_argument("--apply-timeout-blocker", action="store_true")

    authorize_parser = subparsers.add_parser("authorize")
    authorize_parser.add_argument("--root", type=Path, required=True)
    authorize_parser.add_argument("--proposal-id", required=True)
    authorize_parser.add_argument("--evidence", action="append", required=True)
    authorize_parser.add_argument("--scope")

    return parser


def _ready_candidates(
    state: dict[str, object],
    writer: StateWriter,
    ledger_path: Path | None = None,
) -> list[str]:
    """The reconcile predicate, shared by `continue` auto-select, `list-ready`, and dispatch.

    A proposal is 'ready' iff its stored decision is accepted, it has not already been dispatched,
    and it satisfies one of:
      - tier-1: next_required_action is an AUTO_SPAWN action, OR
      - tier-2: next_required_action is gpt_pro_desktop_review AND a fresh orchestrator
        authorization already exists (gate cleared — treat identically to AUTO_SPAWN).
    Returned sorted so callers get a deterministic order.
    """
    proposal_results = state.get("proposal_results", {})
    ledger = ledger_path or writer.ledger_path
    latest_by_target = _latest_accepted_revision_by_target(state, ledger)
    live_gpt_pro_gates = _live_gpt_pro_gate_ids(state, ledger)
    candidates = [
        pid
        for pid, stored in proposal_results.items()
        if isinstance(stored, dict)
        and stored.get("decision") == "accepted"
        and _is_current_actionable_obligation(state, pid, stored, ledger, latest_by_target)
        and (
            not live_gpt_pro_gates
            or stored.get("next_required_action") == "gpt_pro_desktop_review"
        )
        and (
            stored.get("next_required_action") in AUTO_SPAWN_ACTIONS
            or (
                stored.get("next_required_action") == "gpt_pro_desktop_review"
                and writer.is_authorized(pid)
            )
        )
        and not writer.is_dispatched(pid)
    ]
    return sorted(candidates)


def _gated_candidates(
    state: dict[str, object],
    writer: StateWriter,
    ledger_path: Path | None = None,
) -> list[str]:
    """The tier-2 authorization-pending predicate, the GATED mirror of `_ready_candidates`.

    A proposal is 'gated' (a tier-2 candidate awaiting orchestrator authorization) iff its
    stored decision is accepted, its next_required_action is a GATED action (so tier-1
    AUTO_SPAWN actions are excluded), and it has not already been authorized. Unlike tier-1,
    a gated proposal never gets a `dispatched_proposals` entry, so the "already handled"
    predicate is `is_authorized` (a fresh orchestrator authorization), not `is_dispatched`.
    Returned sorted so callers — including the reconcile sweep — get a deterministic order.
    """
    proposal_results = state.get("proposal_results", {})
    ledger = ledger_path or writer.ledger_path
    latest_by_target = _latest_accepted_revision_by_target(state, ledger)
    live_gpt_pro_gates = _live_gpt_pro_gate_ids(state, ledger)
    candidates = [
        pid
        for pid, stored in proposal_results.items()
        if isinstance(stored, dict)
        and stored.get("decision") == "accepted"
        and _is_current_actionable_obligation(state, pid, stored, ledger, latest_by_target)
        and (
            not live_gpt_pro_gates
            or stored.get("next_required_action") == "gpt_pro_desktop_review"
        )
        and stored.get("next_required_action") in GATED_ACTIONS
        and not writer.is_authorized(pid)
    ]
    return sorted(candidates)


def _is_current_actionable_obligation(
    state: dict[str, object],
    proposal_id: str,
    stored: dict[str, object],
    ledger_path: Path | None = None,
    latest_by_target: dict[str, int] | None = None,
) -> bool:
    """Return False for accepted historical proposals that are no longer the live obligation.

    `proposal_results` is an audit history, not a queue.  For GPT Pro gates, the canonical
    live pointer is the target's active_gpt_pro_review_gate_proposal_id.  For other actions
    we use the proposal's target from the append-only ledger when available, require the target
    to still be in the state produced by that proposal, and require the proposal to be the
    latest accepted transition for that target. `dispatch_next_slice_plan_mode` is a global
    "what next?" obligation, so it is current only while it is still the latest state revision.
    """
    next_action = stored.get("next_required_action")
    if next_action == "dispatch_next_slice_plan_mode":
        return stored.get("state_revision") == state.get("state_revision")
    if next_action == "gpt_pro_desktop_review":
        return _is_live_gpt_pro_gate(state, proposal_id, ledger_path)
    if ledger_path is None:
        return True
    proposal = _scan_ledger_for_proposal(ledger_path, proposal_id)
    if proposal is None:
        return True
    target_id = proposal.get("target_id")
    if not isinstance(target_id, str) or not target_id:
        return True
    target = state.get("targets", {}).get(target_id)
    if not isinstance(target, dict):
        return True
    if latest_by_target is not None:
        latest_revision = latest_by_target.get(target_id)
        if isinstance(latest_revision, int) and stored.get("state_revision") != latest_revision:
            return False
    new_state = stored.get("new_state")
    return not isinstance(new_state, str) or target.get("state") == new_state


def _latest_accepted_revision_by_target(
    state: dict[str, object],
    ledger_path: Path | None,
) -> dict[str, int]:
    if ledger_path is None or not ledger_path.exists():
        return {}
    target_by_proposal = _proposal_targets_from_ledger(ledger_path)
    latest: dict[str, int] = {}
    proposal_results = state.get("proposal_results", {})
    if not isinstance(proposal_results, dict):
        return latest
    for pid, stored in proposal_results.items():
        if not isinstance(pid, str) or not isinstance(stored, dict):
            continue
        if stored.get("decision") != "accepted":
            continue
        target_id = target_by_proposal.get(pid)
        revision = stored.get("state_revision")
        if not isinstance(target_id, str) or not isinstance(revision, int):
            continue
        latest[target_id] = max(latest.get(target_id, -1), revision)
    return latest


def _proposal_targets_from_ledger(ledger_path: Path) -> dict[str, str]:
    targets: dict[str, str] = {}
    if not ledger_path.exists():
        return targets
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        proposal = entry.get("proposal")
        if not isinstance(proposal, dict):
            continue
        proposal_id = proposal.get("proposal_id")
        target_id = proposal.get("target_id")
        if isinstance(proposal_id, str) and isinstance(target_id, str):
            targets[proposal_id] = target_id
    return targets


def _is_live_gpt_pro_gate(
    state: dict[str, object],
    proposal_id: str,
    ledger_path: Path | None = None,
) -> bool:
    return proposal_id in _live_gpt_pro_gate_ids(state, ledger_path)


def _live_gpt_pro_gate_ids(state: dict[str, object], ledger_path: Path | None = None) -> set[str]:
    inferred = _inferred_live_gpt_pro_gate_ids(state, ledger_path)
    inferred_target_ids: set[str] = set()
    if inferred and ledger_path is not None and ledger_path.exists():
        target_by_proposal = _proposal_targets_from_ledger(ledger_path)
        inferred_target_ids = {
            target_id for pid, target_id in target_by_proposal.items() if pid in inferred and isinstance(target_id, str)
        }

    gates = state.get("gpt_pro_review_gates", {})
    targets = state.get("targets", {})
    live_ids: set[str] = set(inferred)
    if isinstance(gates, dict) and isinstance(targets, dict):
        for pid, gate in gates.items():
            if not isinstance(pid, str) or not isinstance(gate, dict):
                continue
            target_id = gate.get("target_id")
            if not isinstance(target_id, str):
                continue
            if target_id in inferred_target_ids:
                continue
            target = targets.get(target_id)
            if (
                isinstance(target, dict)
                and target.get("state") == "gpt_pro_review_pending"
                and target.get("active_gpt_pro_review_gate_proposal_id") == pid
            ):
                live_ids.add(pid)
    if isinstance(targets, dict):
        for target_id, target in targets.items():
            if (
                isinstance(target_id, str)
                and isinstance(target, dict)
                and target.get("state") == "gpt_pro_review_pending"
                and isinstance(target.get("active_gpt_pro_review_gate_proposal_id"), str)
            ):
                if target_id in inferred_target_ids:
                    continue
                live_ids.add(target["active_gpt_pro_review_gate_proposal_id"])
    return live_ids


def _inferred_live_gpt_pro_gate_ids(
    state: dict[str, object],
    ledger_path: Path | None,
) -> set[str]:
    """Infer current GPT Pro gates for legacy states whose active pointer predates
    the latest accepted gpt_pro_review_pending decision.

    New writer versions persist ``active_gpt_pro_review_gate_proposal_id`` and
    ``gpt_pro_review_gates`` at apply time. Older accepted states may have a
    fresh accepted ``next_required_action=gpt_pro_desktop_review`` in
    ``proposal_results`` plus package metadata in the append-only ledger, while
    the target pointer still names an older gate. Reconcile/list commands are
    read-only, so they must derive the live gate instead of silently returning
    an empty gated set.
    """
    if ledger_path is None or not ledger_path.exists():
        return set()
    proposal_results = state.get("proposal_results", {})
    targets = state.get("targets", {})
    if not isinstance(proposal_results, dict) or not isinstance(targets, dict):
        return set()
    target_by_proposal = _proposal_targets_from_ledger(ledger_path)
    latest: dict[str, tuple[int, str]] = {}
    for pid, stored in proposal_results.items():
        if not isinstance(pid, str) or not isinstance(stored, dict):
            continue
        if stored.get("decision") != "accepted":
            continue
        if stored.get("next_required_action") != "gpt_pro_desktop_review":
            continue
        revision = stored.get("state_revision")
        if not isinstance(revision, int):
            continue
        target_id = target_by_proposal.get(pid)
        if not isinstance(target_id, str):
            continue
        target = targets.get(target_id)
        if not isinstance(target, dict) or target.get("state") != "gpt_pro_review_pending":
            continue
        current = latest.get(target_id)
        if current is None or revision > current[0]:
            latest[target_id] = (revision, pid)
    return {pid for _, pid in latest.values()}


def _dispatch_one(
    *,
    root: Path,
    writer: StateWriter,
    ledger_path: Path,
    proposal_id: str,
    ao_project_id: str | None,
    dry_run: bool,
) -> int:
    """Reconstruct, gate, and dispatch exactly ONE explicit proposal through the
    claim -> evaluate -> spawn -> confirm/release path. Shared by `continue --proposal-id <pid>`
    and the `dispatch` subcommand so both return identical result strings + exit codes.
    """
    preflight = preflight_reconcile(root=root, writer=writer, ledger_path=ledger_path)
    if preflight is not None:
        payload, exit_code = preflight
        payload.setdefault("proposal_id", proposal_id)
        return _emit(payload, exit_code)
    try:
        state = _read_state(writer.state_path)
    except UnsupportedStateSchemaVersion:
        return _unsupported_state_schema()
    contract_error = _validate_contract(root)
    if contract_error is not None:
        return _emit({"result": contract_error, "proposal_id": proposal_id}, 3)
    proposal_results = state.get("proposal_results", {})

    # 1. Reconstruct the decision (live state first, then the append-only ledger).
    stored = proposal_results.get(proposal_id)
    if stored is None:
        stored = _scan_ledger_for_decision(ledger_path, proposal_id)
    if stored is None:
        return _emit({"result": "unknown_proposal_id", "proposal_id": proposal_id}, 4)
    stored = dict(stored)
    stored.pop("replayed", None)
    decision = StateTransitionDecision(**stored)

    # 2. Gate on the reconstructed decision.
    if decision.decision != "accepted":
        return _emit({"result": "not_accepted", "proposal_id": proposal_id}, 0)
    if decision.next_required_action in GATED_ACTIONS:
        # tier-2: gate stays closed until a live orchestrator authorization is recorded.
        if writer.is_authorized(proposal_id):
            if decision.next_required_action == "gpt_pro_desktop_review":
                command = _read_gpt_pro_desktop_actuator_command(root)
                if not command:
                    return _emit(
                        {
                            "result": "missing_gpt_pro_desktop_actuator_command",
                            "proposal_id": proposal_id,
                            "next_required_action": decision.next_required_action,
                        },
                        3,
                    )
                if dry_run:
                    return _emit(
                        {
                            "result": "would_actuate_gpt_pro_review",
                            "proposal_id": proposal_id,
                            "cmd": [
                                "ao-state-writer",
                                "gpt-pro-actuate",
                                "--root",
                                str(root),
                                "--proposal-id",
                                proposal_id,
                                "--bridge-command",
                                command,
                            ],
                        },
                        0,
                    )
                return _claim_and_run_gpt_pro_actuator_cli(
                    root=root,
                    writer=writer,
                    ledger_path=ledger_path,
                    proposal_id=proposal_id,
                    bridge_command=command,
                    bridge_timeout_seconds=7200,
                )
            return _emit(
                {
                    "result": "authorized",
                    "proposal_id": proposal_id,
                    "next_required_action": decision.next_required_action,
                },
                0,
            )
        return _emit(
            {
                "result": "requires_orchestrator_authorization",
                "proposal_id": proposal_id,
                "next_required_action": decision.next_required_action,
            },
            3,
        )
    if decision.next_required_action not in AUTO_SPAWN_ACTIONS:
        return _emit(
            {
                "result": "unsupported_current_obligation",
                "proposal_id": proposal_id,
                "blocked_action": decision.next_required_action,
                "next_required_action": decision.next_required_action,
                "forbidden_actions": PRECHECK_FORBIDDEN_ACTIONS,
                "allowed_repair_actions": ["orchestrator_vocab_review"],
            },
            3,
        )

    # 3. Idempotency: already consumed.
    if writer.is_dispatched(proposal_id):
        return _emit({"result": "already_continued", "proposal_id": proposal_id}, 0)

    # 3b. major_closure_candidate guard: independently re-validate GPT Pro receipt verdict
    # before auto-spawning closure.  _classify_verdict uses last-match regex on raw text;
    # a mis-parse cannot silently escalate to closure.  We re-read the receipt artifact and
    # confirm the verdict is a genuine pass-family token (not blocker) via a second regex.
    # Fail-closed: any doubt keeps the gate open and returns a human-readable rejection.
    if decision.next_required_action == "major_closure_candidate":
        try:
            _guard_state = _read_state(writer.state_path)
        except UnsupportedStateSchemaVersion:
            return _unsupported_state_schema()
        closure_guard = _gpt_pro_closure_receipt_guard(root, _guard_state, ledger_path, proposal_id)
        if closure_guard is not None:
            return _emit(
                {
                    "result": "major_closure_blocked_by_receipt_guard",
                    "proposal_id": proposal_id,
                    "reason": closure_guard,
                },
                3,
            )

    if dry_run:
        result = evaluate_continuation(
            root=root,
            decision=decision,
            ao_project_id=ao_project_id,
        )
        if result.decision == "blocked":
            return _emit(
                {"result": "blocked", "proposal_id": proposal_id, "reasons": result.blockers or []},
                3,
            )
        if result.decision != "candidate":
            return _emit(
                {
                    "result": result.decision,
                    "proposal_id": proposal_id,
                    "next_required_action": result.next_required_action,
                },
                0,
            )
        return _emit({"result": "would_spawn", "proposal_id": proposal_id, "cmd": result.would_run or []}, 0)

    # 4. Claim the dispatch (single-flight). is_dispatched already caught the consumed
    #    ("spawned") case above, so a lost claim here means a peer holds a live pending lease.
    if not writer.claim_dispatch(proposal_id):
        return _emit({"result": "in_flight", "proposal_id": proposal_id}, 0)

    # 5. Build the spawn candidate from FRESH governance/TODO/template state, then dispatch.
    result = continue_after_apply(
        root=root,
        decision=decision,
        ao_project_id=ao_project_id,
        dry_run=False,
    )
    if result.decision == "blocked":
        writer.release_dispatch(proposal_id)
        return _emit(
            {"result": "blocked", "proposal_id": proposal_id, "reasons": result.blockers or []},
            3,
        )
    if result.decision == "spawn_failed":
        writer.release_dispatch(proposal_id)
        return _emit(
            {"result": "spawn_failed", "proposal_id": proposal_id, "detail": result.reason},
            3,
        )
    if result.decision == "spawned_without_session_attestation":
        writer.confirm_dispatch(
            proposal_id,
            authorized_by="orchestrator_policy",
            spawn_attestation="missing",
        )
        return _emit(
            {
                "result": "spawned_without_session_attestation",
                "proposal_id": proposal_id,
                "detail": result.reason,
            },
            3,
        )
    if result.decision != "spawned":
        # gated / skipped — not a spawn candidate; release and report.
        writer.release_dispatch(proposal_id)
        return _emit(
            {
                "result": result.decision,
                "proposal_id": proposal_id,
                "next_required_action": result.next_required_action,
            },
            0,
        )

    writer.confirm_dispatch(
        proposal_id,
        authorized_by="orchestrator_policy",
        spawn_session_id=result.spawn_session_id,
    )
    return _emit({"result": "spawned", "proposal_id": proposal_id, "spawn_session_id": result.spawn_session_id}, 0)


def cmd_continue(args: argparse.Namespace) -> int:
    state_path, ledger_path = _state_paths(args.root)
    writer = StateWriter(state_path=state_path, ledger_path=ledger_path)

    preflight = preflight_reconcile(root=args.root, writer=writer, ledger_path=ledger_path)
    if preflight is not None:
        payload, exit_code = preflight
        return _emit(payload, exit_code)

    # Resolve proposal_id: explicit flag, or auto-select the UNIQUE ready candidate.
    if args.proposal_id:
        proposal_id = args.proposal_id
    else:
        try:
            state = _read_state(state_path)
            candidates = _ready_candidates(state, writer, ledger_path)
        except UnsupportedStateSchemaVersion:
            return _unsupported_state_schema()
        if len(candidates) == 0:
            gated = _gated_candidates(state, writer, ledger_path)
            if gated:
                return _emit(
                    {
                        "result": "requires_orchestrator_authorization",
                        "candidates": gated,
                    },
                    3,
                )
            return _emit({"result": "nothing_to_continue"}, 0)
        if len(candidates) > 1:
            return _emit({"result": "ambiguous_proposal_id", "candidates": candidates}, 4)
        proposal_id = candidates[0]

    return _dispatch_one(
        root=args.root,
        writer=writer,
        ledger_path=ledger_path,
        proposal_id=proposal_id,
        ao_project_id=args.ao_project_id,
        dry_run=args.dry_run_continuation,
    )


def cmd_list_ready(args: argparse.Namespace) -> int:
    state_path, ledger_path = _state_paths(args.root)
    writer = StateWriter(state_path=state_path, ledger_path=ledger_path)
    preflight = preflight_reconcile(root=args.root, writer=writer, ledger_path=ledger_path)
    if preflight is not None:
        payload, exit_code = preflight
        return _emit(payload, exit_code)
    try:
        candidates = _ready_candidates(_read_state(state_path), writer, ledger_path)
    except UnsupportedStateSchemaVersion:
        return _unsupported_state_schema()
    return _emit({"result": "ready", "candidates": candidates}, 0)


def cmd_list_gated(args: argparse.Namespace) -> int:
    state_path, ledger_path = _state_paths(args.root)
    writer = StateWriter(state_path=state_path, ledger_path=ledger_path)
    preflight = preflight_reconcile(root=args.root, writer=writer, ledger_path=ledger_path)
    if preflight is not None:
        payload, exit_code = preflight
        return _emit(payload, exit_code)
    try:
        candidates = _gated_candidates(_read_state(state_path), writer, ledger_path)
    except UnsupportedStateSchemaVersion:
        return _unsupported_state_schema()
    return _emit({"result": "gated", "candidates": candidates}, 0)


def cmd_dispatch(args: argparse.Namespace) -> int:
    state_path, ledger_path = _state_paths(args.root)
    writer = StateWriter(state_path=state_path, ledger_path=ledger_path)
    return _dispatch_one(
        root=args.root,
        writer=writer,
        ledger_path=ledger_path,
        proposal_id=args.proposal_id,
        ao_project_id=args.ao_project_id,
        dry_run=False,
    )


def cmd_reconcile_once(args: argparse.Namespace) -> int:
    state_path, ledger_path = _state_paths(args.root)
    writer = StateWriter(state_path=state_path, ledger_path=ledger_path)
    preflight = preflight_reconcile(root=args.root, writer=writer, ledger_path=ledger_path, dry_run=True)
    if preflight is not None:
        payload, exit_code = preflight
        return _emit(payload, exit_code)
    try:
        state = _read_state(state_path)
    except UnsupportedStateSchemaVersion:
        return _unsupported_state_schema()
    ready = _ready_candidates(state, writer, ledger_path)
    gated = _gated_candidates(state, writer, ledger_path)
    if gated and not ready:
        return _emit(
            {
                "result": "requires_orchestrator_authorization",
                "ready_candidates": ready,
                "gated_candidates": gated,
            },
            3,
        )
    if ready:
        return _emit({"result": "ready", "ready_candidates": ready, "gated_candidates": gated}, 0)
    return _emit({"result": "nothing_to_continue", "ready_candidates": [], "gated_candidates": []}, 0)


def _build_review_job_payload(
    *,
    root: Path,
    writer: StateWriter,
    state_path: Path,
    ledger_path: Path,
    proposal_id: str,
) -> tuple[dict[str, object], int]:
    preflight = preflight_reconcile(root=root, writer=writer, ledger_path=ledger_path)
    if preflight is not None:
        return preflight
    contract_error = _validate_contract(root)
    if contract_error is not None:
        return {"result": contract_error, "proposal_id": proposal_id}, 3
    try:
        state = _read_state(state_path)
    except UnsupportedStateSchemaVersion:
        return {"result": "unsupported_state_schema_version"}, 3
    stored = state.get("proposal_results", {}).get(proposal_id)
    if stored is None:
        return {"result": "unknown_proposal_id", "proposal_id": proposal_id}, 4
    stored = dict(stored)
    stored.pop("replayed", None)
    decision = StateTransitionDecision(**stored)
    if decision.decision != "accepted":
        return {"result": "not_accepted", "proposal_id": proposal_id}, 0
    if decision.next_required_action != "gpt_pro_desktop_review":
        return {
            "result": "not_gpt_pro_review_gate",
            "proposal_id": proposal_id,
            "next_required_action": decision.next_required_action,
        }, 4
    if not writer.is_authorized(proposal_id):
        return {
            "result": "requires_orchestrator_authorization",
            "proposal_id": proposal_id,
            "next_required_action": decision.next_required_action,
        }, 3
    authorization = writer.authorization_record(proposal_id) or {}

    proposal = _scan_ledger_for_proposal(ledger_path, proposal_id)
    if proposal is None:
        return {"result": "missing_proposal_ledger_entry", "proposal_id": proposal_id}, 3
    package = authorization.get("gpt_pro_review_gate", {})
    if not isinstance(package, dict) or not package.get("package_sha256"):
        return {"result": "missing_gpt_pro_package", "proposal_id": proposal_id}, 3
    target_id = package.get("target_id") or proposal.get("target_id")
    target_kind = package.get("target_kind") or proposal.get("target_kind")
    target = state.get("targets", {}).get(target_id, {}) if isinstance(target_id, str) else {}
    if isinstance(target, dict) and target.get("active_gpt_pro_review_gate_proposal_id") != proposal_id:
        return {"result": "stale_gpt_pro_review_gate", "proposal_id": proposal_id}, 3

    package_sha256 = package.get("package_sha256")
    submission_nonce = package.get("external_review_submission_nonce")
    actuator_command = _read_gpt_pro_desktop_actuator_command(root)
    review_transport = _infer_gpt_pro_review_transport(actuator_command)
    return {
        "result": "review_job",
        "proposal_id": proposal_id,
        "target_id": target_id,
        "target_kind": target_kind,
        "review_scope": "gpt_pro",
        "next_required_action": decision.next_required_action,
        "package_path": package.get("package_path"),
        "prompt_path": package.get("prompt_path"),
        "package_sha256": package_sha256,
        "external_review_submission_nonce": submission_nonce,
        "human_egress_required": False,
        "desktop_actuator_required": review_transport == "chatgpt_desktop_aqua",
        "browser_cdp_actuator_required": review_transport == "chatgpt_browser_cdp",
        "external_actuator_required": True,
        "unattended_required": True,
        "actuator_identity_internal": True,
        "manual_receipt_apply_forbidden": True,
        "desktop_transport": review_transport,
        "review_transport": review_transport,
        "api_transport_allowed": False,
        "required_model_class": "pro",
        "required_caller_type": "gpt_pro_review_actuator",
        "artifact_ref_format": "artifact:reports/<relative-file>",
        "required_receipt_fields": [
            "package_sha256",
            "external_review_receipt_sha256",
            "external_review_submission_nonce",
            "external_review_artifact_ref",
            "external_review_gate_proposal_id",
        ],
        "receipt_proposal_template": {
            "target_kind": target_kind,
            "target_id": target_id,
            "base_state_revision": state.get("state_revision", 0),
            "requested_state": "closure_candidate",
            "actor_role": "gpt_pro",
            "review_scope": "gpt_pro",
            "package_sha256": package_sha256,
            "external_review_receipt_sha256": "<sha256-of-captured-review-receipt>",
            "external_review_submission_nonce": submission_nonce,
            "external_review_artifact_ref": "artifact:reports/<receipt-artifact>",
            "external_review_gate_proposal_id": proposal_id,
        },
    }, 0


def cmd_review_job(args: argparse.Namespace) -> int:
    state_path, ledger_path = _state_paths(args.root)
    writer = StateWriter(state_path=state_path, ledger_path=ledger_path)
    payload, exit_code = _build_review_job_payload(
        root=args.root,
        writer=writer,
        state_path=state_path,
        ledger_path=ledger_path,
        proposal_id=args.proposal_id,
    )
    return _emit(payload, exit_code)


def cmd_gpt_pro_actuate(args: argparse.Namespace) -> int:
    state_path, ledger_path = _state_paths(args.root)
    writer = StateWriter(state_path=state_path, ledger_path=ledger_path)
    preflight = preflight_reconcile(root=args.root, writer=writer, ledger_path=ledger_path)
    if preflight is not None:
        payload, exit_code = preflight
        payload.setdefault("proposal_id", args.proposal_id)
        return _emit(payload, exit_code)
    contract_bridge_command = _read_gpt_pro_desktop_actuator_command(args.root)
    if not contract_bridge_command:
        return _emit(
            {
                "result": "missing_gpt_pro_desktop_actuator_command",
                "proposal_id": args.proposal_id,
            },
            3,
        )
    try:
        contract_command_tokens = shlex.split(contract_bridge_command)
        requested_command_tokens = shlex.split(args.bridge_command) if args.bridge_command else None
    except ValueError as exc:
        return _emit(
            {
                "result": "invalid_gpt_pro_desktop_actuator_command",
                "proposal_id": args.proposal_id,
                "detail": str(exc),
            },
            3,
        )
    if requested_command_tokens is not None and requested_command_tokens != contract_command_tokens:
        return _emit(
            {
                "result": "unauthorized_bridge_command_override",
                "proposal_id": args.proposal_id,
            },
            3,
        )
    return _claim_and_run_gpt_pro_actuator_cli(
        root=args.root,
        writer=writer,
        ledger_path=ledger_path,
        proposal_id=args.proposal_id,
        bridge_command=contract_bridge_command,
        bridge_timeout_seconds=args.bridge_timeout_seconds,
    )


def _claim_and_run_gpt_pro_actuator_cli(
    *,
    root: Path,
    writer: StateWriter,
    ledger_path: Path,
    proposal_id: str,
    bridge_command: str,
    bridge_timeout_seconds: int,
) -> int:
    receipt_proposal_id = _gpt_pro_receipt_proposal_id(proposal_id)
    try:
        state = _read_state(writer.state_path)
    except UnsupportedStateSchemaVersion:
        return _unsupported_state_schema()
    existing = state.get("proposal_results", {}).get(receipt_proposal_id)
    if isinstance(existing, dict) and existing.get("decision") == "accepted":
        if not writer.is_dispatched(proposal_id):
            writer.confirm_dispatch(proposal_id, authorized_by="gpt_pro_review_actuator")
        return _emit(
            {
                "result": "receipt_already_recorded",
                "proposal_id": proposal_id,
                "receipt_proposal_id": receipt_proposal_id,
            },
            0,
        )
    if not writer.claim_dispatch(proposal_id):
        return _emit({"result": "in_flight", "proposal_id": proposal_id}, 0)
    exit_code = _run_gpt_pro_actuator_cli(
        root=root,
        writer=writer,
        ledger_path=ledger_path,
        proposal_id=proposal_id,
        bridge_command=bridge_command,
        bridge_timeout_seconds=bridge_timeout_seconds,
    )
    if exit_code == 0:
        writer.confirm_dispatch(proposal_id, authorized_by="gpt_pro_review_actuator")
    else:
        writer.release_dispatch(proposal_id)
    return exit_code


def _run_gpt_pro_actuator_cli(
    *,
    root: Path,
    writer: StateWriter,
    ledger_path: Path,
    proposal_id: str,
    bridge_command: str,
    bridge_timeout_seconds: int,
) -> int:
    job, job_exit = _build_review_job_payload(
        root=root,
        writer=writer,
        state_path=writer.state_path,
        ledger_path=ledger_path,
        proposal_id=proposal_id,
    )
    if job_exit != 0:
        return _emit(job, job_exit)

    receipt_proposal_id = _gpt_pro_receipt_proposal_id(proposal_id)
    try:
        state = _read_state(writer.state_path)
    except UnsupportedStateSchemaVersion:
        return _unsupported_state_schema()
    existing = state.get("proposal_results", {}).get(receipt_proposal_id)
    if isinstance(existing, dict) and existing.get("decision") == "accepted":
        return _emit(
            {
                "result": "receipt_already_recorded",
                "proposal_id": proposal_id,
                "receipt_proposal_id": receipt_proposal_id,
            },
            0,
        )

    package_path_result = _resolve_review_job_file(root, job.get("package_path"), "package_path")
    if isinstance(package_path_result, dict):
        return _emit(package_path_result, 3)
    prompt_path_result = _resolve_review_job_file(root, job.get("prompt_path"), "prompt_path")
    if isinstance(prompt_path_result, dict):
        return _emit(prompt_path_result, 3)
    package_path = package_path_result
    prompt_path = prompt_path_result

    expected_package_sha = job.get("package_sha256")
    actual_package_sha = hashlib.sha256(package_path.read_bytes()).hexdigest()
    if expected_package_sha != actual_package_sha:
        return _emit(
            {
                "result": "package_file_sha_mismatch",
                "proposal_id": proposal_id,
                "expected_package_sha256": expected_package_sha,
                "actual_package_sha256": actual_package_sha,
            },
            3,
        )

    bridge_result, bridge_exit = _run_gpt_pro_bridge(
        root=root,
        command=bridge_command,
        job=job,
        timeout_seconds=bridge_timeout_seconds,
    )
    if bridge_exit != 0:
        return _record_gpt_pro_actuator_failure(
            writer=writer,
            job=job,
            proposal_id=proposal_id,
            reason=str(bridge_result.get("reason") or bridge_result.get("result") or "bridge_failed"),
            detail=str(bridge_result.get("detail") or bridge_result.get("stderr") or bridge_result.get("stdout") or ""),
            expected_package_sha=str(expected_package_sha),
        )

    bridge_package_sha = bridge_result.get("package_sha256")
    if bridge_package_sha != expected_package_sha:
        return _emit(
            {
                "result": "gpt_pro_package_sha_mismatch",
                "proposal_id": proposal_id,
                "expected_package_sha256": expected_package_sha,
                "actual_package_sha256": bridge_package_sha,
            },
            3,
        )

    bridge_artifact_ref = bridge_result.get("external_review_artifact_ref")
    if bridge_artifact_ref is not None and not _valid_reports_artifact_ref(bridge_artifact_ref):
        return _emit(
            {
                "result": "invalid_bridge_artifact_ref",
                "proposal_id": proposal_id,
                "external_review_artifact_ref": bridge_artifact_ref,
            },
            3,
        )

    nonce = bridge_result.get("external_review_submission_nonce") or bridge_result.get("submission_nonce")
    if not isinstance(nonce, str) or not nonce:
        return _emit({"result": "missing_external_review_submission_nonce", "proposal_id": proposal_id}, 3)
    verdict = bridge_result.get("verdict")
    if verdict not in {"advisory", "pass", "pass_with_nits", "blocker"}:
        return _record_gpt_pro_actuator_failure(
            writer=writer,
            job=job,
            proposal_id=proposal_id,
            reason="unsupported_gpt_pro_verdict",
            detail=str(verdict),
            expected_package_sha=str(expected_package_sha),
        )

    artifact_result = _copy_bridge_artifact_to_reports(
        root=root,
        proposal_id=proposal_id,
        nonce=nonce,
        bridge_result=bridge_result,
    )
    if artifact_result.get("result") != "artifact_ready":
        return _emit({"proposal_id": proposal_id, **artifact_result}, 3)

    artifact_ref = artifact_result["external_review_artifact_ref"]
    artifact_sha = artifact_result["external_review_receipt_sha256"]
    template = job.get("receipt_proposal_template")
    if not isinstance(template, dict):
        return _emit({"result": "missing_receipt_proposal_template", "proposal_id": proposal_id}, 3)

    proposal = StateTransitionProposal(
        proposal_id=receipt_proposal_id,
        target_kind=str(template.get("target_kind")),
        target_id=str(template.get("target_id")),
        base_state_revision=int(template.get("base_state_revision", 0)),
        requested_state="closure_candidate",
        actor_role="gpt_pro",
        evidence_refs=[
            f"gpt-pro-actuator:{proposal_id}",
            artifact_ref,
            f"package_sha256:{expected_package_sha}",
        ],
        summary=str(bridge_result.get("summary") or ""),
        review_scope="gpt_pro",
        verdict=str(verdict),
        model=str(bridge_result.get("model_slug") or bridge_result.get("model") or ""),
        blocker_code=(
            str(bridge_result.get("blocker_code"))
            if verdict == "blocker" and bridge_result.get("blocker_code")
            else None
        ),
        blocker_detail=(
            str(bridge_result.get("blocker_detail") or bridge_result.get("summary") or "")
            if verdict == "blocker"
            else None
        ),
        package_sha256=str(expected_package_sha),
        non_retryable=bool(bridge_result.get("non_retryable", False)),
        external_review_receipt_sha256=str(artifact_sha),
        external_review_submission_nonce=nonce,
        external_review_artifact_ref=str(artifact_ref),
        external_review_gate_proposal_id=proposal_id,
    )

    previous_caller = os.environ.get(CALLER_TYPE_ENV)
    os.environ[CALLER_TYPE_ENV] = "gpt_pro_review_actuator"
    try:
        decision = writer.apply(proposal)
    finally:
        if previous_caller is None:
            os.environ.pop(CALLER_TYPE_ENV, None)
        else:
            os.environ[CALLER_TYPE_ENV] = previous_caller

    if decision.decision == "rejected":
        return _emit(
            {
                "result": "receipt_rejected",
                "proposal_id": proposal_id,
                "receipt_proposal_id": receipt_proposal_id,
                "reason": decision.reason,
            },
            2,
        )

    return _emit(
        {
            "result": "gpt_pro_review_recorded",
            "proposal_id": proposal_id,
            "receipt_proposal_id": receipt_proposal_id,
            "state_revision": decision.state_revision,
            "next_required_action": decision.next_required_action,
            "new_state": decision.new_state,
            "external_review_artifact_ref": artifact_ref,
            "external_review_receipt_sha256": artifact_sha,
        },
        0,
    )


def _record_gpt_pro_actuator_failure(
    *,
    writer: StateWriter,
    job: dict[str, object],
    proposal_id: str,
    reason: str,
    detail: str,
    expected_package_sha: str,
) -> int:
    template = job.get("receipt_proposal_template")
    if not isinstance(template, dict):
        return _emit({"result": "missing_receipt_proposal_template", "proposal_id": proposal_id}, 3)

    failure_proposal_id = _gpt_pro_actuator_failure_proposal_id(proposal_id)
    proposal = StateTransitionProposal(
        proposal_id=failure_proposal_id,
        target_kind=str(template.get("target_kind")),
        target_id=str(template.get("target_id")),
        base_state_revision=int(template.get("base_state_revision", 0)),
        requested_state="closure_candidate",
        actor_role="gpt_pro",
        evidence_refs=[
            f"gpt-pro-actuator-failure:{proposal_id}",
            f"package_sha256:{expected_package_sha}",
            f"reason:{_safe_component(reason)}",
        ],
        summary=f"GPT Pro actuator failed before a usable external review receipt was harvested: {reason}",
        review_scope="gpt_pro",
        verdict="blocker",
        model="chatgpt_desktop_or_browser_actuator",
        review_mode="actuator_failure",
        blocker_code=GPT_PRO_ACTUATOR_FAILURE_BLOCKER_CODE,
        blocker_detail=detail or reason,
        package_sha256=expected_package_sha,
        external_review_gate_proposal_id=proposal_id,
    )

    previous_caller = os.environ.get(CALLER_TYPE_ENV)
    os.environ[CALLER_TYPE_ENV] = "gpt_pro_review_actuator"
    try:
        decision = writer.apply(proposal)
    finally:
        if previous_caller is None:
            os.environ.pop(CALLER_TYPE_ENV, None)
        else:
            os.environ[CALLER_TYPE_ENV] = previous_caller

    if decision.decision == "rejected":
        return _emit(
            {
                "result": "gpt_pro_actuator_failure_rejected",
                "proposal_id": proposal_id,
                "failure_proposal_id": failure_proposal_id,
                "reason": decision.reason,
                "bridge_failure_reason": reason,
            },
            2,
        )

    return _emit(
        {
            "result": "gpt_pro_actuator_failure_recorded",
            "proposal_id": proposal_id,
            "failure_proposal_id": failure_proposal_id,
            "bridge_failure_reason": reason,
            "state_revision": decision.state_revision,
            "next_required_action": decision.next_required_action,
            "new_state": decision.new_state,
        },
        0,
    )


def _run_gpt_pro_bridge(
    *,
    root: Path,
    command: str,
    job: dict[str, object],
    timeout_seconds: int,
) -> tuple[dict[str, object], int]:
    bridge_env = os.environ.copy()
    bridge_env["AO_GPT_PRO_PROPOSAL_ID"] = str(job.get("proposal_id") or "")
    bridge_env["AO_GPT_PRO_SUBMISSION_NONCE"] = str(job.get("external_review_submission_nonce") or "")
    bridge_env["AO_GPT_PRO_PACKAGE_SHA256"] = str(job.get("package_sha256") or "")
    try:
        completed = subprocess.run(
            shlex.split(command),
            cwd=root,
            input=json.dumps(job, ensure_ascii=False, sort_keys=True),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
            env=bridge_env,
        )
    except FileNotFoundError as exc:
        return {"result": "bridge_failed", "reason": "bridge_command_not_found", "detail": str(exc)}, 3
    except subprocess.TimeoutExpired as exc:
        return {"result": "bridge_failed", "reason": "review_timeout", "detail": str(exc)}, 3

    if completed.returncode != 0:
        failure: dict[str, object] = {
            "result": "bridge_failed",
            "reason": "bridge_command_failed",
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            failure["reason"] = str(payload.get("error") or payload.get("reason") or "bridge_command_failed")
            for key in ("error", "artifact_path", "stdout", "stderr"):
                if key in payload:
                    failure[f"bridge_{key}"] = payload[key]
        return failure, 3
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return {"result": "invalid_bridge_output", "detail": str(exc), "stdout": completed.stdout}, 3
    if not isinstance(payload, dict):
        return {"result": "invalid_bridge_output", "detail": "bridge output must be a JSON object"}, 3
    if payload.get("ok") is not True:
        return {
            "result": "bridge_failed",
            "reason": str(payload.get("error") or payload.get("reason") or "unknown_bridge_failure"),
        }, 3
    return payload, 0


def _copy_bridge_artifact_to_reports(
    *,
    root: Path,
    proposal_id: str,
    nonce: str,
    bridge_result: dict[str, object],
) -> dict[str, object]:
    artifact_path = bridge_result.get("artifact_path") or bridge_result.get("verdict_json") or bridge_result.get("raw_file")
    if isinstance(artifact_path, str) and artifact_path:
        source = Path(artifact_path).expanduser()
        if not source.is_absolute():
            source = root / source
    else:
        artifact_ref = bridge_result.get("external_review_artifact_ref")
        if not isinstance(artifact_ref, str) or not _valid_reports_artifact_ref(artifact_ref):
            return {"result": "missing_bridge_artifact"}
        source = root / artifact_ref.removeprefix("artifact:")

    try:
        source = source.resolve()
        source.relative_to(root.resolve())
    except ValueError:
        return {"result": "invalid_bridge_artifact_path", "artifact_path": str(source)}

    if not source.exists() or not source.is_file():
        return {"result": "missing_bridge_artifact", "artifact_path": str(source)}

    suffix = source.suffix if source.suffix else ".txt"
    destination = (
        root
        / "reports"
        / "gpt-pro-receipts"
        / f"{_safe_component(proposal_id)}-{_safe_component(nonce)}{suffix}"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_bytes = source.read_bytes()
    if destination.exists():
        if destination.read_bytes() != source_bytes:
            return {
                "result": "receipt_artifact_collision",
                "external_review_artifact_ref": f"artifact:{destination.relative_to(root)}",
            }
    else:
        shutil.copyfile(source, destination)
    artifact_sha = hashlib.sha256(destination.read_bytes()).hexdigest()
    return {
        "result": "artifact_ready",
        "external_review_artifact_ref": f"artifact:{destination.relative_to(root)}",
        "external_review_receipt_sha256": artifact_sha,
    }


def _resolve_review_job_file(root: Path, value: object, field_name: str) -> Path | dict[str, object]:
    if not isinstance(value, str) or not value:
        return {"result": f"missing_{field_name}"}
    path = Path(value)
    full = path if path.is_absolute() else root / path
    try:
        full = full.resolve()
        full.relative_to(root.resolve())
    except ValueError:
        return {"result": f"invalid_{field_name}", field_name: value}
    if not full.exists() or not full.is_file():
        return {"result": f"missing_{field_name}", field_name: value}
    return full


def _valid_reports_artifact_ref(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("artifact:reports/"):
        return False
    rel_path = Path(value.removeprefix("artifact:"))
    return not rel_path.is_absolute() and ".." not in rel_path.parts and rel_path.parts[:1] == ("reports",)


def _safe_component(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")
    return sanitized or "review"


def _gpt_pro_receipt_proposal_id(proposal_id: str) -> str:
    return f"gpt-pro-receipt-{_safe_component(proposal_id)}"


def _gpt_pro_actuator_failure_proposal_id(proposal_id: str) -> str:
    return f"gpt-pro-actuator-failure-{_safe_component(proposal_id)}"


# Pass-family verdicts that may advance to major closure.
_GPT_PRO_PASS_VERDICTS = frozenset({"pass", "pass_with_nits", "advisory"})
# Independent re-validation regex — does NOT rely on last-match semantics; requires
# the verdict field to appear as a JSON-key-like token immediately before its value.
_RECEIPT_VERDICT_RE = re.compile(
    r'(?i)\bverdict\b.{0,10}[:：].{0,5}(blocker|pass_with_nits|advisory|pass)\b'
)
# Blocker-semantic signal regex (Hole 1 fix): fail-closed if the artifact contains
# a non-null blocker_code field OR a severity=blocker field in any finding, regardless
# of what the top-level verdict token says.  Does NOT fire on "blocker_code": null/""
# so legitimate pass receipts with null blocker_code are not incorrectly rejected.
_BLOCKER_SIGNAL_RE = re.compile(
    r'(?i)"blocker_code"\s*:\s*(?![\s"]*null\b|[\s"]*"\s*"\s*[,}\]])'
    r'|"severity"\s*:\s*"blocker"'
)


def _gpt_pro_closure_receipt_guard(
    root: Path, state: dict, ledger_path: Path, proposal_id: str
) -> str | None:
    """Return an error string if the GPT Pro receipt cannot be independently re-validated
    as a genuine pass-family verdict, or None if all checks pass.

    Binds directly to the receipt proposal in the ledger (proposal_id IS the receipt
    proposal id) to prevent both cross-target bypass and stale-receipt bypass:
      1. Ledger verdict must be pass-family (not blocker).
      2. Artifact ref extracted from ledger proposal evidence_refs (not target[-1]).
      3. Raw artifact: no blocker-semantic fields (severity=blocker, non-null blocker_code).
      4. Raw artifact: independent _RECEIPT_VERDICT_RE scan confirms pass-family.

    Fail-closed: any doubt returns an error string; None clears the gate.
    """
    # Resolve both target_id AND the receipt's evidence from the ledger proposal.
    # proposal_id here is the receipt proposal (gpt-pro-receipt-XXX), not the gate proposal,
    # so ledger_proposal.verdict and ledger_proposal.evidence_refs are the authoritative
    # receipt fields — no need to traverse target.review_receipts at all.
    ledger_proposal = _scan_ledger_for_proposal(ledger_path, proposal_id)
    if ledger_proposal is None:
        return f"proposal {proposal_id!r} not found in ledger; cannot re-validate"
    target_id = ledger_proposal.get("target_id")
    if not isinstance(target_id, str) or not target_id:
        return f"proposal {proposal_id!r} ledger entry has no target_id; cannot re-validate"

    # 1. Verdict from ledger proposal (Hole 2 fix: bound to this proposal, not last receipt).
    stored_verdict = ledger_proposal.get("verdict")
    if stored_verdict not in _GPT_PRO_PASS_VERDICTS:
        return (
            f"receipt proposal {proposal_id!r} verdict {stored_verdict!r} is not pass-family; "
            "major closure requires pass/advisory/pass_with_nits"
        )

    # 2. Artifact ref from ledger proposal evidence_refs (Hole 2 fix: exact binding).
    artifact_ref: str | None = None
    for ref in ledger_proposal.get("evidence_refs") or []:
        if isinstance(ref, str) and ref.startswith("artifact:reports/gpt-pro-receipts/"):
            artifact_ref = ref
            break
    if artifact_ref is None:
        return (
            f"no gpt-pro-receipts artifact ref in ledger entry for {proposal_id!r}; "
            "cannot re-validate"
        )

    artifact_path = root / artifact_ref.removeprefix("artifact:")
    if not artifact_path.exists():
        return f"receipt artifact {artifact_path} does not exist; cannot re-validate"

    try:
        raw = artifact_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"failed to read receipt artifact: {exc}"

    # 3. Blocker-semantic field check (Hole 1 fix): fail-closed on severity=blocker or
    #    non-null blocker_code in artifact body, regardless of top-level verdict token.
    if _BLOCKER_SIGNAL_RE.search(raw):
        return (
            "receipt artifact contains blocker-semantic field "
            "(non-null blocker_code or severity=blocker); "
            "refusing major closure auto-spawn"
        )

    # 4. Independent verdict token scan: confirm pass-family, reject if blocker token present.
    verdicts_found = [m.group(1).lower() for m in _RECEIPT_VERDICT_RE.finditer(raw)]
    if not verdicts_found:
        return "no verdict token found in receipt artifact during re-validation"
    if any(v == "blocker" for v in verdicts_found):
        return (
            f"receipt artifact contains 'blocker' verdict token(s) {verdicts_found}; "
            "refusing major closure auto-spawn"
        )
    if not any(v in _GPT_PRO_PASS_VERDICTS for v in verdicts_found):
        return (
            f"no pass-family verdict found in receipt artifact {verdicts_found}; "
            "refusing major closure auto-spawn"
        )
    return None


def _read_gpt_pro_desktop_actuator_command(root: Path) -> str | None:
    contract_path = root / "DIRECT_PROJECT_CONTRACT.toml"
    if not contract_path.exists():
        return None
    text = contract_path.read_text(encoding="utf-8")
    command: object | None
    if tomllib is not None:
        try:
            contract = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return None
        review_policy = contract.get("review_policy")
        if not isinstance(review_policy, dict):
            return None
        command = review_policy.get("gpt_pro_desktop_actuator_command")
    else:
        command = _extract_review_policy_string(text, "gpt_pro_desktop_actuator_command")
    if not isinstance(command, str) or not command.strip():
        return None
    return command


def _infer_gpt_pro_review_transport(command: str | None) -> str:
    if isinstance(command, str) and "chatgpt_browser_review" in command:
        return "chatgpt_browser_cdp"
    return "chatgpt_desktop_aqua"


def _extract_review_policy_string(text: str, key: str) -> str | None:
    section_match = re.search(r"(?ms)^\[review_policy\]\s*$(.*?)(?:^\[|\Z)", text)
    if section_match is None:
        return None
    section = section_match.group(1)
    value_match = re.search(rf'(?m)^\s*{re.escape(key)}\s*=\s*("(?:\\.|[^"\\])*")\s*$', section)
    if value_match is None:
        return None
    try:
        value = json.loads(value_match.group(1))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, str) else None


def cmd_authorize(args: argparse.Namespace) -> int:
    state_path, ledger_path = _state_paths(args.root)
    writer = StateWriter(state_path=state_path, ledger_path=ledger_path)

    preflight = preflight_reconcile(root=args.root, writer=writer, ledger_path=ledger_path)
    if preflight is not None:
        payload, exit_code = preflight
        payload.setdefault("proposal_id", args.proposal_id)
        return _emit(payload, exit_code)

    contract_error = _validate_contract(args.root)
    if contract_error is not None:
        return _emit({"result": contract_error, "proposal_id": args.proposal_id}, 3)
    try:
        state = _read_state(state_path)
    except UnsupportedStateSchemaVersion:
        return _unsupported_state_schema()
    proposal_id = args.proposal_id
    # Authorization is granted against LIVE accepted state (proposal_results) ONLY,
    # never the append-only ledger. record_authorization itself validates against
    # proposal_results, so accepting a ledger-only (recovery) proposal here would only
    # pass this CLI gate and then be rejected by the writer — keep the two consistent so
    # an un-recoverable proposal fails fast and clearly as unknown_proposal_id (exit 4).
    stored = state.get("proposal_results", {}).get(proposal_id)
    if stored is None:
        return _emit({"result": "unknown_proposal_id", "proposal_id": proposal_id}, 4)
    stored = dict(stored)
    stored.pop("replayed", None)
    decision = StateTransitionDecision(**stored)

    if decision.decision != "accepted":
        return _emit({"result": "not_accepted", "proposal_id": proposal_id}, 0)
    if decision.next_required_action not in GATED_ACTIONS:
        # tier-1 (policy-authorized) actions need no live authorization; only tier-2 is authorizable.
        return _emit(
            {
                "result": "not_authorizable",
                "proposal_id": proposal_id,
                "next_required_action": decision.next_required_action,
            },
            4,
        )

    result = writer.record_authorization(
        proposal_id=proposal_id,
        evidence_refs=args.evidence,
        scope=args.scope,
    )
    return _emit(result, 0 if result.get("decision") == "recorded" else 2)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "apply":
        root_payload = _non_canonical_root_payload(args.root)
        if root_payload is not None:
            return _emit(root_payload, 3)
        contract_error = _validate_contract(args.root)
        if contract_error is not None:
            return _emit({"result": contract_error}, 3)
        state_path, ledger_path = _state_paths(args.root)
        payload = json.loads(args.proposal.read_text(encoding="utf-8"))
        writer = StateWriter(state_path=state_path, ledger_path=ledger_path)
        decision = writer.apply(StateTransitionProposal(**payload))
        output = dict(decision.__dict__)
        sys.stdout.write(json.dumps(output, ensure_ascii=False, sort_keys=True) + "\n")
        if decision.decision == "rejected":
            return 2
        return 0

    if args.command == "continue":
        return cmd_continue(args)

    if args.command == "list-ready":
        return cmd_list_ready(args)

    if args.command == "list-gated":
        return cmd_list_gated(args)

    if args.command == "dispatch":
        return cmd_dispatch(args)

    if args.command == "reconcile-once":
        return cmd_reconcile_once(args)

    if args.command == "review-job":
        return cmd_review_job(args)

    if args.command == "gpt-pro-actuate":
        return cmd_gpt_pro_actuate(args)

    if args.command == "authorize":
        return cmd_authorize(args)

    if args.command == "preflight":
        status_output = args.status_file.read_text(encoding="utf-8")
        result = check_governance_dirty(status_output, set(args.recognized))
        sys.stdout.write(json.dumps(result, default=lambda value: value.__dict__, ensure_ascii=False) + "\n")
        return 0 if result.ok else 2

    if args.command == "repair-todo":
        current = args.todo_file.read_text(encoding="utf-8")
        repaired = repair_compact_current_state(
            current,
            current_phase=args.current_phase,
            next_locked_action=args.next_locked_action,
            review_gate_state=args.review_gate_state,
            latest_session_log_anchor=args.latest_session_log_anchor,
        )
        args.todo_file.write_text(repaired, encoding="utf-8")
        sys.stdout.write(json.dumps({"ok": True, "todo_file": str(args.todo_file)}, ensure_ascii=False) + "\n")
        return 0

    if args.command == "watchdog":
        contract_error = _validate_contract(args.root)
        if contract_error is not None:
            return _emit({"result": contract_error}, 3)
        observation = load_observation(args.observation)
        decision = evaluate_watchdog(root=args.root, observation=observation)
        payload = decision.to_dict()
        if decision.reason == "unsupported_state_schema_version":
            sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            return 3

        if args.write_proposal is not None:
            payload["proposal_written"] = write_proposal(args.write_proposal, decision)
            payload["proposal_path"] = str(args.write_proposal)

        if args.apply_timeout_blocker and decision.proposal is not None:
            state_path, ledger_path = _state_paths(args.root)
            writer = StateWriter(state_path=state_path, ledger_path=ledger_path)
            preflight = preflight_reconcile(root=args.root, writer=writer, ledger_path=ledger_path)
            if preflight is not None:
                preflight_payload, preflight_exit = preflight
                payload["state_write"] = False
                payload["state_writer_preflight"] = preflight_payload
                sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
                return preflight_exit
            apply_decision = writer.apply(StateTransitionProposal(**decision.proposal))
            payload["state_write"] = apply_decision.decision == "accepted"
            payload["state_writer_decision"] = apply_decision.__dict__
            sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            if apply_decision.decision == "rejected":
                return 2
            return 0

        sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


def _emit(payload: dict[str, object], exit_code: int) -> int:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return exit_code


def _unsupported_state_schema() -> int:
    return _emit({"result": "unsupported_state_schema_version"}, 3)


def _read_state(state_path: Path) -> dict[str, object]:
    if not state_path.exists():
        return empty_state()
    return normalize_state(json.loads(state_path.read_text(encoding="utf-8")))


def _scan_ledger_for_decision(ledger_path: Path, proposal_id: str) -> dict[str, object] | None:
    if not ledger_path.exists():
        return None
    found: dict[str, object] | None = None
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        decision = entry.get("decision")
        if isinstance(decision, dict) and decision.get("proposal_id") == proposal_id:
            found = decision  # keep scanning; we want the LAST matching entry
    return found


def _scan_ledger_for_proposal(ledger_path: Path, proposal_id: str) -> dict[str, object] | None:
    if not ledger_path.exists():
        return None
    found: dict[str, object] | None = None
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        proposal = entry.get("proposal")
        if isinstance(proposal, dict) and proposal.get("proposal_id") == proposal_id:
            found = proposal
    return found


if __name__ == "__main__":
    raise SystemExit(main())
