from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9 compatibility for the project test runner.
    tomllib = None

from .compat import (
    CALLER_TYPE_ENV,
    UnsupportedStateSchemaVersion,
    canonical_master_plan_file,
    empty_state,
    normalize_state,
    read_contract_active_root,
    read_contract_project_id,
    read_contract_section,
    validate_contract_compat,
)
from .continuation import (
    AUTO_SPAWN_ACTIONS,
    ENVIRONMENT_ESCALATION_ACTIONS,
    GATED_ACTIONS,
    NON_EXECUTABLE_ACTIONS,
    continue_after_apply,
    evaluate_continuation,
)
from .preflight import check_governance_dirty, find_governance_blockers
from . import session_reaper
from .todo import repair_compact_current_state
from .watchdog import (
    ReviewWatchdogObservation,
    codex_cc_stall_threshold_minutes,
    evaluate_watchdog,
    load_observation,
    write_proposal,
)
from .writer import (
    DECISION_STORED_HINT_KEYS,
    ENVIRONMENTAL_BLOCKER_CODES,
    FINAL_CONVERGENCE_OUTCOME,
    ESCALATED_REVIEW_ACTUATOR_FAILURE_BLOCKER_CODE,
    SPAWNED_DISPATCH_STALL_SECONDS,
    StateTransitionDecision,
    StateTransitionProposal,
    StateWriter,
    WATCHDOG_CALLER_TYPE,
    _REVIEW_VERDICT_ALIASES,
)


def _state_paths(root: Path) -> tuple[Path, Path]:
    base = root / ".omx" / "state" / "ao-state-writer"
    return base / "state.json", base / "state-transitions.jsonl"


PRECHECK_FORBIDDEN_ACTIONS = ["dispatch", "authorize", "spawn", "canonical_write", "allowlist_add"]


# --- Spawned-review stall detection (AO-002 lock-step with watchdog.codex_cc_stall_threshold_minutes) ---
# Public console_script entrypoint (pyproject [project.scripts]); the brand-neutral default for the
# advisory watchdog/reconcile commands emitted in stall/liveness payloads. A source-checkout deployment
# overrides the worker obligation command via contract [state_writer] command; these advisory strings use
# the pip-installed default rather than a machine-coupled interpreter path.
STATE_WRITER_CLI_COMMAND = "ao-state-writer"
# A first codex_cc/escalated review timeout fires at this elapsed point. codex_cc then stages wider via
# codex_cc_stall_threshold_minutes (15/30/45 for prior 0/1/2+); escalated stays at its fixed hard timeout.
SPAWNED_REVIEW_STALL_SECONDS = 15 * 60
SPAWNED_ESCALATED_REVIEW_STALL_SECONDS = 120 * 60
# A dispatch timestamp further in the future than this skew is unusable (clock skew / corrupt record).
DISPATCH_TIME_FUTURE_SKEW_SECONDS = 60


