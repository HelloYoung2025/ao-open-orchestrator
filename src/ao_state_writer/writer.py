from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar
import hashlib
import json
import os
import time
import uuid

from .compat import (
    CALLER_TYPE_ENV,
    SESSION_ID_ENV,
    UnsupportedStateSchemaVersion,
    empty_state,
    normalize_state,
    read_contract_active_root,
    read_contract_project_id,
    read_contract_string,
)


ACCEPTED_ADVISORY_VERDICTS = {"pass", "pass_with_nits", "advisory"}
# Exact, narrow aliases for non-canonical "pass-with-a-note" tokens a reviewer may emit. They are
# normalized to the canonical token BEFORE validation/routing so closure routing fires. Without this,
# a non-canonical verdict matches neither the advisory branches nor the blocker branch, so the
# obligation records next_required_action=None and dispatch cannot route a null action (the
# orchestrator freezes). Keep this map exact and minimal: never alias a token that could carry
# blocker semantics.
_REVIEW_VERDICT_ALIASES = {"pass_with_advisory": "advisory"}
REVIEW_SCOPE_ACTORS = {
    "codex_cc": "codex_cc",
    "gpt_pro": "gpt_pro",
}
REVIEW_SCOPE_CALLER_TYPES = {
    "codex_cc": "codex_cc",
    "gpt_pro": "gpt_pro_review_actuator",
}
WATCHDOG_CALLER_TYPE = "watchdog"
CODEX_CC_MODEL = "gpt-5.5"
CODEX_CC_REASONING_EFFORT = "xhigh"
CODEX_CC_TIMEOUT_BLOCKER_CODE = "codex_cc_review_timeout"
GPT_PRO_WATCHDOG_TIMEOUT_BLOCKER_CODES = {
    "gpt_pro_desktop_uncertain",
    "gpt_pro_review_hard_timeout",
}
GPT_PRO_ACTUATOR_FAILURE_BLOCKER_CODE = "gpt_pro_actuator_failure"
# Bounded auto-repair: at most this many repair dispatches per (target, blocker_code) before the
# next blocker escalates to a human instead of looping repair <-> review indefinitely.
MAX_REPAIR_ATTEMPTS = 2
# Per-target ceiling across ALL blocker_codes, so an agent varying the blocker_code cannot dodge escalation.
MAX_REPAIR_ATTEMPTS_TOTAL = 4
# Bounded backoff for the single-flight state lock so brief contention retries instead of failing hard.
LOCK_TIMEOUT_SECONDS = 10.0
LOCK_INITIAL_BACKOFF_SECONDS = 0.05
LOCK_MAX_BACKOFF_SECONDS = 0.5
# A held lock older than this whose holder cannot be proven alive is treated as
# orphaned (the holder was SIGKILLed mid-critical-section) and reclaimed, so a hard
# kill can never deadlock the state writer. Normal holds are sub-second; 30s is
# comfortably larger than any legitimate hold. (Mirror of STALE_PENDING_DISPATCH_SECONDS.)
STALE_LOCK_SECONDS = 30.0
# A "pending" dispatch claim older than this is treated as an orphaned lease (the
# claimer was killed between claim_dispatch and confirm/release) and may be reclaimed,
# so a hard SIGKILL in that window can never deadlock a slice forever. Comfortably
# larger than the ~120s `ao spawn` timeout.
STALE_PENDING_DISPATCH_SECONDS = 600.0


@dataclass(frozen=True)
class StateTransitionProposal:
    proposal_id: str
    target_kind: str
    target_id: str
    base_state_revision: int
    requested_state: str
    actor_role: str
    evidence_refs: list[str] | None = None
    summary: str = ""
    review_scope: str = "none"
    verdict: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    review_mode: str | None = None
    blocker_code: str | None = None
    blocker_detail: str | None = None
    package_path: str | None = None
    prompt_path: str | None = None
    package_sha256: str | None = None
    non_retryable: bool = False
    codex_cc_transcript_sha256: str | None = None
    external_review_receipt_sha256: str | None = None
    external_review_submission_nonce: str | None = None
    external_review_artifact_ref: str | None = None
    external_review_gate_proposal_id: str | None = None


@dataclass(frozen=True)
class StateTransitionDecision:
    decision: str
    proposal_id: str
    reason: str
    state_revision: int
    new_state: str | None = None
    next_required_action: str | None = None
    blocker_code: str | None = None
    replayed: bool = False


class StateWriter:
    """Single authority for AO clone canonical state-transition decisions."""

    _STATE_ROOT_PARTS: ClassVar[tuple[str, str, str]] = (".omx", "state", "ao-state-writer")

    def __init__(self, *, state_path: Path, ledger_path: Path):
        self.state_path = state_path
        self.ledger_path = ledger_path

    def apply(self, proposal: StateTransitionProposal) -> StateTransitionDecision:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with self._single_flight():
            try:
                state = self._read_state()
            except UnsupportedStateSchemaVersion:
                return StateTransitionDecision(
                    decision="rejected",
                    proposal_id=proposal.proposal_id,
                    reason="unsupported_state_schema_version",
                    state_revision=proposal.base_state_revision,
                )
            replay = state["proposal_results"].get(proposal.proposal_id)
            if replay is not None:
                return StateTransitionDecision(**replay, replayed=True)

            # Normalize an exact non-canonical review-verdict alias (e.g. pass_with_advisory)
            # to its canonical token BEFORE validation and routing, so closure routing and the
            # caller/proof gates all see the canonical verdict. The proposal is frozen, so rebind
            # to a replaced copy.
            if (
                proposal.review_scope in REVIEW_SCOPE_ACTORS
                and proposal.verdict in _REVIEW_VERDICT_ALIASES
            ):
                proposal = replace(
                    proposal, verdict=_REVIEW_VERDICT_ALIASES[proposal.verdict]
                )

            rejection = self._rejection(proposal, state)
            if rejection is not None:
                return rejection

            target = state["targets"].setdefault(
                proposal.target_id,
                {"kind": proposal.target_kind, "state": "planned", "review_receipts": []},
            )
            decision = self._accept(proposal, state, target)
            target["state"] = decision.new_state
            target["kind"] = proposal.target_kind
            target.setdefault("evidence_refs", [])
            target["evidence_refs"].extend(proposal.evidence_refs or [])
            target["evidence_refs"] = sorted(set(target["evidence_refs"]))
            if proposal.review_scope != "none" or proposal.verdict is not None:
                target.setdefault("review_receipts", []).append(self._review_receipt(proposal))
            if proposal.requested_state == "gpt_pro_review_pending" and (
                proposal.package_path or proposal.prompt_path or proposal.package_sha256
            ):
                target["gpt_pro_package"] = {
                    "package_path": proposal.package_path,
                    "prompt_path": proposal.prompt_path,
                    "package_sha256": proposal.package_sha256,
                }
                target["active_gpt_pro_review_gate_proposal_id"] = proposal.proposal_id
                state.setdefault("gpt_pro_review_gates", {})[proposal.proposal_id] = {
                    "proposal_id": proposal.proposal_id,
                    "target_id": proposal.target_id,
                    "target_kind": proposal.target_kind,
                    "package_path": proposal.package_path,
                    "prompt_path": proposal.prompt_path,
                    "package_sha256": proposal.package_sha256,
                }

            state["state_revision"] += 1
            stored = asdict(decision)
            stored["state_revision"] = state["state_revision"]
            stored.pop("replayed", None)
            state["proposal_results"][proposal.proposal_id] = stored
            self._write_state(state)
            self._append_ledger(proposal, StateTransitionDecision(**stored))
            return StateTransitionDecision(**stored)

    def _rejection(
        self, proposal: StateTransitionProposal, state: dict[str, Any]
    ) -> StateTransitionDecision | None:
        if proposal.base_state_revision != state["state_revision"]:
            return self._reject(proposal, state, "stale_revision")
        expected_actor = REVIEW_SCOPE_ACTORS.get(proposal.review_scope)
        if expected_actor is not None and proposal.actor_role != expected_actor:
            return self._reject(proposal, state, "invalid_review_actor")
        if not proposal.evidence_refs:
            return self._reject(proposal, state, "missing_evidence")
        if proposal.review_scope == "codex_cc" and proposal.blocker_code != CODEX_CC_TIMEOUT_BLOCKER_CODE:
            if proposal.model != CODEX_CC_MODEL:
                return self._reject(proposal, state, "invalid_codex_cc_model")
            if proposal.reasoning_effort != CODEX_CC_REASONING_EFFORT:
                return self._reject(proposal, state, "invalid_codex_cc_reasoning_effort")
        if (
            proposal.review_scope == "codex_cc"
            and proposal.verdict in ACCEPTED_ADVISORY_VERDICTS
            and not proposal.codex_cc_transcript_sha256
        ):
            return self._reject(proposal, state, "missing_codex_cc_transcript")
        if proposal.review_scope == "codex_cc" or proposal.review_mode == "watchdog_timeout":
            caller_rejection = self._review_caller_rejection(proposal)
            if caller_rejection is not None:
                return self._reject(proposal, state, caller_rejection)
        if proposal.review_scope == "gpt_pro" and proposal.verdict in ACCEPTED_ADVISORY_VERDICTS.union(
            {"blocker"}
        ):
            if self._is_gpt_pro_watchdog_timeout(proposal):
                pass
            elif self._is_gpt_pro_actuator_failure(proposal):
                if not (proposal.package_sha256 and proposal.external_review_gate_proposal_id):
                    return self._reject(proposal, state, "missing_gpt_pro_actuator_failure_proof")
                caller_rejection = self._review_caller_rejection(proposal)
                if caller_rejection is not None:
                    return self._reject(proposal, state, caller_rejection)
                authorization_rejection = self._gpt_pro_authorization_rejection(proposal, state)
                if authorization_rejection is not None:
                    return self._reject(proposal, state, authorization_rejection)
            elif not (
                proposal.package_sha256
                and proposal.external_review_receipt_sha256
                and proposal.external_review_submission_nonce
                and proposal.external_review_artifact_ref
                and proposal.external_review_gate_proposal_id
            ):
                return self._reject(proposal, state, "missing_gpt_pro_receipt_proof")
            else:
                caller_rejection = self._review_caller_rejection(proposal)
                if caller_rejection is not None:
                    return self._reject(proposal, state, caller_rejection)
                authorization_rejection = self._gpt_pro_authorization_rejection(proposal, state)
                if authorization_rejection is not None:
                    return self._reject(proposal, state, authorization_rejection)
                artifact_rejection = self._external_review_artifact_rejection(proposal)
                if artifact_rejection is not None:
                    return self._reject(proposal, state, artifact_rejection)
        if (
            proposal.target_kind == "small_chapter"
            and proposal.requested_state == "closed"
            and not self._has_codex_cc_receipt(state, proposal.target_id)
        ):
            return self._reject(proposal, state, "missing_codex_cc_receipt")
        if (
            proposal.target_kind == "major_chapter"
            and proposal.requested_state == "closed"
            and not self._has_gpt_pro_receipt(state, proposal.target_id)
        ):
            return self._reject(proposal, state, "missing_gpt_pro_receipt")
        if (
            proposal.target_kind == "major_chapter"
            and proposal.requested_state == "gpt_pro_review_pending"
            and not (proposal.package_path and proposal.prompt_path and proposal.package_sha256)
        ):
            return self._reject(proposal, state, "missing_gpt_pro_package_receipt")
        return None

    def _accept(
        self,
        proposal: StateTransitionProposal,
        state: dict[str, Any],
        target: dict[str, Any],
    ) -> StateTransitionDecision:
        requested = proposal.requested_state
        next_required_action: str | None = None
        blocker_code = proposal.blocker_code

        if proposal.verdict == "blocker":
            attempts = target.setdefault("repair_attempts", {})
            code = proposal.blocker_code or "unspecified"
            attempts[code] = int(attempts.get(code, 0)) + 1
            target["repair_attempts_total"] = int(target.get("repair_attempts_total", 0)) + 1
            if (
                proposal.non_retryable
                or attempts[code] > MAX_REPAIR_ATTEMPTS
                or target["repair_attempts_total"] > MAX_REPAIR_ATTEMPTS_TOTAL
            ):
                requested = "repair_attempts_exhausted"
                next_required_action = "repair_attempts_exhausted"
            else:
                requested = "review_blocked"
                next_required_action = "repair_active"
        elif proposal.review_scope == "codex_cc" and proposal.verdict in ACCEPTED_ADVISORY_VERDICTS:
            requested = "closure_candidate"
            next_required_action = "state_writer_closure"
        elif proposal.review_scope == "gpt_pro" and proposal.verdict in ACCEPTED_ADVISORY_VERDICTS:
            requested = "closure_candidate"
            next_required_action = "major_closure_candidate"
        elif requested == "closed" and proposal.target_kind in ("small_chapter", "major_chapter"):
            next_required_action = "dispatch_next_slice_plan_mode"
        elif requested == "gpt_pro_review_pending":
            next_required_action = "gpt_pro_desktop_review"
        elif requested == "evidence_pending":
            next_required_action = "codex_cc_review"

        return StateTransitionDecision(
            decision="accepted",
            proposal_id=proposal.proposal_id,
            reason="accepted",
            state_revision=state["state_revision"],
            new_state=requested,
            next_required_action=next_required_action,
            blocker_code=blocker_code,
        )

    def _reject(
        self, proposal: StateTransitionProposal, state: dict[str, Any], reason: str
    ) -> StateTransitionDecision:
        return StateTransitionDecision(
            decision="rejected",
            proposal_id=proposal.proposal_id,
            reason=reason,
            state_revision=state["state_revision"],
        )

    def _review_caller_rejection(self, proposal: StateTransitionProposal) -> str | None:
        expected = self._expected_review_caller_type(proposal)
        if expected is None:
            return None
        if os.environ.get(CALLER_TYPE_ENV) != expected:
            return "unauthorized_review_receipt_actor"
        return None

    @staticmethod
    def _expected_review_caller_type(proposal: StateTransitionProposal) -> str | None:
        if proposal.verdict == "blocker" and proposal.review_mode == "watchdog_timeout":
            if (
                proposal.review_scope == "codex_cc"
                and proposal.blocker_code == CODEX_CC_TIMEOUT_BLOCKER_CODE
            ) or (
                proposal.review_scope == "gpt_pro"
                and proposal.blocker_code in GPT_PRO_WATCHDOG_TIMEOUT_BLOCKER_CODES
            ):
                return WATCHDOG_CALLER_TYPE
        if proposal.review_scope == "codex_cc" and proposal.verdict in ACCEPTED_ADVISORY_VERDICTS:
            return REVIEW_SCOPE_CALLER_TYPES["codex_cc"]
        if proposal.review_scope == "gpt_pro" and proposal.verdict in ACCEPTED_ADVISORY_VERDICTS.union({"blocker"}):
            return REVIEW_SCOPE_CALLER_TYPES["gpt_pro"]
        return None

    def _has_codex_cc_receipt(self, state: dict[str, Any], target_id: str) -> bool:
        target = state["targets"].get(target_id, {})
        return self._latest_review_receipt_is_pass_family(target, "codex_cc")

    def _has_gpt_pro_receipt(self, state: dict[str, Any], target_id: str) -> bool:
        target = state["targets"].get(target_id, {})
        return self._latest_review_receipt_is_pass_family(target, "gpt_pro")

    @staticmethod
    def _latest_review_receipt_is_pass_family(target: dict[str, Any], scope: str) -> bool:
        # review_receipts is append-only through apply(); the current close gate
        # deliberately accepts only the newest scoped receipt for a target that is
        # still in the closure_candidate state produced by that receipt. New
        # receipts carry the proposal base_state_revision, which is monotonic per
        # accepted apply; all-legacy receipts fall back to append order, while
        # malformed or post-revision legacy entries fail closed.
        if target.get("state") != "closure_candidate":
            return False
        receipts = target.get("review_receipts", [])
        if not isinstance(receipts, list) or any(not isinstance(receipt, dict) for receipt in receipts):
            return False
        scoped_receipts = [receipt for receipt in receipts if receipt.get("scope") == scope]
        if not scoped_receipts:
            return False
        saw_revisioned_receipt = False
        for receipt in scoped_receipts:
            if isinstance(receipt.get("review_state_revision"), int):
                saw_revisioned_receipt = True
            elif saw_revisioned_receipt:
                return False
        latest = max(
            enumerate(scoped_receipts),
            key=lambda item: (
                item[1].get("review_state_revision")
                if isinstance(item[1].get("review_state_revision"), int)
                else -1,
                item[0],
            ),
        )[1]
        if latest.get("verdict") not in ACCEPTED_ADVISORY_VERDICTS:
            return False
        if scope == "gpt_pro":
            package = target.get("gpt_pro_package")
            if not isinstance(package, dict) or not package.get("package_sha256"):
                return False
            if latest.get("package_sha256") != package.get("package_sha256"):
                return False
        return True

    @staticmethod
    def _is_gpt_pro_watchdog_timeout(proposal: StateTransitionProposal) -> bool:
        return (
            proposal.review_scope == "gpt_pro"
            and proposal.review_mode == "watchdog_timeout"
            and proposal.blocker_code in GPT_PRO_WATCHDOG_TIMEOUT_BLOCKER_CODES
        )

    @staticmethod
    def _is_gpt_pro_actuator_failure(proposal: StateTransitionProposal) -> bool:
        return (
            proposal.review_scope == "gpt_pro"
            and proposal.verdict == "blocker"
            and proposal.review_mode == "actuator_failure"
            and proposal.blocker_code == GPT_PRO_ACTUATOR_FAILURE_BLOCKER_CODE
        )

    def _gpt_pro_authorization_rejection(
        self,
        proposal: StateTransitionProposal,
        state: dict[str, Any],
    ) -> str | None:
        gate_id = proposal.external_review_gate_proposal_id
        if not gate_id:
            return "missing_gpt_pro_review_authorization"
        grant = self._fresh_authorization_record(state, gate_id)
        if grant is None:
            return "missing_gpt_pro_review_authorization"
        gate = grant.get("gpt_pro_review_gate")
        if not isinstance(gate, dict) or not gate.get("package_sha256"):
            return "missing_gpt_pro_package"
        if gate.get("target_id") != proposal.target_id:
            return "gpt_pro_review_authorization_target_mismatch"
        current_gate = self._current_gpt_pro_gate_id_for_target(state, proposal.target_id)
        if current_gate is not None and current_gate != gate_id:
            return "stale_gpt_pro_review_gate"
        if gate.get("package_sha256") != proposal.package_sha256:
            return "gpt_pro_package_sha_mismatch"
        expected_nonce = gate.get("external_review_submission_nonce")
        if (
            isinstance(expected_nonce, str)
            and expected_nonce
            and not self._is_gpt_pro_watchdog_timeout(proposal)
            and not self._is_gpt_pro_actuator_failure(proposal)
        ):
            if proposal.external_review_submission_nonce != expected_nonce:
                return "external_review_submission_nonce_mismatch"
        return None

    def _external_review_artifact_rejection(self, proposal: StateTransitionProposal) -> str | None:
        path = self._external_review_artifact_path(proposal.external_review_artifact_ref)
        if path is None:
            return "invalid_external_review_artifact_ref"
        if not path.exists() or not path.is_file():
            return "missing_external_review_artifact"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != proposal.external_review_receipt_sha256:
            return "external_review_artifact_sha_mismatch"
        return None

    def _external_review_artifact_path(self, artifact_ref: str | None) -> Path | None:
        if not isinstance(artifact_ref, str) or not artifact_ref.startswith("artifact:reports/"):
            return None
        relative = artifact_ref[len("artifact:") :]
        rel_path = Path(relative)
        if rel_path.is_absolute() or ".." in rel_path.parts or rel_path.parts[:1] != ("reports",):
            return None
        root = self._repo_root()
        full_path = (root / rel_path).resolve()
        try:
            full_path.relative_to(root.resolve())
        except ValueError:
            return None
        return full_path

    def _repo_root(self) -> Path:
        parts = self.state_path.parts
        for index in range(0, len(parts) - len(self._STATE_ROOT_PARTS) + 1):
            if parts[index : index + len(self._STATE_ROOT_PARTS)] == self._STATE_ROOT_PARTS:
                return Path(*parts[:index])
        return self.state_path.parent.parent.parent.parent

    def _review_receipt(self, proposal: StateTransitionProposal) -> dict[str, Any]:
        return {
            "scope": proposal.review_scope,
            "actor_role": proposal.actor_role,
            "caller_type": os.environ.get(CALLER_TYPE_ENV),
            "verdict": proposal.verdict,
            "model": proposal.model,
            "reasoning_effort": proposal.reasoning_effort,
            "review_mode": proposal.review_mode,
            "review_state_revision": proposal.base_state_revision,
            "blocker_code": proposal.blocker_code,
            "blocker_detail": proposal.blocker_detail,
            "codex_cc_transcript_sha256": proposal.codex_cc_transcript_sha256,
            "package_sha256": proposal.package_sha256,
            "external_review_receipt_sha256": proposal.external_review_receipt_sha256,
            "external_review_submission_nonce": proposal.external_review_submission_nonce,
            "external_review_artifact_ref": proposal.external_review_artifact_ref,
            "external_review_gate_proposal_id": proposal.external_review_gate_proposal_id,
            "evidence_refs": proposal.evidence_refs or [],
        }

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return empty_state()
        last_error: json.JSONDecodeError | None = None
        for attempt in range(3):
            try:
                return normalize_state(json.loads(self.state_path.read_text(encoding="utf-8")))
            except json.JSONDecodeError as exc:
                last_error = exc
                time.sleep(0.02 * (attempt + 1))
        assert last_error is not None
        raise last_error

    def _write_state(self, state: dict[str, Any]) -> None:
        state = normalize_state(state)
        state["schema_version"] = 1
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
        tmp_path = self.state_path.with_name(f".{self.state_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.state_path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def _append_ledger(
        self, proposal: StateTransitionProposal, decision: StateTransitionDecision
    ) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "id": str(uuid.uuid4()),
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "proposal": asdict(proposal),
            "decision": asdict(decision),
        }
        with self.ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    def _single_flight(self):
        return _FileLock(self.state_path.with_suffix(self.state_path.suffix + ".lock"))

    # ---------------------------------------------------------------------------
    # Consume-once dispatch ledger API
    # state["dispatched_proposals"] maps proposal_id -> {"status": "pending"|"spawned", "time": <iso8601>}
    # Spawned records may also carry spawn_session_id as audit proof parsed from AO's SESSION=<id>
    # stdout attestation; root resolution still comes from live AO session metadata, not this ledger.
    # ---------------------------------------------------------------------------

    def is_dispatched(self, proposal_id: str) -> bool:
        """Return True iff proposal_id has already been successfully spawned."""
        state = self._read_state()
        state.setdefault("dispatched_proposals", {})
        record = state["dispatched_proposals"].get(proposal_id)
        return record is not None and record.get("status") == "spawned"

    def claim_dispatch(self, proposal_id: str) -> bool:
        """Atomically claim dispatch for proposal_id.

        Returns True (writing a fresh "pending" lease) when no live claim exists.
        Returns False when the proposal is already "spawned" (consumed) or a "pending"
        lease is still live (a peer is mid-dispatch). A "pending" lease older than
        STALE_PENDING_DISPATCH_SECONDS — or one with an unparseable timestamp — is an
        orphaned claim (the claimer was SIGKILLed between claim and confirm/release) and
        is reclaimed here, so that window can never deadlock a slice forever.
        """
        with self._single_flight():
            state = self._read_state()
            state.setdefault("dispatched_proposals", {})
            record = state["dispatched_proposals"].get(proposal_id)
            if record is not None:
                if record.get("status") == "spawned":
                    return False
                if record.get("status") == "pending" and not self._pending_lease_expired(record):
                    return False
                # A stale/malformed pending lease falls through and is reclaimed below.
            state["dispatched_proposals"][proposal_id] = {
                "status": "pending",
                "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            self._write_state(state)
            return True

    @staticmethod
    def _pending_lease_expired(record: dict[str, Any]) -> bool:
        """True if a 'pending' dispatch lease is older than STALE_PENDING_DISPATCH_SECONDS
        (or carries an unparseable timestamp) — i.e. an orphaned claim safe to reclaim."""
        raw = record.get("time")
        if not isinstance(raw, str):
            return True
        try:
            claimed = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            return True
        return (datetime.now(timezone.utc) - claimed).total_seconds() > STALE_PENDING_DISPATCH_SECONDS

    def confirm_dispatch(
        self,
        proposal_id: str,
        authorized_by: str = "orchestrator_policy",
        spawn_session_id: str | None = None,
        spawn_attestation: str | None = None,
    ) -> None:
        """Mark a previously claimed proposal_id as successfully spawned.

        Creates the record if missing (e.g. state file was replaced), and updates
        its time to now.  ``authorized_by`` is recorded in the spawned record so
        callers can distinguish policy-pre-authorized dispatches from those that
        required a live orchestrator authorization.
        """
        with self._single_flight():
            state = self._read_state()
            state.setdefault("dispatched_proposals", {})
            record = {
                "status": "spawned",
                "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "authorized_by": authorized_by,
            }
            if spawn_session_id:
                record["spawn_session_id"] = spawn_session_id
                record["spawn_attestation"] = "session"
            elif spawn_attestation:
                record["spawn_attestation"] = spawn_attestation
            state["dispatched_proposals"][proposal_id] = record
            self._write_state(state)

    # ---------------------------------------------------------------------------
    # Orchestrator-authorization API
    # state["orchestrator_authorizations"] maps proposal_id ->
    #   {"actor_role": str, "evidence_refs": list[str], "scope": str|None, "time": <iso8601>}
    # ---------------------------------------------------------------------------

    def record_authorization(
        self,
        *,
        proposal_id: str,
        evidence_refs: list[str],
        scope: str | None = None,
    ) -> dict:
        """Record a live orchestrator authorization for an already-accepted proposal.

        The caller identity is taken from the AO-injected ``AO_CALLER_TYPE`` env var
        (set per session by the AO runtime), NOT from a self-asserted argument: only a
        real orchestrator session (``AO_CALLER_TYPE == "orchestrator"``) may authorize,
        and a missing/other value fails closed (``non_orchestrator_caller``). This binds
        the authorization to the orchestrator so a worker cannot mint one. Evidence and
        proposal acceptance are also validated. Returns a rejection dict (without
        writing) on any failure, or a ``{"decision": "recorded", ...}`` dict on success.

        Note: whether the action is "gated" is intentionally NOT checked here to
        avoid a circular import with continuation.py — the CLI handles that guard.
        """
        caller_type = os.environ.get(CALLER_TYPE_ENV)
        with self._single_flight():
            state = self._read_state()
            state.setdefault("orchestrator_authorizations", {})
            state.setdefault("proposal_results", {})

            if caller_type != "orchestrator":
                return {
                    "decision": "rejected",
                    "reason": "non_orchestrator_caller",
                    "proposal_id": proposal_id,
                    "caller_type": caller_type,
                }
            orchestrator_proof = self._orchestrator_proof_metadata()
            if orchestrator_proof.get("reason"):
                return {
                    "decision": "rejected",
                    "reason": orchestrator_proof["reason"],
                    "proposal_id": proposal_id,
                    "caller_type": caller_type,
                }
            if not evidence_refs:
                return {
                    "decision": "rejected",
                    "reason": "missing_authorization_evidence",
                    "proposal_id": proposal_id,
                }

            stored = state["proposal_results"].get(proposal_id)
            if stored is None or stored.get("decision") != "accepted":
                return {
                    "decision": "rejected",
                    "reason": "unknown_or_unaccepted_proposal",
                    "proposal_id": proposal_id,
                }

            record: dict = {
                "actor_role": "orchestrator",
                "caller_type": caller_type,
                "session_id": orchestrator_proof.get("session_id"),
                "project_id": orchestrator_proof.get("project_id"),
                "active_root": orchestrator_proof.get("active_root"),
                "evidence_refs": list(evidence_refs),
                "scope": scope,
                "state_revision": stored.get("state_revision"),
                "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            if stored.get("next_required_action") == "gpt_pro_desktop_review":
                gate = self._gpt_pro_review_gate_metadata(state, proposal_id)
                if not isinstance(gate, dict) or not gate.get("package_sha256"):
                    return {
                        "decision": "rejected",
                        "reason": "missing_gpt_pro_package",
                        "proposal_id": proposal_id,
                    }
                current_gate = self._current_gpt_pro_gate_id_for_target(state, gate.get("target_id"))
                if current_gate is not None and current_gate != proposal_id:
                    return {
                        "decision": "rejected",
                        "reason": "stale_gpt_pro_review_gate",
                        "proposal_id": proposal_id,
                    }
                gate.setdefault("external_review_submission_nonce", uuid.uuid4().hex)
                record["gpt_pro_review_gate"] = dict(gate)
                state.setdefault("gpt_pro_review_gates", {})[proposal_id] = dict(gate)
                target_id = gate.get("target_id")
                target = state.get("targets", {}).get(target_id) if isinstance(target_id, str) else None
                if isinstance(target, dict):
                    target["active_gpt_pro_review_gate_proposal_id"] = proposal_id
                    target["gpt_pro_package"] = {
                        "package_path": gate.get("package_path"),
                        "prompt_path": gate.get("prompt_path"),
                        "package_sha256": gate.get("package_sha256"),
                    }
            state["orchestrator_authorizations"][proposal_id] = record
            self._write_state(state)
            return {"decision": "recorded", "proposal_id": proposal_id, "authorization": record}

    def _gpt_pro_review_gate_metadata(
        self,
        state: dict[str, Any],
        proposal_id: str,
    ) -> dict[str, Any] | None:
        gate = state.get("gpt_pro_review_gates", {}).get(proposal_id)
        if isinstance(gate, dict) and gate.get("package_sha256"):
            return dict(gate)

        proposal = self._ledger_proposal(proposal_id)
        if not isinstance(proposal, dict):
            return None
        target_id = proposal.get("target_id")
        target_kind = proposal.get("target_kind")
        if not isinstance(target_id, str) or not isinstance(target_kind, str):
            return None
        target = state.get("targets", {}).get(target_id)
        if not isinstance(target, dict) or target.get("state") != "gpt_pro_review_pending":
            return None

        package = target.get("gpt_pro_package") if isinstance(target.get("gpt_pro_package"), dict) else {}
        package_path = proposal.get("package_path") or package.get("package_path")
        prompt_path = proposal.get("prompt_path") or package.get("prompt_path")
        package_sha256 = proposal.get("package_sha256") or package.get("package_sha256")
        if not (
            isinstance(package_path, str)
            and isinstance(prompt_path, str)
            and isinstance(package_sha256, str)
            and package_sha256
        ):
            return None
        return {
            "proposal_id": proposal_id,
            "target_id": target_id,
            "target_kind": target_kind,
            "package_path": package_path,
            "prompt_path": prompt_path,
            "package_sha256": package_sha256,
            "external_review_submission_nonce": proposal.get("external_review_submission_nonce")
            or package.get("external_review_submission_nonce"),
        }

    def _current_gpt_pro_gate_id_for_target(
        self,
        state: dict[str, Any],
        target_id: object,
    ) -> str | None:
        if not isinstance(target_id, str):
            return None
        target = state.get("targets", {}).get(target_id)
        if not isinstance(target, dict) or target.get("state") != "gpt_pro_review_pending":
            return None
        proposal_results = state.get("proposal_results", {})
        if not isinstance(proposal_results, dict):
            return None
        latest: tuple[int, str] | None = None
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
            proposal = self._ledger_proposal(pid)
            if not isinstance(proposal, dict) or proposal.get("target_id") != target_id:
                continue
            if latest is None or revision > latest[0]:
                latest = (revision, pid)
        if latest is not None:
            return latest[1]
        pointer = target.get("active_gpt_pro_review_gate_proposal_id")
        return pointer if isinstance(pointer, str) else None

    def _ledger_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        if not self.ledger_path.exists():
            return None
        for line in self.ledger_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            proposal = entry.get("proposal")
            if isinstance(proposal, dict) and proposal.get("proposal_id") == proposal_id:
                return dict(proposal)
        return None

    def _orchestrator_proof_metadata(self) -> dict[str, Any]:
        session_id = os.environ.get(SESSION_ID_ENV)
        if not isinstance(session_id, str) or not session_id.strip():
            return {"reason": "missing_or_stale_orchestrator_proof"}

        root = self._repo_root().resolve()
        session_id = session_id.strip()
        expected_session = read_contract_string(root, "continuation_policy", "orchestrator_session")
        if not expected_session or session_id != expected_session:
            return {"reason": "missing_or_stale_orchestrator_proof"}

        metadata: dict[str, Any] = {"session_id": session_id}
        active_root = read_contract_active_root(root)
        if active_root is None or active_root != root:
            return {"reason": "missing_or_stale_orchestrator_proof"}
        metadata["active_root"] = str(active_root)

        project_id = read_contract_project_id(root)
        if not project_id:
            return {"reason": "missing_or_stale_orchestrator_proof"}
        metadata["project_id"] = project_id
        return metadata

    def is_authorized(self, proposal_id: str) -> bool:
        """Return True iff a FRESH orchestrator authorization exists for proposal_id.

        Fresh requires ALL of:
          - a grant whose actor_role is "orchestrator" (a worker cannot mint one — see
            record_authorization's AO_CALLER_TYPE binding),
          - the proposal is STILL accepted in canonical state, and
          - the grant's state_revision equals the proposal's current accepted
            state_revision — i.e. the grant is bound to the exact accepted decision it
            was issued against.

        The revision binding scopes the grant to a single accepted decision: it cannot
        validate against, or outlive, a different/superseded decision, and a grant left
        behind by a tampered/corrupted state (proposal removed, or its revision changed)
        fails closed instead of silently clearing the gate. We deliberately do NOT delete
        the grant after a read ("consume by deletion"): `continue` for a gated action is
        idempotent (a daemon re-poke must keep returning a stable "authorized", not flap
        back to requires_orchestrator_authorization); the revision binding, not deletion,
        is what makes this a one-decision grant.

        Honest note on reachability: a proposal_id is accepted exactly once (apply()
        replays a duplicate proposal_id without bumping state — writer.py:84-86), so
        proposal_results[pid].state_revision is immutable and the revision check never
        diverges under normal operation. Its value is defense-in-depth: tamper/corruption
        resistance and an explicit, self-validating invariant if accept-once ever changes.
        """
        state = self._read_state()
        return self._fresh_authorization_record(state, proposal_id) is not None

    def authorization_record(self, proposal_id: str) -> dict[str, Any] | None:
        state = self._read_state()
        record = self._fresh_authorization_record(state, proposal_id)
        return dict(record) if record is not None else None

    @staticmethod
    def _fresh_authorization_record(state: dict[str, Any], proposal_id: str) -> dict[str, Any] | None:
        grant = state.get("orchestrator_authorizations", {}).get(proposal_id)
        if not isinstance(grant, dict) or grant.get("actor_role") != "orchestrator":
            return None
        accepted = state.get("proposal_results", {}).get(proposal_id)
        if not isinstance(accepted, dict) or accepted.get("decision") != "accepted":
            return None
        if grant.get("state_revision") != accepted.get("state_revision"):
            return None
        return grant

    def release_dispatch(self, proposal_id: str) -> None:
        """Release a *pending* dispatch claim so a failed/aborted dispatch can be retried.

        Only a "pending" record is removed; a "spawned" record (a confirmed successful
        dispatch) is never deleted, preserving the consume-once invariant even if
        release is called out of order.
        """
        with self._single_flight():
            state = self._read_state()
            state.setdefault("dispatched_proposals", {})
            record = state["dispatched_proposals"].get(proposal_id)
            if record is not None and record.get("status") == "pending":
                del state["dispatched_proposals"][proposal_id]
                self._write_state(state)

    def reclaim_orphaned_pending_leases(
        self, proposal_ids: list[str], *, apply: bool
    ) -> dict[str, list[str]]:
        """Delete provably-orphaned 'pending' dispatch leases for the given proposal_ids.

        A lease is reclaimed ONLY IF, re-read under the single-flight lock, it is still
        ``status == "pending"`` AND ``_pending_lease_expired`` (older than
        STALE_PENDING_DISPATCH_SECONDS, or an unparseable timestamp). The under-lock expiry
        recheck is the race guard: a lease a concurrent claim refreshed (e.g. a just-started
        external review actuator bridge) is younger than the floor and is left untouched, so a
        reclaim can never delete a live lease out from under an in-flight dispatch.

        The CALLER owns the action-class invariant: ``proposal_ids`` must already be filtered to
        ordinary fast auto-spawn continuation leases — never external review actuator/review leases
        (e.g. desktop GPT Pro review), which legitimately hold "pending" far longer than the orphan
        floor. This method enforces only the orphan-age invariant, all that can be verified from the
        lease record alone. It mirrors ``release_dispatch`` (pending-only delete) with the orphan-age
        gate added, and never deletes a ``spawned`` record.

        With ``apply=False`` this is a pure dry-run: it reports what WOULD be reclaimed and writes
        nothing.
        """
        reclaimed: list[str] = []
        skipped: list[str] = []
        with self._single_flight():
            state = self._read_state()
            dispatched = state.setdefault("dispatched_proposals", {})
            changed = False
            for proposal_id in proposal_ids:
                record = dispatched.get(proposal_id)
                if (
                    isinstance(record, dict)
                    and record.get("status") == "pending"
                    and self._pending_lease_expired(record)
                ):
                    reclaimed.append(proposal_id)
                    if apply:
                        del dispatched[proposal_id]
                        changed = True
                else:
                    skipped.append(proposal_id)
            if changed:
                self._write_state(state)
        return {"reclaimed": reclaimed, "skipped": skipped}


def _lock_is_stale(text: str) -> bool:
    """True if a held lockfile (whose body is the `pid=<pid> time=<iso>` line __enter__
    writes) is orphaned — the holder was SIGKILLed mid-critical-section — and safe to
    reclaim. STALE if EITHER: (a) the pid parses and the process is dead
    (os.kill(pid, 0) -> ProcessLookupError); or (b) the pid is unparseable/absent, or the
    timestamp is older than STALE_LOCK_SECONDS, or the timestamp is unparseable. A pid that
    is alive (no exception) or alive-but-other-user (PermissionError) is NOT stale; a
    provably-alive holder within the time fallback is NOT stale. Mirrors
    _pending_lease_expired: a ValueError on the timestamp is treated as stale."""
    pid_raw: str | None = None
    time_raw: str | None = None
    for token in text.split():
        if token.startswith("pid="):
            pid_raw = token[len("pid="):]
        elif token.startswith("time="):
            time_raw = token[len("time="):]
    # (a) pid-liveness (primary): a dead pid is conclusively stale; alive (or alive
    # other-user via PermissionError) is conclusively NOT stale.
    if pid_raw is not None:
        try:
            pid = int(pid_raw)
        except ValueError:
            pid = None
        if pid is not None:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            except PermissionError:
                return False
            else:
                return False
    # An identity-less body (no pid= AND no time=) is NOT a reclaimable orphan: it is a
    # holder mid-create, in the window between the O_EXCL create of the empty lockfile in
    # __enter__ and the pid/time write below. A genuine orphan always carries the full
    # pid= time= line, so an empty or whitespace body means the holder is LIVE -- treat it
    # as fresh and never steal it. Without this guard a concurrent acquirer reads the empty
    # body, deems it stale, and rename-steals a live just-created lock -> two holders in the
    # critical section -> lost update. (Residual: a holder SIGKILLed inside that
    # sub-microsecond window leaves an empty orphan treated as live, needing a manual remove
    # of the lockfile -- deliberately preferred over stealing live locks and silently
    # corrupting canonical state. This predicate only ever fails safe: it may decline to
    # reclaim, never wrongly reclaim a live lock.)
    if pid_raw is None and time_raw is None:
        return False
    # (b) time fallback (secondary): pid unparseable/absent but a timestamp is present ->
    # rely on the timestamp; a present-but-unparseable timestamp is treated as stale.
    if not isinstance(time_raw, str):
        return True
    try:
        held = datetime.strptime(time_raw, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - held).total_seconds() > STALE_LOCK_SECONDS


class _FileLock:
    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> "_FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        delay = LOCK_INITIAL_BACKOFF_SECONDS
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                # Before waiting, check whether the current holder was SIGKILLed mid-hold
                # (lockfile never unlinked) and reclaim it, so a hard kill cannot deadlock
                # the state writer forever. Mirror of claim_dispatch's orphaned-lease reclaim.
                try:
                    text = self.path.read_text(encoding="utf-8")
                except FileNotFoundError:
                    continue  # holder just released -> retry the create
                if _lock_is_stale(text):
                    # Reclaim race-free via atomic rename: exactly one waiter can win the
                    # rename of a given inode, so only one steals; losers get
                    # FileNotFoundError and retry. A naive unlink would let two waiters both
                    # reclaim-then-create and both believe they hold the lock.
                    steal = Path(f"{self.path}.stale.{os.getpid()}.{time.monotonic_ns()}")
                    try:
                        os.rename(self.path, steal)
                    except FileNotFoundError:
                        continue  # another waiter stole it / holder released -> retry create
                    except OSError:
                        pass  # not reclaimable this iteration -> fall through to backoff sleep
                    else:
                        try:
                            os.unlink(steal)
                        except FileNotFoundError:
                            pass
                        continue  # normal O_EXCL create now races legitimately
                if time.monotonic() >= deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, LOCK_MAX_BACKOFF_SECONDS)
        os.write(self.fd, f"pid={os.getpid()} time={time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n".encode("utf-8"))
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