def _validate_contract(root: Path) -> str | None:
    return validate_contract_compat(
        root,
        auto_spawn_actions=AUTO_SPAWN_ACTIONS,
        gated_actions=GATED_ACTIONS,
        non_executable_actions=NON_EXECUTABLE_ACTIONS,
        environment_escalation_actions=ENVIRONMENT_ESCALATION_ACTIONS,
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


# In-flight dispatch-lease statuses for the operator_paused diagnostic: the short 'pending' claim
# window PLUS the 'spawned*' family (a worker/actuator is actually running). EXCLUDES
# 'final_convergence_recorded' (a parked terminal/owner-visible convergence record, not an active
# worker lease) and any released lease (those are deleted from dispatched_proposals, not retained).
IN_FLIGHT_DISPATCH_STATUSES = frozenset(
    {
        "pending",
        "spawned",
        "spawned_unattested",
        "spawned_base_mismatch",
        "spawned_base_unverified",
    }
)


def _count_active_dispatch_leases(state: dict[str, object]) -> int:
    """Diagnostic for the operator_paused envelope: how many dispatch leases are still IN-FLIGHT.

    Pause does NOT abort an already-claimed/spawned lease (writer-owned), so the paused envelope
    REPORTS the in-flight count rather than implying immediate quiescence. In-flight =
    IN_FLIGHT_DISPATCH_STATUSES (the 'pending' claim window plus the 'spawned*' family). Counting
    only 'pending' would under-report: a confirmed worker/actuator that is actually running sits in
    'spawned'/'spawned_unattested', so a 'pending'-only count reports 0 (false quiescence) exactly
    when a lease is live.
    """
    dispatched = state.get("dispatched_proposals", {})
    if not isinstance(dispatched, dict):
        return 0
    return sum(
        1
        for record in dispatched.values()
        if isinstance(record, dict) and record.get("status") in IN_FLIGHT_DISPATCH_STATUSES
    )


def _operator_pause_issue(
    state: dict[str, object],
) -> tuple[dict[str, object], int] | None:
    """First-class operator-pause gate (P5). Fail-closed.

    Returns a typed (payload, exit_code) envelope when the project is paused (or the
    ``operator_pause`` record is malformed), else ``None``. Checked at the TOP of
    ``preflight_reconcile`` (upstream of obligation/env/lease projection, so it cannot regress those)
    AND at the head of the side-effectful canonical-write commands that bypass shared preflight
    (record-final-convergence, reconcile-leases, reconcile-spawn-attestation,
    reconcile-spawned-dispatch). Distinct result code from the final-convergence parked terminal /
    review_environment_unavailable: operator-invokable, not content-exhaustion-gated.
    """
    pause = state.get("operator_pause")
    if pause is None:
        return None
    if not isinstance(pause, dict) or not isinstance(pause.get("paused"), bool):
        return (
            {
                "result": "operator_pause_malformed",
                "current_obligation": True,
                "forbidden_actions": PRECHECK_FORBIDDEN_ACTIONS,
                "allowed_repair_actions": ["operator_resume"],
            },
            3,
        )
    if pause.get("paused") is True:
        return (
            {
                "result": "operator_paused",
                "current_obligation": True,
                "paused_reason": pause.get("reason", ""),
                "set_by": pause.get("set_by", ""),
                "set_at_revision": pause.get("set_at_revision"),
                "set_at": pause.get("set_at"),
                "active_dispatch_leases": _count_active_dispatch_leases(state),
                "forbidden_actions": PRECHECK_FORBIDDEN_ACTIONS,
                "allowed_repair_actions": ["operator_resume"],
            },
            3,
        )
    return None


def preflight_reconcile(
    *,
    root: Path,
    writer: StateWriter,
    ledger_path: Path,
    dry_run: bool = True,
    resolving_review_timeout: dict[str, object] | None = None,
) -> tuple[dict[str, object], int] | None:
    """Shared read-only guard for orchestration entrypoints.

    It does not authorize, claim, release, spawn, dispatch, enqueue, or persist a
    reconcile result.  It only converts unsafe current obligations into typed
    fail-closed envelopes before a CLI entrypoint can silently return an empty list.

    ``resolving_review_timeout`` is the ONE exception channel for the watchdog
    ``--apply-timeout-blocker`` path: it fingerprints the exact spawned_review_timeout_due
    obligation that invocation is clearing, so preflight does not self-block on the very
    stall the resolver exists to resolve (the 070c412 circular block) while every other
    obligation and guard still fails closed.
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

    pause_issue = _operator_pause_issue(state)
    if pause_issue is not None:
        return pause_issue

    # Governance gate: an uncommitted change to a governance file (or an unsafe canonical filename)
    # makes the working tree non-authoritative, so EVERY shared-preflight entrypoint (continue,
    # dispatch, list-ready, list-gated, reconcile) must fail closed here, not just the continuation
    # path. No-ops outside a git repo.
    governance_blockers = find_governance_blockers(root)
    if governance_blockers:
        return {
            "result": "blocked",
            "reasons": governance_blockers,
            "forbidden_actions": PRECHECK_FORBIDDEN_ACTIONS,
            "allowed_repair_actions": ["orchestrator_governance_dirty_review"],
        }, 3

    issue = _current_obligation_issue(
        root=root,
        state=state,
        writer=writer,
        ledger_path=ledger_path,
        resolving_review_timeout=resolving_review_timeout,
    )
    if issue is not None:
        return issue, 3

    return None


def _review_environment_unavailable_payload(
    proposal_id: str,
    action: str,
    *,
    environment_fault_counts: dict[str, object] | None = None,
) -> dict[str, object]:
    """Owner-visible escalation payload for a persistently-broken review environment.

    Not a spawn candidate, not a gated authorization, not convergence: the review ENVIRONMENT
    (actuator or review runner) failed past MAX_ENVIRONMENT_ATTEMPTS without ever
    producing a content verdict. The orchestrator wrapper surfaces this to the owner (out-of-band /
    `ao report needs_input`); the target resumes automatically once a successful review receipt
    supersedes this obligation by revision. environment_fault_counts is per-code diagnostics.
    """
    payload: dict[str, object] = {
        "result": "review_environment_unavailable",
        "proposal_id": proposal_id,
        "next_required_action": action,
        "blocked_action": action,
        "current_obligation": True,
        "forbidden_actions": PRECHECK_FORBIDDEN_ACTIONS,
        "allowed_repair_actions": ["review_environment_repair"],
    }
    if environment_fault_counts is not None:
        payload["environment_fault_counts"] = environment_fault_counts
    return payload


def _spawn_baseline_issue_payload(proposal_id: str, record: dict[str, object]) -> dict[str, object]:
    """Surface a recorded spawn-baseline lease (status spawned_base_mismatch / spawned_base_unverified,
    written by writer.record_spawn_baseline_issue) as an owner-visible current obligation: the worker
    was spawned on a worktree that could not prove the required source baseline, so a fresh spawn is
    forbidden and the baseline must be repaired. The auto-RELEASE of this lease is wired in a later
    slice (reconcile_spawned_dispatch only accepts a normal "spawned" lease today); until then this is
    consumed + surfaced + intentionally unreleased."""
    result = str(record.get("baseline_attestation_result") or record.get("result") or "spawn_baseline_unverified")
    if result != "spawn_base_commit_mismatch":
        result = "spawn_baseline_unverified"
    payload: dict[str, object] = {
        "result": result,
        "proposal_id": proposal_id,
        "spawn_session_id": record.get("spawn_session_id"),
        "forbidden_actions": ["spawn"],
        "allowed_repair_actions": ["orchestrator_spawn_baseline_repair"],
    }
    termination = record.get("spawn_session_termination")
    if result == "spawn_base_commit_mismatch" and isinstance(termination, dict):
        if termination.get("result") == "failed":
            payload["result"] = "spawn_base_commit_mismatch_kill_failed"
            payload["allowed_repair_actions"] = [
                "orchestrator_spawn_session_termination_retry",
                "orchestrator_spawn_baseline_repair",
            ]
    payload["reason"] = str(record.get("reason") or record.get("baseline_attestation_result") or result)
    for key in ("required_source_commit", "worker_worktree", "worker_head", "reason", "session_path"):
        value = record.get(key)
        if value:
            payload[key] = value
    if isinstance(termination, dict):
        payload["spawn_session_termination"] = termination
    return payload


# --- Spawn-baseline DETECTION (Group E slice b3b; WIRED into the spawn flow by b3c). Decides whether a
# freshly spawned worker is on the required source baseline. The whole feature is GATED on the contract
# opt-in token state_writer.preflight.spawned_worker_requires_current_source_baseline: _dispatch_one only
# runs the post-spawn check (and _source_baseline_commit_for_spawn only reports source_not_git) when a
# project opts in. LIVE's own contract sets the token, so this is behaviorally identical to LIVE's
# always-on call for a token-setting source project while leaving generic adopters unencumbered. The B1
# merge-base relaxation keeps the check from false-positiving on normal git histories. ---
GIT_COMMAND_TIMEOUT_SECONDS = 10
SPAWN_SESSION_METADATA_RETRIES = 20
SPAWN_SESSION_METADATA_RETRY_DELAY_SECONDS = 0.25
SPAWN_SESSION_TERMINATE_TIMEOUT_SECONDS = 30
# AO session statuses that mean the worker is gone (used by _kill_readback to decide absent vs active).
TERMINAL_AO_SESSION_STATUSES = {
    "closed",
    "complete",
    "completed",
    "dead",
    "done",
    "killed",
    "stopped",
    "terminated",
}
# Dispatch statuses worth surfacing in the read-only historical-records audit side-channel (NOT a
# behavior-driving set: kept separate from CONSUMED_DISPATCH_STATUSES, which gates re-dispatch). A
# superseded (non-current) lease in one of these states gets its advisory reconcile commands surfaced so
# an unattended orchestrator can discover how to clean it up.
HISTORICAL_DISPATCH_AUDIT_STATUSES = frozenset(
    {"spawned", "spawned_unattested", "spawned_base_mismatch", "spawned_base_unverified"}
)


def _git_stdout(root: Path, *args: str) -> str | None:
    try:
        process = subprocess.Popen(
            ["git", "-C", str(root), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, _ = process.communicate(timeout=GIT_COMMAND_TIMEOUT_SECONDS)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.communicate()
        except UnboundLocalError:
            pass
        return None
    if process.returncode != 0:
        return None
    return stdout.strip() or None


def _git_worktree_status(root: Path) -> tuple[bool | None, str | None]:
    try:
        process = subprocess.Popen(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = process.communicate(timeout=GIT_COMMAND_TIMEOUT_SECONDS)
    except FileNotFoundError:
        return None, "git_unavailable"
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.communicate()
        except UnboundLocalError:
            pass
        return None, "git_probe_timeout"
    if process.returncode == 0:
        return stdout.strip() == "true", None
    if "not a git repository" in stderr.lower():
        return False, None
    return None, "git_probe_failed"


def _git_commit_exists(root: Path, commit: str) -> bool | None:
    try:
        process = subprocess.Popen(
            ["git", "-C", str(root), "cat-file", "-e", f"{commit}^{{commit}}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        process.communicate(timeout=GIT_COMMAND_TIMEOUT_SECONDS)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.communicate()
        except UnboundLocalError:
            pass
        return None
    return process.returncode == 0


def _ao_projects_root() -> Path:
    override = os.environ.get("AO_PROJECTS_ROOT")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".agent-orchestrator" / "projects"


def _session_worktree_from_payload(payload: object) -> Path | None:
    if not isinstance(payload, dict):
        return None
    worktree = payload.get("worktree")
    if isinstance(worktree, str) and worktree.strip():
        return Path(worktree).expanduser()
    workspace_path = payload.get("workspacePath")
    if isinstance(workspace_path, str) and workspace_path.strip():
        return Path(workspace_path).expanduser()
    if isinstance(worktree, dict):
        worktree_path = worktree.get("path")
        if isinstance(worktree_path, str) and worktree_path.strip():
            return Path(worktree_path).expanduser()
    lifecycle = payload.get("lifecycle")
    if isinstance(lifecycle, dict):
        runtime = lifecycle.get("runtime")
        if isinstance(runtime, dict):
            handle = runtime.get("handle")
            if isinstance(handle, dict):
                data = handle.get("data")
                if isinstance(data, dict):
                    workspace_path = data.get("workspacePath")
                    if isinstance(workspace_path, str) and workspace_path.strip():
                        return Path(workspace_path).expanduser()
    return None


def _read_spawn_session_payload(session_path: Path) -> tuple[object | None, str | None]:
    reason: str | None = None
    for attempt in range(SPAWN_SESSION_METADATA_RETRIES):
        try:
            payload = json.loads(session_path.read_text(encoding="utf-8"))
            if _session_worktree_from_payload(payload) is not None:
                return payload, None
            reason = "missing_spawn_worktree"
        except FileNotFoundError:
            reason = "missing_spawn_session_metadata"
        except json.JSONDecodeError:
            reason = "invalid_spawn_session_metadata"
        if attempt < SPAWN_SESSION_METADATA_RETRIES - 1:
            time.sleep(SPAWN_SESSION_METADATA_RETRY_DELAY_SECONDS)
    return None, reason


def _contract_requires_spawn_baseline(root: Path) -> bool:
    section = read_contract_section(root, "state_writer.preflight")
    return bool(section and "spawned_worker_requires_current_source_baseline" in section)


def _source_baseline_commit_for_spawn(root: Path, spawn_session_id: str = "") -> tuple[str | None, dict[str, object] | None]:
    source_root = read_contract_active_root(root) or root
    is_worktree, worktree_reason = _git_worktree_status(source_root)
    if is_worktree is False:
        if _contract_requires_spawn_baseline(root):
            return None, {
                "result": "spawn_baseline_unverified",
                "reason": "source_not_git",
                "spawn_session_id": spawn_session_id,
            }
        return None, None
    if is_worktree is None:
        return None, {
            "result": "spawn_baseline_unverified",
            "reason": worktree_reason or "git_probe_failed",
            "spawn_session_id": spawn_session_id,
        }
    required_source_commit = _git_stdout(source_root, "rev-parse", "HEAD")
    if required_source_commit is None:
        return None, {
            "result": "spawn_baseline_unverified",
            "reason": "source_head_unreadable",
            "spawn_session_id": spawn_session_id,
        }
    return required_source_commit, None


def _spawn_baseline_issue(
    *,
    root: Path,
    ao_project_id: str | None,
    spawn_session_id: str,
    required_source_commit: str | None = None,
) -> dict[str, object] | None:
    if required_source_commit is None:
        required_source_commit, source_issue = _source_baseline_commit_for_spawn(root, spawn_session_id)
        if source_issue is not None:
            return source_issue
        if required_source_commit is None:
            return None
    project_id = ao_project_id or read_contract_project_id(root)
    if not project_id:
        return {
            "result": "spawn_baseline_unverified",
            "reason": "missing_ao_project_id",
            "spawn_session_id": spawn_session_id,
            "required_source_commit": required_source_commit,
        }
    session_path = _ao_projects_root() / project_id / "sessions" / f"{spawn_session_id}.json"
    session_payload, session_reason = _read_spawn_session_payload(session_path)
    if session_reason is not None:
        return {
            "result": "spawn_baseline_unverified",
            "reason": session_reason,
            "spawn_session_id": spawn_session_id,
            "required_source_commit": required_source_commit,
            "session_path": str(session_path),
        }
    worktree = _session_worktree_from_payload(session_payload)
    if worktree is None:
        return {
            "result": "spawn_baseline_unverified",
            "reason": "missing_spawn_worktree",
            "spawn_session_id": spawn_session_id,
            "required_source_commit": required_source_commit,
            "session_path": str(session_path),
        }
    worker_head = _git_stdout(worktree, "rev-parse", "HEAD")
    if worker_head is None:
        return {
            "result": "spawn_baseline_unverified",
            "reason": "spawn_worktree_not_git",
            "spawn_session_id": spawn_session_id,
            "required_source_commit": required_source_commit,
            "worker_worktree": str(worktree),
        }
    try:
        merge_base_proc = subprocess.Popen(
            ["git", "-C", str(worktree), "merge-base", required_source_commit, "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        merge_base_stdout, _ = merge_base_proc.communicate(timeout=GIT_COMMAND_TIMEOUT_SECONDS)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        try:
            merge_base_proc.kill()
            merge_base_proc.communicate()
        except UnboundLocalError:
            pass
        return {
            "result": "spawn_baseline_unverified",
            "reason": "spawn_baseline_check_failed",
            "spawn_session_id": spawn_session_id,
            "required_source_commit": required_source_commit,
            "worker_worktree": str(worktree),
            "worker_head": worker_head,
        }
    # B1 (owner directive 2026-06-05): the spawn-baseline attestation no longer requires the worker
    # worktree to CONTAIN active-root HEAD. AO-native worktree transport always bases a new worker on
    # origin/<defaultBranch>, so a worker is structurally never a descendant of the (sibling, far-ahead)
    # active-root branch -- `merge-base --is-ancestor` returned 1 on 100% of spawns (false positive).
    # Instead require the worker HEAD and active-root HEAD to SHARE AN EXISTING COMMON ANCESTOR: a
    # resolvable merge-base proves both commits exist in the worktree object store and the histories are
    # related. A genuinely foreign worktree (separate object store -> missing source object) or an
    # unrelated history (no common ancestor) still fails closed as spawn_base_commit_mismatch, preserving
    # the contract protection against unrelated/missing-object baselines.
    if merge_base_proc.returncode == 0 and merge_base_stdout.strip():
        return None
    if merge_base_proc.returncode == 1:
        # Histories share no common ancestor (unrelated worktree) -> blocking mismatch.
        reason = "spawn_base_commit_mismatch"
    elif _git_commit_exists(worktree, required_source_commit) is False:
        # Required source commit object is absent from the worktree (foreign/unshared object store).
        reason = "spawn_base_commit_mismatch"
    else:
        reason = "spawn_baseline_check_failed"
    return {
        "result": reason if reason == "spawn_base_commit_mismatch" else "spawn_baseline_unverified",
        "reason": reason,
        "spawn_session_id": spawn_session_id,
        "required_source_commit": required_source_commit,
        "worker_worktree": str(worktree),
        "worker_head": worker_head,
    }


# --- Spawn-session liveness readback (Group E slice b3c). Produces the live `terminal_readback` the
# reconcile CLI feeds to writer.reconcile_spawned_dispatch so a bad-baseline lease is released ONLY once
# the offending worker is proven gone. NB: LIVE also defines _terminate_spawned_session / _same_path_string
# (an `ao session kill` helper) but NOTHING in the LIVE tree calls them — they are dead there — so they are
# intentionally NOT ported until an actual caller exists; the release path proves absence via readback, it
# does not itself kill. ---
def _kill_readback(spawn_session_id: str, ao_project_id: str | None) -> dict[str, object]:
    if not ao_project_id:
        return {"result": "skipped", "reason": "missing_ao_project_id"}
    command = ["ao", "session", "ls", "-p", ao_project_id, "--json"]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=SPAWN_SESSION_TERMINATE_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        return {"result": "failed", "reason": "ao_command_not_found", "stderr": str(exc), "command": command}
    except subprocess.TimeoutExpired:
        return {"result": "failed", "reason": "ao_session_ls_timeout", "command": command}
    if completed.returncode != 0:
        return {
            "result": "failed",
            "reason": "ao_session_ls_failed",
            "command": command,
            "returncode": completed.returncode,
            "stdout": (completed.stdout or "").strip(),
            "stderr": (completed.stderr or "").strip(),
        }
    try:
        payload = json.loads(completed.stdout or "[]")
    except json.JSONDecodeError:
        return {
            "result": "failed",
            "reason": "invalid_ao_session_ls_json",
            "command": command,
            "stdout": (completed.stdout or "").strip(),
        }
    if isinstance(payload, list):
        sessions = payload
    elif isinstance(payload, dict):
        sessions = payload.get("sessions")
        if sessions is None:
            sessions = payload.get("data", [])
    else:
        sessions = []
    if not isinstance(sessions, list):
        return {"result": "failed", "reason": "invalid_ao_session_ls_shape", "command": command}
    for session in sessions:
        if isinstance(session, dict) and session.get("id") == spawn_session_id:
            status = session.get("status")
            normalized_status = str(status or "").strip().lower()
            if normalized_status in TERMINAL_AO_SESSION_STATUSES:
                return {
                    "result": "absent",
                    "id": spawn_session_id,
                    "status": status,
                    "command": command,
                }
            result: dict[str, object] = {
                "result": "active",
                "id": spawn_session_id,
                "status": status,
                "command": command,
            }
            worktree = _session_worktree_from_payload(session)
            if worktree is not None:
                result["workspacePath"] = str(worktree)
            return result
    return {"result": "absent", "command": command}


def _confirmed_absent_readback(spawn_session_id: str, ao_project_id: str | None) -> dict[str, object]:
    first = _kill_readback(spawn_session_id, ao_project_id)
    if first.get("result") != "absent":
        return first
    second = _kill_readback(spawn_session_id, ao_project_id)
    if second.get("result") != "absent":
        return second
    return {
        "result": "absent",
        "confirmations": [first, second],
    }


def _orchestrator_cli_proof_issue(
    *,
    writer: StateWriter,
    proposal_id: str | None = None,
) -> dict[str, object] | None:
    """Fail-closed gate for side-effectful CLI entrypoints (escalated-review actuation, watchdog
    timeout-blocker apply): the caller MUST present a live orchestrator identity, else the path must
    not run. Complements the writer-level ``non_orchestrator_caller`` guard (which protects specific
    state-write methods); this is the cli-preflight layer for the actuate/watchdog paths that are not
    themselves those writer methods.
    """
    caller_type = os.environ.get(CALLER_TYPE_ENV)
    if caller_type != "orchestrator":
        payload: dict[str, object] = {
            "result": "missing_or_stale_orchestrator_proof",
            "caller_type": caller_type,
            "forbidden_actions": ["actuate", "canonical_write"],
            "allowed_repair_actions": ["orchestrator_session_rebind"],
        }
        if proposal_id is not None:
            payload["proposal_id"] = proposal_id
        return payload
    proof = writer._orchestrator_proof_metadata()
    if proof.get("reason"):
        payload = {
            "result": str(proof["reason"]),
            "caller_type": caller_type,
            "forbidden_actions": ["actuate", "canonical_write"],
            "allowed_repair_actions": ["orchestrator_session_rebind"],
        }
        if proposal_id is not None:
            payload["proposal_id"] = proposal_id
        return payload
    return None


def _current_obligation_issue(
    *,
    root: Path,
    state: dict[str, object],
    writer: StateWriter,
    ledger_path: Path,
    resolving_review_timeout: dict[str, object] | None = None,
) -> dict[str, object] | None:
    proposal_results = state.get("proposal_results", {})
    if not isinstance(proposal_results, dict):
        return None
    latest_by_target = _latest_accepted_revision_by_target(state, ledger_path)
    live_escalated_review_gates = _live_escalated_review_gate_ids(state, ledger_path)
    target_by_proposal = _proposal_targets_from_ledger(ledger_path)
    supported = (
        set(AUTO_SPAWN_ACTIONS)
        | set(GATED_ACTIONS)
        | set(NON_EXECUTABLE_ACTIONS)
        | set(ENVIRONMENT_ESCALATION_ACTIONS)
    )
    for proposal_id, stored in sorted(proposal_results.items()):
        if not isinstance(proposal_id, str) or not isinstance(stored, dict):
            continue
        if stored.get("decision") != "accepted":
            continue
        # dispatch_record is read up front (the early is_dispatched skip moved BELOW the stall checks)
        # so a stalled/hung spawned review is still surfaced as a resolvable obligation rather than
        # silently skipped forever.
        dispatch_record = writer.dispatch_record(proposal_id)
        if not _is_current_actionable_obligation(
            state,
            proposal_id,
            stored,
            ledger_path,
            latest_by_target,
        ):
            continue
        # Spawn-baseline lease surface (mirrors LIVE): a worker spawned on a worktree that cannot
        # prove the required source baseline is consumed (status spawned_base_mismatch /
        # spawned_base_unverified) and must surface as an owner-visible obligation HERE — before the
        # action/env/stall/liveness routing below and before the is_dispatched skip. It precedes the
        # env-escalation branch deliberately: that branch keys on `action`, so a baseline lease whose
        # stored action happens to be an environment-escalation action would otherwise be MASKED by a
        # review_environment_unavailable payload and the baseline obligation would never surface.
        if isinstance(dispatch_record, dict) and dispatch_record.get("status") in {
            "spawned_base_mismatch",
            "spawned_base_unverified",
        }:
            payload = _spawn_baseline_issue_payload(proposal_id, dispatch_record)
            payload["current_obligation"] = True
            return payload
        # Read-time projection: an env-inflated false exhaustion classifies as repair_active here, so
        # the obligation is surfaced as a live repair (not a convergence dead-end). Genuine content
        # exhaustion projects to itself and still routes to owner_proxy_convergence_required below.
        action = _effective_action(state, proposal_id, stored, target_by_proposal)
        if action in ENVIRONMENT_ESCALATION_ACTIONS:
            # Persistently-broken review environment: surface a first-class owner-visible escalation
            # (NOT unsupported_current_obligation, NOT convergence, NOT a spawn candidate) before the
            # gate/convergence logic, so every shared-preflight entrypoint reports it consistently.
            # Terminal until a later successful review receipt supersedes this obligation by revision.
            target_id = target_by_proposal.get(proposal_id)
            targets = state.get("targets", {})
            target = targets.get(target_id, {}) if isinstance(targets, dict) and target_id else {}
            attempts = target.get("repair_attempts", {}) if isinstance(target, dict) else {}
            env_counts = (
                {c: n for c, n in attempts.items() if c in ENVIRONMENTAL_BLOCKER_CODES}
                if isinstance(attempts, dict)
                else {}
            )
            return _review_environment_unavailable_payload(
                proposal_id, action, environment_fault_counts=env_counts
            )
        # Spawned-review stall detection (Group E slice a, AO-002 lock-step with the watchdog's staged
        # codex_cc_stall_threshold_minutes): surface a stalled spawned review BEFORE the is_dispatched
        # skip below, so a hung codex_cc/escalated review becomes a resolvable spawned_review_timeout_due
        # obligation instead of being skipped forever.
        stall_issue = _spawned_review_stall_issue(
            root=root,
            proposal_id=proposal_id,
            stored=stored,
            dispatch_record=dispatch_record,
            target_by_proposal=target_by_proposal,
            state=state,
        )
        if stall_issue is not None and not _review_timeout_is_self_resolving(
            stall_issue, state, resolving_review_timeout
        ):
            return stall_issue
        # If self-resolving, this is the exact spawned_review_timeout_due the watchdog
        # --apply-timeout-blocker path is clearing; do NOT gate the resolver on the stall it exists to
        # clear (the 070c412 circular self-block). Fall through: the late is_dispatched skip below still
        # skips it, while the separate liveness check still runs (a distinct liveness issue is not masked).
        liveness_issue = _spawned_dispatch_liveness_issue(
            root=root,
            state=state,
            proposal_id=proposal_id,
            stored=stored,
            dispatch_record=dispatch_record,
            target_by_proposal=target_by_proposal,
        )
        if liveness_issue is not None:
            return liveness_issue
        if writer.is_dispatched(proposal_id):
            continue
        if live_escalated_review_gates and action != "escalated_review":
            continue
        if action in NON_EXECUTABLE_ACTIONS:
            return {
                "result": "owner_proxy_convergence_required",
                "proposal_id": proposal_id,
                "next_required_action": action,
                "blocked_action": action,
                "forbidden_actions": PRECHECK_FORBIDDEN_ACTIONS,
                # The immediate repair action stays a convergence REVIEW (fail-closed, unchanged). The
                # record command is advisory DISCOVERABILITY for the eventual terminal consumer, so an
                # unattended orchestrator can find the exact `record-final-convergence` argv it must run
                # once the review concludes -- it does NOT relabel the next action (task#14a-1).
                "allowed_repair_actions": ["orchestrator_convergence_review"],
                "owner_proxy_final_convergence_command": _final_convergence_record_command(
                    root, proposal_id
                ),
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


def _spawned_review_stall_issue(
    *,
    root: Path,
    proposal_id: str,
    stored: dict[str, object],
    dispatch_record: dict[str, object] | None,
    target_by_proposal: dict[str, str],
    state: dict[str, object],
) -> dict[str, object] | None:
    if not isinstance(dispatch_record, dict) or dispatch_record.get("status") != "spawned":
        return None
    action = stored.get("next_required_action")
    review_scope_by_action = {
        "codex_cc_review": "codex_cc",
        "escalated_review": "escalated_review",
    }
    review_scope = review_scope_by_action.get(action)
    if review_scope is None:
        return None
    elapsed_seconds = _dispatch_record_elapsed_seconds(dispatch_record)
    if elapsed_seconds is None:
        target_id = target_by_proposal.get(proposal_id)
        return {
            "result": "spawned_review_time_reconciliation_required",
            "proposal_id": proposal_id,
            "target_id": target_id,
            "next_required_action": action,
            "review_scope": review_scope,
            "spawn_session_id": dispatch_record.get("spawn_session_id"),
            "spawned_at": dispatch_record.get("time"),
            "forbidden_actions": ["spawn", "dispatch", "review_watchdog_apply_timeout_blocker"],
            "allowed_repair_actions": [
                "ao_spawn_time_reconcile",
                "ao_worker_liveness_reconcile",
            ],
        }
    target_id = target_by_proposal.get(proposal_id)
    # AO-002: align the codex_cc preflight stall threshold to the watchdog's staged escalation
    # (15/30/45 for prior 0/1/2+) so this chokepoint emits spawned_review_timeout_due at exactly the
    # elapsed point the watchdog first produces a resolvable blocker proposal -- no premature stall
    # window that shadows siblings while the resolver returns not_yet_due. prior_timeouts is read from
    # the SAME source the watchdog reads; escalated stays fixed at its hard-timeout threshold.
    prior_timeouts = 0
    if review_scope == "codex_cc" and target_id is not None:
        prior_timeouts = int(
            (state.get("targets", {}) or {})
            .get(target_id, {})
            .get("repair_attempts", {})
            .get("codex_cc_review_timeout", 0)
        )
    threshold_seconds = _spawned_review_stall_threshold_seconds(review_scope, prior_timeouts)
    if elapsed_seconds < threshold_seconds:
        return None
    raw_started_at = dispatch_record.get("time")
    observation: dict[str, object] = {
        "target_id": target_id,
        "review_scope": review_scope,
        "started_at": raw_started_at,
        "last_activity_at": raw_started_at,
        "last_activity_kind": "ao_spawned_review",
        "evidence_refs": [f"dispatched_proposal:{proposal_id}"],
    }
    spawn_session_id = dispatch_record.get("spawn_session_id")
    if isinstance(spawn_session_id, str) and spawn_session_id:
        observation["evidence_refs"].append(f"spawn_session:{spawn_session_id}")  # type: ignore[index]
    return {
        "result": "spawned_review_timeout_due",
        "proposal_id": proposal_id,
        "target_id": target_id,
        "next_required_action": action,
        "review_scope": review_scope,
        "spawn_session_id": spawn_session_id,
        "spawned_at": raw_started_at,
        "elapsed_seconds": elapsed_seconds,
        "threshold_seconds": threshold_seconds,
        "forbidden_actions": ["spawn", "dispatch"],
        "allowed_repair_actions": [
            "review_watchdog_apply_timeout_blocker",
            "ao_worker_liveness_reconcile",
        ],
        "review_watchdog_observation": observation,
        "review_watchdog_command": [
            STATE_WRITER_CLI_COMMAND,
            "watchdog",
            "--root",
            str(root),
            "--observation",
            "<observation.json>",
            "--apply-timeout-blocker",
        ],
    }


def _dispatched_proposal_id_from_observation(
    observation: ReviewWatchdogObservation,
) -> str | None:
    """Recover the stalled source proposal_id a watchdog observation refers to.

    ``_spawned_review_stall_issue`` stamps ``dispatched_proposal:<proposal_id>`` into the observation's
    ``evidence_refs``, so the --apply-timeout-blocker path can fingerprint the exact obligation it is
    resolving.
    """
    for ref in observation.evidence_refs or []:
        if isinstance(ref, str) and ref.startswith("dispatched_proposal:"):
            return ref.split(":", 1)[1]
    return None


def _review_timeout_is_self_resolving(
    stall_issue: dict[str, object],
    state: dict[str, object],
    resolving_review_timeout: dict[str, object] | None,
) -> bool:
    """True iff ``stall_issue`` is the exact spawned_review_timeout_due obligation a watchdog
    ``--apply-timeout-blocker`` invocation is currently resolving.

    The fingerprint must match on target_id + review_scope, on the EXACT stalled source proposal_id (a
    non-empty string the caller MUST supply — see the hardening note below), AND on the state revision the
    watchdog evaluated against (compared to live ``state``) so a stale/replayed watchdog command whose
    base revision no longer matches live state is never exempted. Only ``spawned_review_timeout_due`` is
    self-resolving; the
    ``spawned_review_time_reconciliation_required`` variant (which lists the watchdog apply in its own
    forbidden_actions) is never exempted.
    """
    if resolving_review_timeout is None:
        return False
    if stall_issue.get("result") != "spawned_review_timeout_due":
        return False
    if stall_issue.get("target_id") != resolving_review_timeout.get("target_id"):
        return False
    if stall_issue.get("review_scope") != resolving_review_timeout.get("review_scope"):
        return False
    # Public hardening (STRICTER than LIVE, fail-closed per codex confirm-review): the resolver MUST pin
    # the exact stalled proposal_id. A malformed/manual observation lacking the ``dispatched_proposal:``
    # evidence ref yields proposal_id=None; it must NOT self-exempt on target/scope/base alone, which
    # could clear a DIFFERENT proposal's stall sharing the same target/scope/revision.
    expected_pid = resolving_review_timeout.get("proposal_id")
    if not isinstance(expected_pid, str) or not expected_pid:
        return False
    if stall_issue.get("proposal_id") != expected_pid:
        return False
    expected_base = resolving_review_timeout.get("base_state_revision")
    if expected_base is not None and state.get("state_revision") != expected_base:
        return False
    return True


def _spawned_review_stall_threshold_seconds(review_scope: str, prior_timeouts: int = 0) -> int:
    if review_scope == "escalated_review":
        return SPAWNED_ESCALATED_REVIEW_STALL_SECONDS
    # codex_cc: staged to match the watchdog (AO-002). prior_timeouts=0 -> 15 min, preserving the
    # historical fixed threshold (== SPAWNED_REVIEW_STALL_SECONDS) for a first review timeout.
    return int(codex_cc_stall_threshold_minutes(prior_timeouts) * 60)


def _spawned_dispatch_liveness_issue(
    *,
    root: Path,
    state: dict[str, object],
    proposal_id: str,
    stored: dict[str, object],
    dispatch_record: dict[str, object] | None,
    target_by_proposal: dict[str, str],
) -> dict[str, object] | None:
    if not isinstance(dispatch_record, dict) or dispatch_record.get("status") != "spawned":
        return None
    # Project an env-inflated exhaustion to its effective repair_active: a spawned repair dispatch must
    # be reconciled for liveness as the repair it is, not skipped because its raw token is the
    # (non-reconcile) repair_attempts_exhausted.
    action = _effective_action(state, proposal_id, stored, target_by_proposal)
    if not _is_spawned_dispatch_reconcile_action(action):
        return None
    target_id = target_by_proposal.get(proposal_id)
    spawn_session_id = dispatch_record.get("spawn_session_id")
    raw_started_at = dispatch_record.get("time")
    evidence_refs = [f"dispatched_proposal:{proposal_id}"]
    if isinstance(spawn_session_id, str) and spawn_session_id:
        evidence_refs.append(f"spawn_session:{spawn_session_id}")
    elapsed_seconds = _dispatch_record_elapsed_seconds(dispatch_record)
    if elapsed_seconds is None:
        return {
            "result": "spawned_dispatch_time_reconciliation_required",
            "proposal_id": proposal_id,
            "target_id": target_id,
            "next_required_action": action,
            "spawn_session_id": spawn_session_id,
            "spawned_at": raw_started_at,
            "forbidden_actions": ["spawn", "dispatch"],
            "allowed_repair_actions": ["ao_spawn_time_reconcile", "ao_worker_liveness_reconcile"],
            "liveness_evidence_refs": evidence_refs,
            "spawned_dispatch_reconcile_commands": _spawned_dispatch_reconcile_commands(root, proposal_id),
        }
    if elapsed_seconds < SPAWNED_DISPATCH_STALL_SECONDS:
        return None
    return {
        "result": "spawned_dispatch_liveness_reconcile_required",
        "proposal_id": proposal_id,
        "target_id": target_id,
        "next_required_action": action,
        "spawn_session_id": spawn_session_id,
        "spawned_at": raw_started_at,
        "elapsed_seconds": elapsed_seconds,
        "threshold_seconds": SPAWNED_DISPATCH_STALL_SECONDS,
        "forbidden_actions": ["spawn", "dispatch"],
        "allowed_repair_actions": [
            "ao_worker_liveness_reconcile",
            "orchestrator_spawned_dispatch_review",
        ],
        "liveness_evidence_refs": evidence_refs,
        "spawned_dispatch_reconcile_commands": _spawned_dispatch_reconcile_commands(root, proposal_id),
    }


def _dispatch_record_elapsed_seconds(record: dict[str, object]) -> float | None:
    raw = record.get("time")
    if not isinstance(raw, str):
        return None
    dispatched_at = _parse_dispatch_timestamp(raw)
    if dispatched_at is None:
        return None
    now = datetime.now(timezone.utc)
    dispatched_at = dispatched_at.astimezone(timezone.utc)
    if (dispatched_at - now).total_seconds() > DISPATCH_TIME_FUTURE_SKEW_SECONDS:
        return None
    return max(0.0, (now - dispatched_at).total_seconds())


def _parse_dispatch_timestamp(raw: str) -> datetime | None:
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _spawned_dispatch_reconcile_commands(root: Path, proposal_id: str) -> dict[str, list[str]]:
    # Brand-neutral: the public engine ships the ``ao-state-writer`` console_script and is pip-installed,
    # so the advisory reconcile commands drop LIVE's machine-coupled ``env PYTHONPATH=<src> <interpreter>``
    # wrapper entirely.
    base = [
        STATE_WRITER_CLI_COMMAND,
        "reconcile-spawned-dispatch",
        "--root",
        str(root),
        "--proposal-id",
        proposal_id,
        "--evidence",
        "orchestrator:spawned-dispatch-reconcile",
    ]
    return {
        "release_terminal": base + ["--release-if-terminal"],
        "refresh_time": base + ["--refresh-time"],
    }


# --- Advisory command discoverability (Group E task#14a-1). The unattended orchestrator discovers HOW to
# self-heal/converge by reading the exact CLI argv from the obligation/readout payloads. Brand-neutral:
# every command emits the ``ao-state-writer`` console_script (STATE_WRITER_CLI_COMMAND) and NEVER LIVE's
# machine-coupled ``env PYTHONPATH=<.../src> <interpreter>`` wrapper. ---
def _final_convergence_record_command(root: Path, proposal_id: str) -> list[str]:
    return [
        STATE_WRITER_CLI_COMMAND,
        "record-final-convergence",
        "--root",
        str(root),
        "--proposal-id",
        proposal_id,
        "--evidence",
        "owner-proxy:final-convergence",
    ]


def _spawn_attestation_reconcile_commands(root: Path, proposal_id: str) -> dict[str, list[str]]:
    base = [
        STATE_WRITER_CLI_COMMAND,
        "reconcile-spawn-attestation",
        "--root",
        str(root),
        "--proposal-id",
        proposal_id,
        "--evidence",
        "orchestrator:spawn-attestation-reconcile",
    ]
    return {
        "record_existing_session": base + ["--spawn-session-id", "<session-id>"],
        "release_absent_session": base + ["--release-if-absent"],
    }


def _historical_dispatch_records(
    state: dict[str, object],
    writer: StateWriter,
    ledger_path: Path,
) -> list[dict[str, object]]:
    """Read-only audit side-channel: superseded (NON-current) dispatch leases still in an audit status,
    each annotated with its advisory reconcile commands. Lets an unattended orchestrator discover how to
    clean up leftover spawned/unattested/base-* leases that a newer obligation has overtaken. NEVER a
    current obligation (those route through the normal obligation checker)."""
    dispatched = state.get("dispatched_proposals", {})
    proposal_results = state.get("proposal_results", {})
    if not isinstance(dispatched, dict) or not isinstance(proposal_results, dict):
        return []
    latest_by_target = _latest_accepted_revision_by_target(state, ledger_path)
    records: list[dict[str, object]] = []
    for proposal_id, dispatch_record in dispatched.items():
        if not isinstance(proposal_id, str) or not isinstance(dispatch_record, dict):
            continue
        status = dispatch_record.get("status")
        if status not in HISTORICAL_DISPATCH_AUDIT_STATUSES:
            continue
        stored = proposal_results.get(proposal_id)
        if not isinstance(stored, dict) or stored.get("decision") != "accepted":
            continue
        if _is_current_actionable_obligation(state, proposal_id, stored, ledger_path, latest_by_target):
            continue
        record: dict[str, object] = {
            "proposal_id": proposal_id,
            "status": status,
            "current_obligation": False,
        }
        if status == "spawned_unattested":
            record["ao_session_attestation_reconcile_commands"] = _spawn_attestation_reconcile_commands(
                writer._repo_root(),
                proposal_id,
            )
        elif status in {"spawned", "spawned_base_mismatch", "spawned_base_unverified"}:
            record["spawned_dispatch_reconcile_commands"] = _spawned_dispatch_reconcile_commands(
                writer._repo_root(),
                proposal_id,
            )
        records.append(record)
    return sorted(records, key=lambda item: str(item["proposal_id"]))


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

    escalated_review_parser = subparsers.add_parser("escalated-review-actuate")
    escalated_review_parser.add_argument("--root", type=Path, required=True)
    escalated_review_parser.add_argument("--proposal-id", required=True)
    escalated_review_parser.add_argument("--bridge-command")
    escalated_review_parser.add_argument("--bridge-timeout-seconds", type=int, default=7200)

    reconcile_parser = subparsers.add_parser("reconcile-once")
    reconcile_parser.add_argument("--root", type=Path, required=True)
    reconcile_parser.add_argument("--dry-run", action="store_true")

    reconcile_leases_parser = subparsers.add_parser("reconcile-leases")
    reconcile_leases_parser.add_argument("--root", type=Path, required=True)
    reconcile_leases_parser.add_argument("--apply", action="store_true")

    retire_parser = subparsers.add_parser("retire-dead-sessions")
    retire_parser.add_argument("--root", type=Path, required=True)
    retire_parser.add_argument("--project-id")
    retire_parser.add_argument("--apply", action="store_true")
    retire_parser.add_argument(
        "--grace-seconds",
        type=int,
        default=session_reaper.DEFAULT_GRACE_SECONDS,
    )

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

    # Owner-proxy convergence / dispatch-record reconcile consumers. record-final-convergence is the
    # consumer that makes a repair_attempts_exhausted obligation resolvable (the loop reaches a
    # converged/closed terminal instead of dead-ending at owner_proxy_convergence_required). The two
    # reconcile consumers resolve auxiliary spawned-dispatch-record states.
    final_convergence_parser = subparsers.add_parser("record-final-convergence")
    final_convergence_parser.add_argument("--root", type=Path, required=True)
    final_convergence_parser.add_argument("--proposal-id", required=True)
    final_convergence_parser.add_argument("--evidence", action="append", required=True)
    final_convergence_parser.add_argument(
        "--outcome",
        choices=[FINAL_CONVERGENCE_OUTCOME],
        default=FINAL_CONVERGENCE_OUTCOME,
    )

    spawn_attestation_parser = subparsers.add_parser("reconcile-spawn-attestation")
    spawn_attestation_parser.add_argument("--root", type=Path, required=True)
    spawn_attestation_parser.add_argument("--proposal-id", required=True)
    spawn_attestation_parser.add_argument("--evidence", action="append", required=True)
    spawn_attestation_parser.add_argument("--spawn-session-id")
    spawn_attestation_parser.add_argument("--release-if-absent", action="store_true")

    spawned_dispatch_parser = subparsers.add_parser("reconcile-spawned-dispatch")
    spawned_dispatch_parser.add_argument("--root", type=Path, required=True)
    spawned_dispatch_parser.add_argument("--proposal-id", required=True)
    spawned_dispatch_parser.add_argument("--evidence", action="append", required=True)
    spawned_dispatch_parser.add_argument("--release-if-terminal", action="store_true")
    spawned_dispatch_parser.add_argument("--refresh-time", action="store_true")

    pause_parser = subparsers.add_parser("pause")
    pause_parser.add_argument("--root", type=Path, required=True)
    pause_parser.add_argument("--reason")
    pause_parser.add_argument("--set-by")

    resume_parser = subparsers.add_parser("resume")
    resume_parser.add_argument("--root", type=Path, required=True)

    dispatch_stall_parser = subparsers.add_parser("dispatch-stall-check")
    dispatch_stall_parser.add_argument("--root", type=Path, required=True)
    dispatch_stall_parser.add_argument("--last-seen-revision", type=int, default=None)
    dispatch_stall_parser.add_argument("--no-advance-count", type=int, default=0)
    dispatch_stall_parser.add_argument("--threshold-checks", type=int, default=3)

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
      - tier-2: next_required_action is escalated_review AND a fresh orchestrator
        authorization already exists (gate cleared — treat identically to AUTO_SPAWN).
    Returned sorted so callers get a deterministic order.
    """
    proposal_results = state.get("proposal_results", {})
    ledger = ledger_path or writer.ledger_path
    latest_by_target = _latest_accepted_revision_by_target(state, ledger)
    live_escalated_review_gates = _live_escalated_review_gate_ids(state, ledger)
    target_by_proposal = _proposal_targets_from_ledger(ledger)
    candidates: list[str] = []
    for pid, stored in proposal_results.items():
        if not isinstance(stored, dict) or stored.get("decision") != "accepted":
            continue
        if not _is_current_actionable_obligation(state, pid, stored, ledger, latest_by_target):
            continue
        # Project an env-inflated exhaustion to repair_active so `continue` auto-select and
        # `list-ready` classify it as the real repair it is, not the raw exhausted token. A genuine
        # content exhaustion projects to itself (repair_attempts_exhausted) and stays non-ready — it
        # converges via record-final-convergence, not a spawn (public has no convergence-spawn lane).
        action = _effective_action(state, pid, stored, target_by_proposal)
        if live_escalated_review_gates and action != "escalated_review":
            continue
        is_ready_action = (
            action in AUTO_SPAWN_ACTIONS
            or (action == "escalated_review" and writer.is_authorized(pid))
        )
        if not is_ready_action or writer.is_dispatched(pid):
            continue
        candidates.append(pid)
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
    live_escalated_review_gates = _live_escalated_review_gate_ids(state, ledger)
    candidates = [
        pid
        for pid, stored in proposal_results.items()
        if isinstance(stored, dict)
        and stored.get("decision") == "accepted"
        and _is_current_actionable_obligation(state, pid, stored, ledger, latest_by_target)
        and (
            not live_escalated_review_gates
            or stored.get("next_required_action") == "escalated_review"
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

    `proposal_results` is an audit history, not a queue.  For escalated review gates, the canonical
    live pointer is the target's active_escalated_review_gate_proposal_id.  For other actions
    we use the proposal's target from the append-only ledger when available, require the target
    to still be in the state produced by that proposal, and require the proposal to be the
    latest accepted transition for that target. `dispatch_next_slice_plan_mode` is a global
    "what next?" obligation, so it is current only while it is still the latest state revision.
    """
    next_action = stored.get("next_required_action")
    if next_action == "dispatch_next_slice_plan_mode":
        return stored.get("state_revision") == state.get("state_revision")
    if next_action == "escalated_review":
        return _is_live_escalated_review_gate(state, proposal_id, ledger_path)
    if ledger_path is None:
        return True
    # Capture ledger-file presence BEFORE the scan (avoid a TOCTOU on a post-scan re-check).
    # _scan_ledger_for_proposal collapses file-absent, proposal-absent, and corrupt-line-skipped
    # all into None, so the presence flag is what distinguishes them.
    ledger_existed = ledger_path.exists()
    proposal = _scan_ledger_for_proposal(ledger_path, proposal_id)
    if proposal is None:
        if not ledger_existed:
            # A legitimately-absent ledger FILE -> cannot disambiguate -> current (back-compat).
            return True
        # C-FIX-9: the proposal is MISSING from an EXISTING ledger. This check used to fail closed
        # here, treating this on-disk shape (accepted in state, absent from ledger) as a stale/phantom
        # obligation. But the ONLY way the writer produces a proposal_results entry with no ledger
        # line is a crash between _write_state and _append_ledger (Part A now appends the ledger
        # first, so NEW crashes land ledger-ahead-of-state and never reach here) -> a LEGITIMATE
        # accepted obligation, not a phantom. Recover it as the live obligation IFF it still exactly
        # matches the target it claims; anything ambiguous, superseded, or malformed stays fail-closed
        # so it cannot shadow healthy siblings (preserving the original phantom suppression).
        return _orphan_obligation_is_current(state, stored, latest_by_target)
    target_id = proposal.get("target_id")
    if not isinstance(target_id, str) or not target_id:
        return False  # malformed ledger proposal (no target) -> not a current obligation
    target = state.get("targets", {}).get(target_id)
    if not isinstance(target, dict):
        return False  # the proposal's target is gone from state -> not a current obligation
    if latest_by_target is not None:
        latest_revision = latest_by_target.get(target_id)
        if isinstance(latest_revision, int) and stored.get("state_revision") != latest_revision:
            return False
    new_state = stored.get("new_state")
    return not isinstance(new_state, str) or target.get("state") == new_state


def _orphan_obligation_is_current(
    state: dict[str, object],
    stored: dict[str, object],
    latest_by_target: dict[str, int] | None,
) -> bool:
    """C-FIX-9: decide whether an accepted proposal_results entry that is MISSING from an EXISTING
    ledger (a crash-split orphan: state.json was persisted but the ledger append did not run) is
    still the live obligation. Fail closed for anything that is not an exact, still-matching, latest
    accepted obligation, so a genuinely malformed/superseded record cannot shadow healthy siblings
    (this preserves the phantom suppression for the cases it was really guarding).
    """
    if stored.get("decision") != "accepted":
        return False
    new_state = stored.get("new_state")
    stored_revision = stored.get("state_revision")
    target_id = stored.get("target_id")
    if isinstance(target_id, str) and target_id:
        # EXACT path: the stored decision carries its own target (C-FIX-9 forward records). The
        # global state_revision may have advanced past this orphan (other targets kept moving), so
        # do NOT gate on the global revision here; gate on target-state-match plus no-later-revision.
        target = state.get("targets", {}).get(target_id)
        if not isinstance(target, dict):
            return False
        if isinstance(new_state, str) and target.get("state") != new_state:
            return False
        # Reject if a strictly-later accepted obligation for this SAME target exists in the ledger
        # (the orphan was superseded). latest_by_target is ledger-derived and excludes the orphan
        # itself, so a ledger revision GREATER than the orphan's means a newer obligation supersedes
        # it; a revision LESS-OR-EQUAL means the orphan legitimately supersedes an older ledger entry.
        if latest_by_target is not None and isinstance(stored_revision, int):
            latest_revision = latest_by_target.get(target_id)
            if isinstance(latest_revision, int) and latest_revision > stored_revision:
                return False
        return True
    # CONSERVATIVE legacy fallback: a record persisted before C-FIX-9 has no target hint. Recover it
    # only when it is unambiguous -- it is the global latest revision (so nothing applied after the
    # crash) AND exactly one target sits in the stored new_state. Otherwise keep the fail-closed
    # phantom suppression.
    if not isinstance(new_state, str):
        return False
    if not isinstance(stored_revision, int) or stored_revision != state.get("state_revision"):
        return False
    targets = state.get("targets", {})
    if not isinstance(targets, dict):
        return False
    matching = [t for t in targets.values() if isinstance(t, dict) and t.get("state") == new_state]
    return len(matching) == 1


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


def _effective_action(
    state: dict[str, object],
    proposal_id: str,
    stored: dict[str, object],
    target_by_proposal: dict[str, str],
) -> str | None:
    """Resolve a proposal's target and return its read-time effective next_required_action.

    Thin cli-side wrapper over StateWriter.project_effective_action: an env-inflated
    repair_attempts_exhausted (transient escalated-review actuator failures or review
    timeout/uncertain faults that never produced a content verdict inflated the per-target content
    budget) projects to repair_active; everything else (including genuine content exhaustion) is
    returned unchanged. Fail-closed when the target cannot be resolved (the raw action is kept)."""
    target_id = target_by_proposal.get(proposal_id)
    targets = state.get("targets", {})
    target = targets.get(target_id) if isinstance(targets, dict) and target_id else None
    return StateWriter.project_effective_action(stored, target)


def _projected_decision(
    state: dict[str, object],
    proposal_id: str,
    stored: dict[str, object],
    ledger_path: Path,
) -> StateTransitionDecision:
    """Reconstruct a StateTransitionDecision with the read-time effective-exhaustion projection
    applied to next_required_action, so the dispatch/continuation path sees an env-inflated
    repair_attempts_exhausted as the repair_active it effectively is (and a genuinely-exhausted
    target unchanged). ``stored`` must already have any ``replayed`` key removed. Only
    next_required_action is projected; new_state and every other field pass through verbatim."""
    target_by_proposal = _proposal_targets_from_ledger(ledger_path)
    effective = _effective_action(state, proposal_id, stored, target_by_proposal)
    projected = dict(stored)
    projected["next_required_action"] = effective
    # C-FIX-9: drop the target hint keys persisted alongside the decision (they are not
    # StateTransitionDecision fields, so the **splat would raise). See DECISION_STORED_HINT_KEYS.
    for _hint in DECISION_STORED_HINT_KEYS:
        projected.pop(_hint, None)
    return StateTransitionDecision(**projected)


def _is_live_escalated_review_gate(
    state: dict[str, object],
    proposal_id: str,
    ledger_path: Path | None = None,
) -> bool:
    return proposal_id in _live_escalated_review_gate_ids(state, ledger_path)


def _live_escalated_review_gate_ids(state: dict[str, object], ledger_path: Path | None = None) -> set[str]:
    inferred = _inferred_live_escalated_review_gate_ids(state, ledger_path)
    inferred_target_ids: set[str] = set()
    if inferred and ledger_path is not None and ledger_path.exists():
        target_by_proposal = _proposal_targets_from_ledger(ledger_path)
        inferred_target_ids = {
            target_id for pid, target_id in target_by_proposal.items() if pid in inferred and isinstance(target_id, str)
        }

    gates = state.get("escalated_review_gates", {})
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
                and target.get("state") == "escalated_review_pending"
                and target.get("active_escalated_review_gate_proposal_id") == pid
            ):
                live_ids.add(pid)
    if isinstance(targets, dict):
        for target_id, target in targets.items():
            if (
                isinstance(target_id, str)
                and isinstance(target, dict)
                and target.get("state") == "escalated_review_pending"
                and isinstance(target.get("active_escalated_review_gate_proposal_id"), str)
            ):
                if target_id in inferred_target_ids:
                    continue
                live_ids.add(target["active_escalated_review_gate_proposal_id"])
    return live_ids


def _escalated_review_package_gate_proposals_from_ledger(ledger_path: Path | None) -> set[str]:
    """Proposal ids whose append-only ledger record is a VALID escalated-review gate request:
    target_kind major_chapter AND a complete package (package_path + prompt_path + package_sha256).
    Used to harden legacy-state gate inference (see _inferred_live_escalated_review_gate_ids) so a
    mis-routed or package-less accepted escalated_review_pending can never be inferred as a live
    (and therefore unclearable) gate. The append-only ledger is the documented source of package
    metadata for legacy states, so this preserves the legitimate legacy inference while excluding
    the deadlock-inducing poison records."""
    valid: set[str] = set()
    if ledger_path is None or not ledger_path.exists():
        return valid
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
        pid = proposal.get("proposal_id")
        if not isinstance(pid, str):
            continue
        if proposal.get("target_kind") != "major_chapter":
            continue
        # Fail-closed (codex CFIX nit): require NON-EMPTY STRING package fields, mirroring the
        # authorize-side metadata validation. A manually-malformed ledger with truthy non-string
        # package fields must NOT be inferred as a live gate (authorize would later reject it).
        package_fields = (
            proposal.get("package_path"),
            proposal.get("prompt_path"),
            proposal.get("package_sha256"),
        )
        if all(isinstance(field, str) and field for field in package_fields):
            valid.add(pid)
    return valid


def _inferred_live_escalated_review_gate_ids(
    state: dict[str, object],
    ledger_path: Path | None,
) -> set[str]:
    """Infer current escalated review gates for legacy states whose active pointer predates
    the latest accepted escalated_review_pending decision.

    New writer versions persist ``active_escalated_review_gate_proposal_id`` and
    ``escalated_review_gates`` at apply time. Older accepted states may have a
    fresh accepted ``next_required_action=escalated_review`` in
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
    # Defense-in-depth (CRITICAL deadlock fix): only a VALID gate request — major_chapter WITH a
    # complete package in its ledger proposal — may be inferred as a live gate. A mis-routed or
    # package-less accepted escalated_review_pending (e.g. a pre-fix legacy/corrupt state) must NOT
    # be inferred live: it can never be cleared (no package -> record_authorization rejects) and
    # would suppress all sibling work forever. The accept-time guard now blocks new ones; this
    # protects already-persisted old states.
    valid_gate_pids = _escalated_review_package_gate_proposals_from_ledger(ledger_path)
    latest: dict[str, tuple[int, str]] = {}
    for pid, stored in proposal_results.items():
        if not isinstance(pid, str) or not isinstance(stored, dict):
            continue
        if pid not in valid_gate_pids:
            continue
        if stored.get("decision") != "accepted":
            continue
        if stored.get("next_required_action") != "escalated_review":
            continue
        revision = stored.get("state_revision")
        if not isinstance(revision, int):
            continue
        target_id = target_by_proposal.get(pid)
        if not isinstance(target_id, str):
            continue
        target = targets.get(target_id)
        if not isinstance(target, dict) or target.get("state") != "escalated_review_pending":
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
    # Project an env-inflated false exhaustion to repair_active BEFORE the gate checks below, so an
    # exhaustion caused only by env review faults (which never consumed content budget) is gated and
    # dispatched as the repair it really is. A genuine content exhaustion projects to itself.
    decision = _projected_decision(state, proposal_id, stored, ledger_path)

    # 2. Gate on the reconstructed decision.
    if decision.decision != "accepted":
        return _emit({"result": "not_accepted", "proposal_id": proposal_id}, 0)
    if decision.next_required_action in GATED_ACTIONS:
        # tier-2: gate stays closed until a live orchestrator authorization is recorded.
        if writer.is_authorized(proposal_id):
            if decision.next_required_action == "escalated_review":
                command = _read_escalated_review_actuator_command(root)
                if not command:
                    return _emit(
                        {
                            "result": "missing_escalated_review_actuator_command",
                            "proposal_id": proposal_id,
                            "next_required_action": decision.next_required_action,
                        },
                        3,
                    )
                if dry_run:
                    return _emit(
                        {
                            "result": "would_actuate_escalated_review",
                            "proposal_id": proposal_id,
                            "cmd": [
                                "ao-state-writer",
                                "escalated-review-actuate",
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
                return _claim_and_run_escalated_review_actuator_cli(
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
    if decision.next_required_action in ENVIRONMENT_ESCALATION_ACTIONS:
        # A dispatch attempt on a persistently-broken-environment obligation returns the owner-visible
        # escalation (NOT unsupported_current_obligation): the review environment is down, not the
        # action vocabulary. Resumes when a later review receipt supersedes it.
        return _emit(
            _review_environment_unavailable_payload(proposal_id, decision.next_required_action),
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

    # 3b. major_closure_candidate guard: independently re-validate escalated review receipt verdict
    # before auto-spawning closure.  _classify_verdict uses last-match regex on raw text;
    # a mis-parse cannot silently escalate to closure.  We re-read the receipt artifact and
    # confirm the verdict is a genuine pass-family token (not blocker) via a second regex.
    # Fail-closed: any doubt keeps the gate open and returns a human-readable rejection.
    if decision.next_required_action == "major_closure_candidate":
        try:
            _guard_state = _read_state(writer.state_path)
        except UnsupportedStateSchemaVersion:
            return _unsupported_state_schema()
        closure_guard = _escalated_review_closure_receipt_guard(root, _guard_state, ledger_path, proposal_id)
        if closure_guard is not None:
            return _emit(
                {
                    "result": "major_closure_blocked_by_receipt_guard",
                    "proposal_id": proposal_id,
                    "reason": closure_guard,
                },
                3,
            )

    result, current_state_override = _evaluate_continuation_with_todo_mirror_context(
        root=root,
        ledger_path=ledger_path,
        proposal_id=proposal_id,
        decision=decision,
        ao_project_id=ao_project_id,
    )
    if dry_run:
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

    # Pre-claim guards (mirror LIVE): when the stale-mirror repair resolves to blocked or a
    # non-candidate outcome, short-circuit BEFORE claiming the dispatch lease so we never hold a
    # single-flight lease for work that is not actually dispatchable.
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

    # 4. Claim the dispatch (single-flight). is_dispatched already caught the consumed
    #    ("spawned") case above, so a lost claim here means a peer holds a live pending lease.
    if not writer.claim_dispatch(proposal_id):
        return _emit({"result": "in_flight", "proposal_id": proposal_id}, 0)

    # 5. Build the spawn candidate from FRESH governance/TODO/template state, then dispatch.
    # The ENTIRE spawn-baseline feature (pre-spawn source check + post-spawn worker check) is gated on the
    # contract opt-in token spawned_worker_requires_current_source_baseline. LIVE's own contract sets it,
    # so this is behaviorally identical to LIVE's always-on calls for the source project, while a generic
    # adopter that does not opt in spawns unencumbered. The gate MUST cover the pre-spawn check too: its
    # git_probe_failed / source_head_unreadable branches return issues UNCONDITIONALLY (not only when
    # opted in), so leaving it ungated would fail-close non-opted-in dispatches on any git probe glitch.
    requires_baseline = _contract_requires_spawn_baseline(root)
    # 5a. Pre-spawn source-baseline check (b3c): if the source itself cannot provide a trustworthy baseline
    #     (not a git worktree / unreadable HEAD / probe failure), fail fast WITHOUT spawning a worker. For
    #     a healthy git source this returns the HEAD commit, threaded into the post-spawn check below.
    required_source_commit = None
    if requires_baseline:
        required_source_commit, source_issue = _source_baseline_commit_for_spawn(root)
        if source_issue is not None:
            writer.release_dispatch(proposal_id)
            return _emit(_spawn_baseline_issue_payload(proposal_id, source_issue), 3)
    result = continue_after_apply(
        root=root,
        decision=decision,
        ao_project_id=ao_project_id,
        dry_run=False,
        current_state_override=current_state_override,
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

    # C-FIX-11: the worker now exists and we hold its session id, but the dispatch lease is still
    # merely 'pending' until confirm_dispatch. A SIGKILL during the git-bound baseline verification
    # below would leave 'pending' — which is_dispatched() does NOT count — so after 600s the stale-lease
    # reclaim (claim_dispatch fall-through / reconcile-leases) would re-dispatch and DOUBLE-SPAWN the
    # same proposal (the spawn runtime is NOT idempotent: proposal_id dedupe lives only in
    # state-writer's claim/confirm/release). Persist the consume-once 'spawned' record WITH the session
    # id NOW, before baseline verification, closing that crash window. If baseline then fails,
    # record_spawn_baseline_issue overwrites this record with spawned_base_unverified/_mismatch (also
    # consumed, with session-id-based reconcile); if it passes, this IS the final 'spawned' record, so
    # the previously-redundant post-baseline confirm is dropped. (The during-subprocess window — a kill
    # before the spawn returns a session id — is a documented residual: no by-proposal worker lookup
    # exists to safely reclaim it, and a pre-spawn marker would risk a worse false-consume stall.)
    writer.confirm_dispatch(
        proposal_id,
        authorized_by="orchestrator_policy",
        spawn_session_id=result.spawn_session_id,
    )

    # 5b. Post-spawn worker-baseline check (b3c), under the same opt-in gate as 5a. When opted in and the
    #     freshly spawned worker is NOT on the required source baseline, consume the lease as a
    #     spawned_base_* obligation (so the target is never re-spawned on a bad baseline) and surface it
    #     instead of confirming. The B1 merge-base relaxation keeps the check from false-positiving on
    #     normal git histories. (codex Q2 reversal 2026-06-09: OPTION-B-OPTIN, after Option A's always-on
    #     was shown to impose AO session metadata as a generic-engine requirement.)
    if requires_baseline:
        baseline_issue = _spawn_baseline_issue(
            root=root,
            ao_project_id=ao_project_id,
            spawn_session_id=result.spawn_session_id or "",
            required_source_commit=required_source_commit,
        )
        if baseline_issue is not None:
            reason = str(
                baseline_issue.get("reason") or baseline_issue.get("result") or "spawn_baseline_unverified"
            )
            writer.record_spawn_baseline_issue(
                proposal_id,
                spawn_session_id=str(result.spawn_session_id or ""),
                reason=reason,
                required_source_commit=(
                    str(baseline_issue["required_source_commit"])
                    if baseline_issue.get("required_source_commit")
                    else None
                ),
                worker_worktree=(
                    str(baseline_issue["worker_worktree"]) if baseline_issue.get("worker_worktree") else None
                ),
                worker_head=str(baseline_issue["worker_head"]) if baseline_issue.get("worker_head") else None,
                spawn_session_termination=None,
                ao_project_id=ao_project_id,
            )
            payload = _spawn_baseline_issue_payload(
                proposal_id, {**baseline_issue, "baseline_attestation_result": reason}
            )
            return _emit(payload, 3)

    # Baseline verified (or not required). The consume-once 'spawned' record (with session id) was
    # already persisted before baseline verification (C-FIX-11), so no second confirm_dispatch is
    # needed here.
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


def _is_spawned_dispatch_reconcile_action(action: object) -> bool:
    """True iff ``action`` is an ordinary fast auto-spawn continuation action whose orphaned
    'pending' lease is safe for the deterministic janitor to reclaim. Excludes the external
    review/actuator actions (codex_cc_review, escalated_review), which legitimately hold a
    'pending' lease far longer than the orphan floor — reclaiming one of those could let a second
    actuator run double-submit."""
    if action in {"codex_cc_review", "escalated_review"}:
        return False
    return action in AUTO_SPAWN_ACTIONS or action == "repair_attempts_exhausted"


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
    # Read-only audit side-channel: surface superseded leases + their advisory reconcile commands so an
    # unattended orchestrator can discover how to clean them up (task#14a-1). Always present (empty list
    # when none) across all three result branches.
    historical = _historical_dispatch_records(state, writer, ledger_path)
    if gated and not ready:
        return _emit(
            {
                "result": "requires_orchestrator_authorization",
                "ready_candidates": ready,
                "gated_candidates": gated,
                "historical_dispatch_records": historical,
            },
            3,
        )
    if ready:
        return _emit(
            {
                "result": "ready",
                "ready_candidates": ready,
                "gated_candidates": gated,
                "historical_dispatch_records": historical,
            },
            0,
        )
    return _emit(
        {
            "result": "nothing_to_continue",
            "ready_candidates": [],
            "gated_candidates": [],
            "historical_dispatch_records": historical,
        },
        0,
    )


def cmd_reconcile_leases(args: argparse.Namespace) -> int:
    """Deterministic janitor: delete provably-orphaned fast auto-spawn 'pending' dispatch leases.

    When an orchestrator session is SIGKILLed between claim_dispatch and confirm_dispatch a
    'pending' dispatch lease is orphaned, and nothing re-runs the reclaim on its own (claim_dispatch's
    reclaim is a fall-through reached only by re-dispatching, which spawns a worker). This
    identity-free command lets a non-LLM caller (sidecar/cron) clear that orphan class so a revived
    orchestrator finds a clean lease table.

    Fail-closed scope: a lease is reclaimed ONLY when its stored next_required_action is a fast
    auto-spawn continuation action (``_is_spawned_dispatch_reconcile_action`` is True). That
    deliberately EXCLUDES escalated_review / codex_cc_review actuator/review leases, which
    legitimately hold 'pending' far longer than the orphan floor; deleting one could let a second
    actuator run double-submit. Unclassifiable leases (no proposal_result) are left for the
    LLM/human. The writer re-checks the orphan-age under the single-flight lock before deleting, so a
    lease a concurrent claim refreshed is never reclaimed.
    """
    state_path, ledger_path = _state_paths(args.root)
    writer = StateWriter(state_path=state_path, ledger_path=ledger_path)
    try:
        state = _read_state(state_path)
    except UnsupportedStateSchemaVersion:
        return _unsupported_state_schema()
    pause_issue = _operator_pause_issue(state)
    if pause_issue is not None:
        return _emit(*pause_issue)
    dispatched = state.get("dispatched_proposals", {})
    if not isinstance(dispatched, dict):
        dispatched = {}
    proposal_results = state.get("proposal_results", {})
    if not isinstance(proposal_results, dict):
        proposal_results = {}
    candidates: list[str] = []
    action_skipped: list[dict[str, object]] = []
    for proposal_id, record in dispatched.items():
        if not (
            isinstance(record, dict)
            and record.get("status") == "pending"
            and StateWriter._pending_lease_expired(record)
        ):
            continue
        stored = proposal_results.get(proposal_id)
        if not isinstance(stored, dict):
            action_skipped.append({"proposal_id": proposal_id, "reason": "no_proposal_result"})
            continue
        action = stored.get("next_required_action")
        if _is_spawned_dispatch_reconcile_action(action):
            candidates.append(proposal_id)
        else:
            action_skipped.append(
                {
                    "proposal_id": proposal_id,
                    "next_required_action": action,
                    "reason": "actuator_or_review_or_unknown_action",
                }
            )
    outcome = writer.reclaim_orphaned_pending_leases(candidates, apply=bool(args.apply))
    return _emit(
        {
            "result": "reclaimed" if args.apply else "would_reclaim",
            "applied": bool(args.apply),
            "reclaimed": outcome["reclaimed"],
            "not_expired_at_lock": outcome["skipped"],
            "action_skipped": action_skipped,
        },
        0,
    )


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
    decision = _projected_decision(state, proposal_id, stored, ledger_path)
    if decision.decision != "accepted":
        return {"result": "not_accepted", "proposal_id": proposal_id}, 0
    if decision.next_required_action != "escalated_review":
        return {
            "result": "not_escalated_review_gate",
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
    package = authorization.get("escalated_review_gate", {})
    if not isinstance(package, dict) or not package.get("package_sha256"):
        return {"result": "missing_escalated_review_package", "proposal_id": proposal_id}, 3
    target_id = package.get("target_id") or proposal.get("target_id")
    target_kind = package.get("target_kind") or proposal.get("target_kind")
    target = state.get("targets", {}).get(target_id, {}) if isinstance(target_id, str) else {}
    # C-FIX-12: the actuator-time gate check must match the DISPATCH live-gate predicate and the
    # receipt-apply rejection (writer-side _current_escalated_review_gate_id_for_target, which
    # returns a gate ONLY while the target is in escalated_review_pending). The sticky
    # `active_escalated_review_gate_proposal_id` is never cleared, so a pointer-only check passed
    # even after a watchdog hard-timeout (or any blocker) moved the target to review_blocked —
    # letting a crash-reclaimed (>=7800s, C-FIX-10) or stale direct actuate build a review job and
    # re-SEND to the external reviewer, with only the LATER receipt-apply rejecting it (too late;
    # the external submission already happened — the bridge is NOT consume-once). Require the
    # pending state HERE so the stale actuate returns nonzero before the bridge runs, and the
    # actuator path then release_dispatches the reclaimed lease. Fail closed (refuse) if the
    # target is missing/malformed.
    if (
        not isinstance(target, dict)
        or target.get("active_escalated_review_gate_proposal_id") != proposal_id
        or target.get("state") != "escalated_review_pending"
    ):
        return {"result": "stale_escalated_review_gate", "proposal_id": proposal_id}, 3

    package_sha256 = package.get("package_sha256")
    submission_nonce = package.get("external_review_submission_nonce")
    return {
        "result": "review_job",
        "proposal_id": proposal_id,
        "target_id": target_id,
        "target_kind": target_kind,
        "review_scope": "escalated_review",
        "next_required_action": decision.next_required_action,
        "package_path": package.get("package_path"),
        "prompt_path": package.get("prompt_path"),
        "package_sha256": package_sha256,
        "external_review_submission_nonce": submission_nonce,
        "human_egress_required": False,
        "external_actuator_required": True,
        "unattended_required": True,
        "actuator_identity_internal": True,
        "manual_receipt_apply_forbidden": True,
        "api_transport_allowed": False,
        "required_model_class": "pro",
        "required_caller_type": "escalated_review_actuator",
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
            "actor_role": "escalated_review",
            "review_scope": "escalated_review",
            "package_sha256": package_sha256,
            "external_review_receipt_sha256": "<sha256-of-captured-review-receipt>",
            "external_review_submission_nonce": submission_nonce,
            "external_review_artifact_ref": "artifact:reports/<receipt-artifact>",
            "external_review_gate_proposal_id": proposal_id,
        },
    }, 0


def cmd_retire_dead_sessions(args: argparse.Namespace) -> int:
    """Maintenance reconcile: retire provably-dead worker sessions by writing AO's own lost-runtime
    terminal lifecycle (state-writer GC; no runtime command/poke is issued). Dry-run by default;
    --apply mutates session JSON under the AO-compatible per-file lock.

    P5 integration (codex V2_DESIGN APPROVE-WITH-CHANGES): --apply is a canonical-mutating,
    preflight-bypassing write (it never calls preflight_reconcile), so — exactly like the other
    preflight-bypassing writers P5 guards (record-final-convergence / reconcile-leases /
    reconcile-spawn-attestation / reconcile-spawned-dispatch) — it honors operator_pause read from
    the --root canonical state.json (NOT the sessions dir). A VALID pause BLOCKS --apply
    (operator_paused, exit 3, zero session-file mutation) but ALLOWS dry-run (read-only). A MALFORMED
    operator_pause record fails closed for BOTH modes (never silently treated as unpaused, which
    would let a corrupt pause record permit a mutating reap)."""
    project_id = args.project_id or read_contract_project_id(args.root)
    if not project_id:
        return _emit({"result": "missing_project_id"}, 3)

    state_path, _ledger_path = _state_paths(args.root)
    try:
        state = _read_state(state_path)
    except UnsupportedStateSchemaVersion:
        return _unsupported_state_schema()
    pause_issue = _operator_pause_issue(state)
    if pause_issue is not None:
        payload, exit_code = pause_issue
        # malformed -> fail closed for BOTH modes; valid pause -> block only the mutating --apply.
        if payload.get("result") == "operator_pause_malformed" or args.apply:
            return _emit(payload, exit_code)

    sessions_dir = _ao_projects_root() / project_id / "sessions"
    live_tmux = session_reaper.live_tmux_names()
    now_epoch = time.time()
    if not args.apply:
        report = session_reaper.plan(
            sessions_dir,
            now_epoch=now_epoch,
            grace_seconds=args.grace_seconds,
            live_tmux=live_tmux,
        )
        return _emit(report, 0)
    audit_path = _ao_projects_root() / project_id / session_reaper.AUDIT_FILENAME
    report = session_reaper.apply_retirements(
        sessions_dir,
        now_epoch=now_epoch,
        stamp=session_reaper.now_iso(),
        grace_seconds=args.grace_seconds,
        live_tmux=live_tmux,
        audit_path=audit_path,
    )
    return _emit(report, 0)


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


def cmd_escalated_review_actuate(args: argparse.Namespace) -> int:
    state_path, ledger_path = _state_paths(args.root)
    writer = StateWriter(state_path=state_path, ledger_path=ledger_path)
    preflight = preflight_reconcile(root=args.root, writer=writer, ledger_path=ledger_path)
    if preflight is not None:
        payload, exit_code = preflight
        payload.setdefault("proposal_id", args.proposal_id)
        return _emit(payload, exit_code)
    proof_issue = _orchestrator_cli_proof_issue(writer=writer, proposal_id=args.proposal_id)
    if proof_issue is not None:
        return _emit(proof_issue, 3)
    contract_bridge_command = _read_escalated_review_actuator_command(args.root)
    if not contract_bridge_command:
        return _emit(
            {
                "result": "missing_escalated_review_actuator_command",
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
                "result": "invalid_escalated_review_actuator_command",
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
    return _claim_and_run_escalated_review_actuator_cli(
        root=args.root,
        writer=writer,
        ledger_path=ledger_path,
        proposal_id=args.proposal_id,
        bridge_command=contract_bridge_command,
        bridge_timeout_seconds=args.bridge_timeout_seconds,
    )


def _try_acquire_machine_actuator_lock() -> int | None:
    """Non-blocking MACHINE-GLOBAL mutex for the escalated-review actuator (multi-project).

    With multiple projects registered on one machine, each project has its own dispatch ledger,
    so per-project single-flight (claim_dispatch) cannot stop TWO projects' actuators from driving
    the ONE machine-global external review surface (one browser endpoint, one reviewer account)
    concurrently. This lock is an fcntl.flock on a path OUTSIDE any project root (default
    ``~/.agent-orchestrator/locks/escalated-review-actuator.lock``; override the directory with
    ``AO_ESCALATED_REVIEW_ACTUATOR_LOCK_DIR``), so every project's actuator contends on the same
    inode. Non-blocking by design: a busy machine means "yield and retry later", never "queue up
    behind an hours-long bridge run". Returns the HELD fd (caller must os.close() it to release —
    the kernel also releases on process exit/SIGKILL, so a dead holder cannot wedge the machine),
    or None when another holder owns it."""
    lock_dir = Path(
        os.environ.get("AO_ESCALATED_REVIEW_ACTUATOR_LOCK_DIR")
        or Path.home() / ".agent-orchestrator" / "locks"
    ).expanduser()
    lock_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_dir / "escalated-review-actuator.lock", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except OSError:
        os.close(fd)
        raise
    # Forensics only (the flock alone guarantees exclusion): record the holder.
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"pid={os.getpid()} time={time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n".encode("utf-8"))
    except OSError:
        pass
    return fd


def _claim_and_run_escalated_review_actuator_cli(
    *,
    root: Path,
    writer: StateWriter,
    ledger_path: Path,
    proposal_id: str,
    bridge_command: str,
    bridge_timeout_seconds: int,
) -> int:
    # Defense-in-depth (matches LIVE cli.py:2657): this receipt-minting helper is reached both from the
    # actuate command (gated above) AND from the authorized `continue` auto-spawn path, so it re-asserts
    # the orchestrator proof itself — a non-orchestrator caller cannot mint an actuator receipt even if a
    # future caller bypasses the outer command gate.
    proof_issue = _orchestrator_cli_proof_issue(writer=writer, proposal_id=proposal_id)
    if proof_issue is not None:
        return _emit(proof_issue, 3)
    receipt_proposal_id = _escalated_review_receipt_proposal_id(proposal_id)
    try:
        state = _read_state(writer.state_path)
    except UnsupportedStateSchemaVersion:
        return _unsupported_state_schema()
    existing = state.get("proposal_results", {}).get(receipt_proposal_id)
    if isinstance(existing, dict) and existing.get("decision") == "accepted":
        if not writer.is_dispatched(proposal_id):
            writer.confirm_dispatch(proposal_id, authorized_by="escalated_review_actuator")
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
    # M2-1 multi-project mutex: AFTER the per-project claim succeeds, take the machine-global
    # actuator flock. BUSY means another project's actuator is mid-bridge on this machine's one
    # external review surface — release THIS project's lease (in_flight semantics: the obligation
    # is not consumed; a later actuate retries) and yield with exit 0. The lock is held across the
    # ENTIRE bridge run and released on every path (success/failure/exception) by the finally;
    # a SIGKILLed holder is released by the kernel on process death.
    machine_lock_fd = _try_acquire_machine_actuator_lock()
    if machine_lock_fd is None:
        writer.release_dispatch(proposal_id)
        return _emit(
            {"result": "escalated_review_actuator_busy", "proposal_id": proposal_id}, 0
        )
    try:
        exit_code = _run_escalated_review_actuator_cli(
            root=root,
            writer=writer,
            ledger_path=ledger_path,
            proposal_id=proposal_id,
            bridge_command=bridge_command,
            bridge_timeout_seconds=bridge_timeout_seconds,
        )
    finally:
        os.close(machine_lock_fd)  # LOCK_UN is implicit in the close; path stays (lock namespace)
    if exit_code == 0:
        writer.confirm_dispatch(proposal_id, authorized_by="escalated_review_actuator")
    else:
        writer.release_dispatch(proposal_id)
    return exit_code


def _run_escalated_review_actuator_cli(
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

    receipt_proposal_id = _escalated_review_receipt_proposal_id(proposal_id)
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

    bridge_result, bridge_exit = _run_escalated_review_bridge(
        root=root,
        command=bridge_command,
        job=job,
        timeout_seconds=bridge_timeout_seconds,
    )
    if bridge_exit != 0:
        return _record_escalated_review_actuator_failure(
            writer=writer,
            job=job,
            proposal_id=proposal_id,
            reason=str(bridge_result.get("reason") or bridge_result.get("result") or "bridge_failed"),
            detail=str(bridge_result.get("detail") or ""),
            expected_package_sha=str(expected_package_sha),
        )

    bridge_package_sha = bridge_result.get("package_sha256")
    if bridge_package_sha != expected_package_sha:
        return _emit(
            {
                "result": "escalated_review_package_sha_mismatch",
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
    # Accept the pass_with_advisory alias here too; the writer normalizes it -> advisory at apply
    # time. Rejecting it at this actuator gate would wedge a legitimate pass receipt before the
    # writer ever sees it.
    if verdict not in {"advisory", "pass", "pass_with_nits", "pass_with_advisory", "blocker"}:
        return _record_escalated_review_actuator_failure(
            writer=writer,
            job=job,
            proposal_id=proposal_id,
            reason="unsupported_escalated_review_verdict",
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
        actor_role="escalated_review",
        evidence_refs=[
            f"escalated-review-actuator:{proposal_id}",
            artifact_ref,
            f"package_sha256:{expected_package_sha}",
        ],
        summary=str(bridge_result.get("summary") or ""),
        review_scope="escalated_review",
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
    os.environ[CALLER_TYPE_ENV] = "escalated_review_actuator"
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
            "result": "escalated_review_recorded",
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


def _record_escalated_review_actuator_failure(
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

    failure_proposal_id = _escalated_review_actuator_failure_proposal_id(proposal_id)
    proposal = StateTransitionProposal(
        proposal_id=failure_proposal_id,
        target_kind=str(template.get("target_kind")),
        target_id=str(template.get("target_id")),
        base_state_revision=int(template.get("base_state_revision", 0)),
        requested_state="closure_candidate",
        actor_role="escalated_review",
        evidence_refs=[
            f"escalated-review-actuator-failure:{proposal_id}",
            f"package_sha256:{expected_package_sha}",
            f"reason:{_safe_component(reason)}",
        ],
        summary=f"escalated review actuator failed before a usable external review receipt was harvested: {reason}",
        review_scope="escalated_review",
        verdict="blocker",
        model="escalated_review_actuator",
        review_mode="actuator_failure",
        blocker_code=ESCALATED_REVIEW_ACTUATOR_FAILURE_BLOCKER_CODE,
        blocker_detail=detail or reason,
        package_sha256=expected_package_sha,
        external_review_gate_proposal_id=proposal_id,
    )

    previous_caller = os.environ.get(CALLER_TYPE_ENV)
    os.environ[CALLER_TYPE_ENV] = "escalated_review_actuator"
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
                "result": "escalated_review_actuator_failure_rejected",
                "proposal_id": proposal_id,
                "failure_proposal_id": failure_proposal_id,
                "reason": decision.reason,
                "bridge_failure_reason": reason,
            },
            2,
        )

    return _emit(
        {
            "result": "escalated_review_actuator_failure_recorded",
            "proposal_id": proposal_id,
            "failure_proposal_id": failure_proposal_id,
            "bridge_failure_reason": reason,
            "state_revision": decision.state_revision,
            "next_required_action": decision.next_required_action,
            "new_state": decision.new_state,
        },
        0,
    )


def _run_escalated_review_bridge(
    *,
    root: Path,
    command: str,
    job: dict[str, object],
    timeout_seconds: int,
) -> tuple[dict[str, object], int]:
    bridge_env = os.environ.copy()
    bridge_env["AO_ESCALATED_REVIEW_PROPOSAL_ID"] = str(job.get("proposal_id") or "")
    bridge_env["AO_ESCALATED_REVIEW_SUBMISSION_NONCE"] = str(job.get("external_review_submission_nonce") or "")
    bridge_env["AO_ESCALATED_REVIEW_PACKAGE_SHA256"] = str(job.get("package_sha256") or "")
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
            "stdout_sha256": hashlib.sha256(completed.stdout.encode("utf-8", errors="replace")).hexdigest(),
            "stderr_sha256": hashlib.sha256(completed.stderr.encode("utf-8", errors="replace")).hexdigest(),
            "stdout_bytes": len(completed.stdout.encode("utf-8", errors="replace")),
            "stderr_bytes": len(completed.stderr.encode("utf-8", errors="replace")),
        }
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            failure["reason"] = str(payload.get("error") or payload.get("reason") or "bridge_command_failed")
            for key in ("error", "artifact_path"):
                if key in payload:
                    failure[f"bridge_{key}"] = payload[key]
        return failure, 3
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return {
            "result": "invalid_bridge_output",
            "detail": str(exc),
            "stdout_sha256": hashlib.sha256(completed.stdout.encode("utf-8", errors="replace")).hexdigest(),
            "stdout_bytes": len(completed.stdout.encode("utf-8", errors="replace")),
        }, 3
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
        / "escalated-review-receipts"
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


def _escalated_review_receipt_proposal_id(proposal_id: str) -> str:
    return f"escalated-review-receipt-{_safe_component(proposal_id)}"


def _escalated_review_actuator_failure_proposal_id(proposal_id: str) -> str:
    return f"escalated-review-actuator-failure-{_safe_component(proposal_id)}"


# Pass-family verdicts that may advance to major closure.
_ESCALATED_REVIEW_PASS_VERDICTS = frozenset({"pass", "pass_with_nits", "advisory"})
# Independent re-validation regex — does NOT rely on last-match semantics; requires
# the verdict field to appear as a JSON-key-like token immediately before its value.
# Includes the pass_with_advisory alias (ordered BEFORE `pass` so the longer alias wins; `pass\b`
# alone never matches inside "pass_with_advisory" — the trailing `_` is a word char). This RAW-artifact
# re-scan must recognize the alias or a legitimate pass receipt wedges major closure; tokens are
# normalized through _REVIEW_VERDICT_ALIASES before the pass-family membership test below.
_RECEIPT_VERDICT_RE = re.compile(
    r'(?i)\bverdict\b.{0,10}[:：].{0,5}(blocker|pass_with_advisory|pass_with_nits|advisory|pass)\b'
)
# Blocker-semantic signal regex (Hole 1 fix): fail-closed if the artifact contains
# a non-null blocker_code field OR a severity=blocker field in any finding, regardless
# of what the top-level verdict token says.  Does NOT fire on "blocker_code": null/""
# so legitimate pass receipts with null blocker_code are not incorrectly rejected.
_BLOCKER_SIGNAL_RE = re.compile(
    r'(?i)"blocker_code"\s*:\s*(?![\s"]*null\b|[\s"]*"\s*"\s*[,}\]])'
    r'|"severity"\s*:\s*"blocker"'
)


def _escalated_review_closure_receipt_guard(
    root: Path, state: dict, ledger_path: Path, proposal_id: str
) -> str | None:
    """Return an error string if the escalated review receipt cannot be independently re-validated
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
    # proposal_id here is the receipt proposal (escalated-review-receipt-XXX), not the gate proposal,
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
    if stored_verdict not in _ESCALATED_REVIEW_PASS_VERDICTS:
        return (
            f"receipt proposal {proposal_id!r} verdict {stored_verdict!r} is not pass-family; "
            "major closure requires pass/advisory/pass_with_nits"
        )

    # 2. Artifact ref from ledger proposal evidence_refs (Hole 2 fix: exact binding).
    artifact_ref: str | None = None
    for ref in ledger_proposal.get("evidence_refs") or []:
        if isinstance(ref, str) and ref.startswith("artifact:reports/escalated-review-receipts/"):
            artifact_ref = ref
            break
    if artifact_ref is None:
        return (
            f"no escalated-review-receipts artifact ref in ledger entry for {proposal_id!r}; "
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
    # Normalize each found token through the writer's alias map (single source of truth) so
    # pass_with_advisory -> advisory before the pass-family membership test below. The blocker
    # check is unaffected (blocker is not aliased).
    verdicts_found = [
        _REVIEW_VERDICT_ALIASES.get(token, token)
        for token in (m.group(1).lower() for m in _RECEIPT_VERDICT_RE.finditer(raw))
    ]
    if not verdicts_found:
        return "no verdict token found in receipt artifact during re-validation"
    if any(v == "blocker" for v in verdicts_found):
        return (
            f"receipt artifact contains 'blocker' verdict token(s) {verdicts_found}; "
            "refusing major closure auto-spawn"
        )
    if not any(v in _ESCALATED_REVIEW_PASS_VERDICTS for v in verdicts_found):
        return (
            f"no pass-family verdict found in receipt artifact {verdicts_found}; "
            "refusing major closure auto-spawn"
        )
    return None


def _read_escalated_review_actuator_command(root: Path) -> str | None:
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
        command = review_policy.get("escalated_review_actuator_command")
    else:
        command = _extract_review_policy_string(text, "escalated_review_actuator_command")
    if not isinstance(command, str) or not command.strip():
        return None
    return command


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
    decision = _projected_decision(state, proposal_id, stored, ledger_path)

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


def cmd_record_final_convergence(args: argparse.Namespace) -> int:
    """Consume a repair_attempts_exhausted obligation as an audited owner-proxy convergence decision.

    This is the consumer that flips the convergence dead-end: once recorded, the proposal becomes
    is_dispatched (status final_convergence_recorded) so the global preflight skips it and the loop
    reaches a clean converged terminal (continue -> nothing_to_continue, list-ready -> candidates=[]).
    """
    root_payload = _non_canonical_root_payload(args.root)
    if root_payload is not None:
        return _emit(root_payload, 3)
    contract_error = _validate_contract(args.root)
    if contract_error is not None:
        return _emit({"result": contract_error, "proposal_id": args.proposal_id}, 3)

    state_path, ledger_path = _state_paths(args.root)
    writer = StateWriter(state_path=state_path, ledger_path=ledger_path)
    try:
        state = _read_state(state_path)
    except UnsupportedStateSchemaVersion:
        return _unsupported_state_schema()

    pause_issue = _operator_pause_issue(state)
    if pause_issue is not None:
        return _emit(*pause_issue)

    result = writer.record_final_convergence(
        proposal_id=args.proposal_id,
        evidence_refs=args.evidence,
        outcome=args.outcome,
    )
    payload = dict(result)
    if payload.get("decision") == "recorded":
        payload["result"] = "owner_proxy_final_convergence_recorded"
        return _emit(payload, 0)
    payload["result"] = "owner_proxy_final_convergence_rejected"
    return _emit(payload, 2)


def cmd_pause(args: argparse.Namespace) -> int:
    """Set a first-class, state-visible operator pause (P5). Orchestrator-owner-proxy caller only.

    Ships the engine capability; flipping a deployment's live pause flag is a separate
    operator-authorized operation (not performed by tests). ``--set-by`` is a free-text audit
    label, NOT authentication — the caller-type proof is the gate.
    """
    root_payload = _non_canonical_root_payload(args.root)
    if root_payload is not None:
        return _emit(root_payload, 3)
    contract_error = _validate_contract(args.root)
    if contract_error is not None:
        return _emit({"result": contract_error}, 3)
    state_path, ledger_path = _state_paths(args.root)
    writer = StateWriter(state_path=state_path, ledger_path=ledger_path)
    proof_issue = _orchestrator_cli_proof_issue(writer=writer)
    if proof_issue is not None:
        return _emit(proof_issue, 3)
    record = writer.set_operator_pause(reason=args.reason, set_by=args.set_by)
    return _emit({"result": "operator_paused", "operator_pause": record}, 0)


def cmd_resume(args: argparse.Namespace) -> int:
    """Clear a first-class operator pause (P5). Orchestrator-owner-proxy caller only."""
    root_payload = _non_canonical_root_payload(args.root)
    if root_payload is not None:
        return _emit(root_payload, 3)
    contract_error = _validate_contract(args.root)
    if contract_error is not None:
        return _emit({"result": contract_error}, 3)
    state_path, ledger_path = _state_paths(args.root)
    writer = StateWriter(state_path=state_path, ledger_path=ledger_path)
    proof_issue = _orchestrator_cli_proof_issue(writer=writer)
    if proof_issue is not None:
        return _emit(proof_issue, 3)
    outcome = writer.clear_operator_pause()
    return _emit({"result": "operator_resumed", **outcome}, 0)


def cmd_dispatch_stall_check(args: argparse.Namespace) -> int:
    """Read-only owner-visible escalation (V1): detect a dispatch obligation that has stayed READY
    across >= threshold consecutive checks with NO state_revision advance — the systemic
    "detector but no autonomous tier-1 executor" stall (the orchestrator LLM is the sole dispatcher;
    if it is down/wedged the ready obligation persists and the sidecar only pokes it forever).

    OBSERVABILITY ONLY: this NEVER dispatches/spawns/authorizes or writes canonical state (it never
    calls _dispatch_one/cmd_continue/cmd_dispatch/continue_after_apply). It mirrors the
    final-convergence parking projection (owner-visible halt, no policy decision) and the watchdog's
    read-only discipline. The no-advance watermark is CALLER-PASSED (--last-seen-revision /
    --no-advance-count); the next watermark is RETURNED for the caller (sidecar) to persist — the
    engine writes nothing, and the sidecar wiring/activation is a separate owner-gated change.

    Strictly distinct from the final-convergence parked halt: that fires only with NO ready/gated
    candidates (content-exhaustion parked); this fires only WITH a ready candidate (dispatch not
    actuated), so the two are mutually exclusive. operator_pause is excluded FIRST (an intentionally
    paused project is never a stall).
    """
    root_payload = _non_canonical_root_payload(args.root)
    if root_payload is not None:
        return _emit(root_payload, 3)
    contract_error = _validate_contract(args.root)
    if contract_error is not None:
        return _emit({"result": contract_error}, 3)
    threshold = args.threshold_checks
    no_advance_count = args.no_advance_count
    last_seen_revision = args.last_seen_revision
    if threshold < 1 or no_advance_count < 0 or (last_seen_revision is not None and last_seen_revision < 0):
        return _emit(
            {
                "result": "invalid_dispatch_stall_watermark",
                "dispatch_stalled": False,
                "threshold_checks": threshold,
                "no_advance_count": no_advance_count,
                "last_seen_revision": last_seen_revision,
            },
            2,
        )

    state_path, ledger_path = _state_paths(args.root)
    writer = StateWriter(state_path=state_path, ledger_path=ledger_path)
    try:
        state = _read_state(state_path)
    except UnsupportedStateSchemaVersion:
        return _unsupported_state_schema()

    current_revision = state.get("state_revision")
    if not isinstance(current_revision, int):
        current_revision = 0

    # Pause exclusion FIRST: an intentionally paused project is never a dispatch stall.
    pause_issue = _operator_pause_issue(state)
    if pause_issue is not None:
        payload, _exit = pause_issue
        if payload.get("result") == "operator_paused":
            return _emit(
                {
                    "result": "operator_paused",
                    "dispatch_stalled": False,
                    "state_revision": current_revision,
                    "next_watermark": {"last_seen_revision": current_revision, "no_advance_count": 0},
                },
                0,
            )
        return _emit(*pause_issue)  # malformed operator_pause -> fail closed

    ready = _ready_candidates(state, writer, ledger_path)
    if not ready:
        # No ready obligation => not a dispatch stall. (A final-convergence-parked project also has NO
        # ready/gated candidates, so it can never be reported as a dispatch stall: mutually exclusive.)
        return _emit(
            {
                "result": "no_ready_obligation",
                "dispatch_stalled": False,
                "state_revision": current_revision,
                "next_watermark": {"last_seen_revision": current_revision, "no_advance_count": 0},
            },
            0,
        )

    if last_seen_revision is None or current_revision > last_seen_revision:
        # First-ever check, or the obligation advanced since the last check: streak resets to 1.
        return _emit(
            {
                "result": "dispatch_progressing" if last_seen_revision is None else "dispatch_advanced",
                "dispatch_stalled": False,
                "state_revision": current_revision,
                "ready_candidates": ready,
                "next_watermark": {"last_seen_revision": current_revision, "no_advance_count": 1},
            },
            0,
        )

    if current_revision < last_seen_revision:
        # Revision rewound below the caller watermark: stale / cross-root watermark, NOT a no-advance
        # stall. Fail closed and have the caller reset the watermark.
        return _emit(
            {
                "result": "dispatch_stall_watermark_regressed",
                "dispatch_stalled": False,
                "state_revision": current_revision,
                "last_seen_revision": last_seen_revision,
                "next_watermark": {"last_seen_revision": current_revision, "no_advance_count": 1},
            },
            3,
        )

    # current_revision == last_seen_revision: ready obligation present, no advance since last check.
    streak = no_advance_count + 1
    if streak >= threshold:
        return _emit(
            {
                "result": "dispatch_stalled_no_advance",
                "dispatch_stalled": True,
                "current_obligation": True,
                "state_revision": current_revision,
                "ready_candidates": ready,
                "no_advance_count": streak,
                "threshold_checks": threshold,
                "forbidden_actions": PRECHECK_FORBIDDEN_ACTIONS,
                "allowed_repair_actions": ["orchestrator_session_rebind"],
                "next_watermark": {"last_seen_revision": current_revision, "no_advance_count": streak},
            },
            3,
        )
    return _emit(
        {
            "result": "dispatch_no_advance_below_threshold",
            "dispatch_stalled": False,
            "state_revision": current_revision,
            "ready_candidates": ready,
            "no_advance_count": streak,
            "threshold_checks": threshold,
            "next_watermark": {"last_seen_revision": current_revision, "no_advance_count": streak},
        },
        0,
    )


def cmd_reconcile_spawn_attestation(args: argparse.Namespace) -> int:
    """Promote a spawned_unattested lease to spawned (session found) or release it (session absent).

    On the public core's simpler dispatch model a fully-attested dispatch is already 'spawned', so
    this consumer rejects 'not_spawned_unattested' (exit 2) for an ordinary post-spawn record — it is
    wired but inert until a missing-attestation spawn produces a spawned_unattested lease.
    """
    root_payload = _non_canonical_root_payload(args.root)
    if root_payload is not None:
        return _emit(root_payload, 3)
    contract_error = _validate_contract(args.root)
    if contract_error is not None:
        return _emit({"result": contract_error, "proposal_id": args.proposal_id}, 3)

    state_path, ledger_path = _state_paths(args.root)
    writer = StateWriter(state_path=state_path, ledger_path=ledger_path)
    try:
        state = _read_state(state_path)
    except UnsupportedStateSchemaVersion:
        return _unsupported_state_schema()
    pause_issue = _operator_pause_issue(state)
    if pause_issue is not None:
        return _emit(*pause_issue)
    result = writer.reconcile_spawn_attestation(
        proposal_id=args.proposal_id,
        evidence_refs=args.evidence,
        spawn_session_id=args.spawn_session_id,
        release_if_absent=args.release_if_absent,
    )
    payload = dict(result)
    if payload.get("decision") == "recorded":
        payload["result"] = "spawn_attestation_recorded"
        return _emit(payload, 0)
    if payload.get("decision") == "released":
        payload["result"] = "spawn_attestation_absent_released"
        return _emit(payload, 0)
    payload["result"] = "spawn_attestation_reconcile_rejected"
    return _emit(payload, 2)


def cmd_reconcile_spawned_dispatch(args: argparse.Namespace) -> int:
    """Refresh, or release-if-overdue, a 'spawned' dispatch lease — including the spawn-baseline
    (spawned_base_mismatch / spawned_base_unverified) leases b1+b2 record.

    For a base-status lease under --release-if-terminal this performs a live ao-session liveness readback
    (b3c) and passes the result as `terminal_readback` so the writer releases the lease ONLY once the
    offending worker is proven gone (mismatch: stored-terminated+absent OR a fresh absent readback;
    unverified: a fresh absent readback). A still-active or unverifiable worker fails closed (exit 2). The
    normal 'spawned' lease path is unchanged (no readback, no shellout). The action-/orchestrator-/status
    gating lives in writer.reconcile_spawned_dispatch (b3a), so this CLI does NOT duplicate LIVE's full
    pre-gate block; it only shells out to `ao session ls` when the record is actually a base-status lease
    being released.
    """
    root_payload = _non_canonical_root_payload(args.root)
    if root_payload is not None:
        return _emit(root_payload, 3)
    contract_error = _validate_contract(args.root)
    if contract_error is not None:
        return _emit({"result": contract_error, "proposal_id": args.proposal_id}, 3)

    state_path, ledger_path = _state_paths(args.root)
    writer = StateWriter(state_path=state_path, ledger_path=ledger_path)
    try:
        state = _read_state(state_path)
    except UnsupportedStateSchemaVersion:
        return _unsupported_state_schema()

    pause_issue = _operator_pause_issue(state)
    if pause_issue is not None:
        return _emit(*pause_issue)

    # Spawn-baseline liveness readback (b3c). ONLY for a base-status lease being released — a normal
    # 'spawned' lease never shells out to `ao session ls`. The writer (b3a) still owns all gating; this
    # block just proves the offending worker is gone so the writer may release.
    dispatch_record = writer.dispatch_record(args.proposal_id)
    terminal_readback: dict[str, object] | None = None
    if (
        isinstance(dispatch_record, dict)
        and dispatch_record.get("status") == "spawned_base_unverified"
        and args.release_if_terminal
    ):
        spawn_session_id = dispatch_record.get("spawn_session_id")
        if not isinstance(spawn_session_id, str) or not spawn_session_id:
            return _emit(
                {
                    "result": "spawned_dispatch_reconcile_rejected",
                    "reason": "spawn_baseline_unverified_missing_session_id",
                    "proposal_id": args.proposal_id,
                },
                2,
            )
        recorded_project_id = dispatch_record.get("ao_project_id")
        project_id = recorded_project_id if isinstance(recorded_project_id, str) and recorded_project_id else None
        readback = _confirmed_absent_readback(spawn_session_id, project_id or read_contract_project_id(args.root))
        if readback.get("result") == "active":
            return _emit(
                {
                    "result": "spawned_dispatch_reconcile_rejected",
                    "reason": "spawn_baseline_unverified_worker_still_active",
                    "proposal_id": args.proposal_id,
                    "readback": readback,
                },
                2,
            )
        if readback.get("result") != "absent":
            return _emit(
                {
                    "result": "spawned_dispatch_reconcile_rejected",
                    "reason": "spawn_baseline_unverified_liveness_unverified",
                    "proposal_id": args.proposal_id,
                    "readback": readback,
                },
                2,
            )
        terminal_readback = readback

    if (
        isinstance(dispatch_record, dict)
        and dispatch_record.get("status") == "spawned_base_mismatch"
        and args.release_if_terminal
    ):
        termination = dispatch_record.get("spawn_session_termination")
        stored_readback = termination.get("readback") if isinstance(termination, dict) else None
        stored_absent = (
            isinstance(termination, dict)
            and termination.get("result") == "terminated"
            and isinstance(stored_readback, dict)
            and stored_readback.get("result") == "absent"
        )
        if not stored_absent:
            spawn_session_id = dispatch_record.get("spawn_session_id")
            if not isinstance(spawn_session_id, str) or not spawn_session_id:
                return _emit(
                    {
                        "result": "spawned_dispatch_reconcile_rejected",
                        "reason": "spawn_baseline_mismatch_missing_session_id",
                        "proposal_id": args.proposal_id,
                    },
                    2,
                )
            recorded_project_id = dispatch_record.get("ao_project_id")
            project_id = recorded_project_id if isinstance(recorded_project_id, str) and recorded_project_id else None
            readback = _confirmed_absent_readback(spawn_session_id, project_id or read_contract_project_id(args.root))
            if readback.get("result") == "active":
                return _emit(
                    {
                        "result": "spawned_dispatch_reconcile_rejected",
                        "reason": "spawn_baseline_mismatch_worker_still_active",
                        "proposal_id": args.proposal_id,
                        "readback": readback,
                    },
                    2,
                )
            if readback.get("result") != "absent":
                return _emit(
                    {
                        "result": "spawned_dispatch_reconcile_rejected",
                        "reason": "spawn_baseline_mismatch_liveness_unverified",
                        "proposal_id": args.proposal_id,
                        "readback": readback,
                    },
                    2,
                )
            terminal_readback = readback

    result = writer.reconcile_spawned_dispatch(
        proposal_id=args.proposal_id,
        evidence_refs=args.evidence,
        release_if_terminal=args.release_if_terminal,
        refresh_time=args.refresh_time,
        terminal_readback=terminal_readback,
    )
    payload = dict(result)
    if payload.get("decision") == "released":
        payload["result"] = "spawned_dispatch_terminal_released"
        return _emit(payload, 0)
    if payload.get("decision") == "refreshed":
        payload["result"] = "spawned_dispatch_time_refreshed"
        return _emit(payload, 0)
    payload["result"] = "spawned_dispatch_reconcile_rejected"
    return _emit(payload, 2)


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

    if args.command == "reconcile-leases":
        return cmd_reconcile_leases(args)

    if args.command == "retire-dead-sessions":
        return cmd_retire_dead_sessions(args)

    if args.command == "review-job":
        return cmd_review_job(args)

    if args.command == "escalated-review-actuate":
        return cmd_escalated_review_actuate(args)

    if args.command == "authorize":
        return cmd_authorize(args)

    if args.command == "record-final-convergence":
        return cmd_record_final_convergence(args)

    if args.command == "reconcile-spawn-attestation":
        return cmd_reconcile_spawn_attestation(args)

    if args.command == "reconcile-spawned-dispatch":
        return cmd_reconcile_spawned_dispatch(args)

    if args.command == "pause":
        return cmd_pause(args)

    if args.command == "resume":
        return cmd_resume(args)

    if args.command == "dispatch-stall-check":
        return cmd_dispatch_stall_check(args)

    if args.command == "preflight":
        status_output = args.status_file.read_text(encoding="utf-8")
        result = check_governance_dirty(status_output, set(args.recognized))
        sys.stdout.write(json.dumps(result, default=lambda value: value.__dict__, ensure_ascii=False) + "\n")
        return 0 if result.ok else 2

    if args.command == "repair-todo":
        current = args.todo_file.read_text(encoding="utf-8")
        try:
            repaired = repair_compact_current_state(
                current,
                current_phase=args.current_phase,
                next_locked_action=args.next_locked_action,
                review_gate_state=args.review_gate_state,
                latest_session_log_anchor=args.latest_session_log_anchor,
            )
        except ValueError as exc:
            sys.stdout.write(
                json.dumps(
                    {
                        "ok": False,
                        "result": "todo_mirror_repair_failed",
                        "reason": str(exc),
                        "todo_file": str(args.todo_file),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            return 3
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
            # This path is the sole sanctioned resolver for the spawned_review_timeout_due obligation it
            # just evaluated, yet it must clear that same preflight first. Pass a fingerprint of the exact
            # obligation being resolved so preflight does not self-block on it (the 070c412 circular
            # block) while every other obligation and guard still fails closed. The fingerprint pins the
            # stalled source proposal_id and the base revision the watchdog evaluated against, so a
            # stale/replayed command whose base no longer matches live state is not exempted.
            resolving_review_timeout: dict[str, object] = {
                "proposal_id": _dispatched_proposal_id_from_observation(observation),
                "target_id": decision.target_id,
                "review_scope": decision.review_scope,
                "base_state_revision": decision.proposal.get("base_state_revision"),
            }
            preflight = preflight_reconcile(
                root=args.root,
                writer=writer,
                ledger_path=ledger_path,
                resolving_review_timeout=resolving_review_timeout,
            )
            if preflight is not None:
                preflight_payload, preflight_exit = preflight
                payload["state_write"] = False
                payload["state_writer_preflight"] = preflight_payload
                sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
                return preflight_exit
            # Orchestrator-CLI-proof gate (slice f, matches LIVE cli.py:3668): the EXTERNAL caller of
            # `watchdog --apply-timeout-blocker` must be a proven orchestrator. Only AFTER that gate passes
            # does the wrapper below temporarily elevate to caller_type=watchdog for the apply — a
            # non-orchestrator cannot mint a watchdog timeout receipt by invoking this subcommand directly.
            proof_issue = _orchestrator_cli_proof_issue(
                writer=writer,
                proposal_id=str(decision.proposal.get("proposal_id") or ""),
            )
            if proof_issue is not None:
                payload["state_write"] = False
                payload["state_writer_preflight"] = proof_issue
                sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
                return 3
            # The watchdog timeout blocker is a review receipt whose authorized actor is
            # caller_type=watchdog (writer._expected_review_caller_type). Assert that internal caller only
            # after the preflight guard above has passed; a non-watchdog caller cannot mint a watchdog
            # receipt by invoking this subcommand directly. Mirrors the escalated_review_actuator wrapper.
            previous_caller = os.environ.get(CALLER_TYPE_ENV)
            os.environ[CALLER_TYPE_ENV] = WATCHDOG_CALLER_TYPE
            try:
                apply_decision = writer.apply(StateTransitionProposal(**decision.proposal))
            finally:
                if previous_caller is None:
                    os.environ.pop(CALLER_TYPE_ENV, None)
                else:
                    os.environ[CALLER_TYPE_ENV] = previous_caller
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


def _evaluate_continuation_with_todo_mirror_context(
    *,
    root: Path,
    ledger_path: Path,
    proposal_id: str,
    decision: StateTransitionDecision,
    ao_project_id: str | None,
) -> tuple[object, dict[str, str] | None]:
    """Evaluate continuation, self-healing a stale TODO mirror against canonical state.

    The first evaluation may report ``stale_compact_current_state`` when the TODO mirror's
    Current Execution State has lagged the authoritative state-writer revision. Rather than
    dispatch against the stale mirror (or block the loop), we rebuild a canonical current-state
    override from the accepted proposal + decision and re-evaluate WITH it, so a lagging mirror
    does not stall unattended operation. Returns ``(result, override)`` where ``override`` is
    non-None only when the repair produced a dispatchable candidate, so the caller can thread it
    into ``continue_after_apply``.
    """
    result = evaluate_continuation(root=root, decision=decision, ao_project_id=ao_project_id)
    if result.reason != "stale_compact_current_state":
        return result, None

    current_revision = _stale_todo_revision(result.blockers or [])
    proposal = _scan_ledger_for_proposal(ledger_path, proposal_id)
    if current_revision is None or proposal is None:
        return result, None
    context = _state_writer_current_state_context(
        proposal=proposal,
        decision=decision,
        stale_todo_revision=current_revision,
        master_plan_file=canonical_master_plan_file(root),
    )
    repaired_result = evaluate_continuation(
        root=root,
        decision=decision,
        ao_project_id=ao_project_id,
        current_state_override=context,
    )
    return repaired_result, context if repaired_result.decision == "candidate" else None


def _stale_todo_revision(blockers: list[str]) -> int | None:
    for blocker in blockers:
        match = re.fullmatch(r"todo_current_state_rev:(\d+)", blocker)
        if match:
            return int(match.group(1))
    return None


def _state_writer_current_state_context(
    *,
    proposal: dict[str, object],
    decision: StateTransitionDecision,
    stale_todo_revision: int,
    master_plan_file: str,
) -> dict[str, str]:
    target_id = _text_or_unknown(proposal.get("target_id"), "target")
    target_kind = _text_or_unknown(proposal.get("target_kind"), "target")
    new_state = decision.new_state or _text_or_unknown(proposal.get("requested_state"), "accepted")
    revision = decision.state_revision
    next_action = decision.next_required_action or "none"
    proposal_id = decision.proposal_id
    evidence_refs = proposal.get("evidence_refs")
    evidence_anchor = (
        next((ref for ref in evidence_refs if isinstance(ref, str) and ref), None)
        if isinstance(evidence_refs, list)
        else None
    )
    if next_action == "dispatch_next_slice_plan_mode":
        next_locked_action = (
            f"{next_action} — select the next unlocked Master Plan/TODO slice after {target_id}; "
            "compact-repair TODO Current Execution State if it still shows the older mirror"
        )
    else:
        next_locked_action = (
            f"{next_action} — execute the current state-writer obligation for {target_id}; "
            "compact-repair TODO Current Execution State if it still shows the older mirror"
        )
    return {
        "current_phase": (
            f"{target_id} {new_state} (state-writer rev{revision}; "
            f"TODO mirror lagged rev{stale_todo_revision})"
        ),
        "next_locked_action": next_locked_action,
        "review_gate_state": (
            f"state-writer proposal {proposal_id} is the current {target_kind} obligation; "
            "TODO mirror is non-authoritative until compact-repaired"
        ),
        "latest_session_log_anchor": evidence_anchor or f"state-writer:{proposal_id}",
        "todo_mirror_repair_note": (
            f"Do not modify {master_plan_file}. "
            f"TODO Current Execution State lagged state-writer rev{revision} "
            f"(observed rev{stale_todo_revision}); use this state-writer context for dispatch, "
            "then record compact TODO/session-log sync through the normal worker evidence path."
        ),
    }


def _text_or_unknown(value: object, fallback: str) -> str:
    if isinstance(value, str) and value:
        return value
    return fallback


if __name__ == "__main__":
    raise SystemExit(main())
