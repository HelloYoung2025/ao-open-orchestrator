from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json
import re

from .compat import UnsupportedStateSchemaVersion, empty_state, normalize_state
from .writer import ACCEPTED_ADVISORY_VERDICTS, StateTransitionProposal


DIAGNOSTIC_ACTIVITY_KINDS = {"diagnostic_lsp", "heavy_static"}
GPT_PRO_WARNING_MINUTES = 30.0
GPT_PRO_LONG_WAIT_MINUTES = 60.0
GPT_PRO_UNCERTAIN_MINUTES = 90.0
GPT_PRO_HARD_TIMEOUT_MINUTES = 120.0
ORDINARY_TIMEOUT_MINUTES = 15.0
DIAGNOSTIC_TIMEOUT_MINUTES = 20.0


@dataclass(frozen=True)
class ReviewWatchdogObservation:
    target_id: str
    review_scope: str
    started_at: str
    last_activity_at: str | None = None
    last_activity_kind: str = "ordinary"
    target_kind: str | None = None
    process_pid: int | None = None
    process_command: str | None = None
    rollout_path: str | None = None
    last_function_call: str | None = None
    evidence_refs: list[str] | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ReviewWatchdogObservation":
        return cls(
            target_id=str(payload["target_id"]),
            review_scope=str(payload["review_scope"]),
            started_at=str(payload["started_at"]),
            last_activity_at=payload.get("last_activity_at"),
            last_activity_kind=str(payload.get("last_activity_kind", "ordinary")),
            target_kind=payload.get("target_kind"),
            process_pid=payload.get("process_pid"),
            process_command=payload.get("process_command"),
            rollout_path=payload.get("rollout_path"),
            last_function_call=payload.get("last_function_call"),
            evidence_refs=list(payload.get("evidence_refs") or []),
        )


@dataclass(frozen=True)
class ReviewWatchdogDecision:
    decision: str
    reason: str
    target_id: str
    review_scope: str
    state_write: bool
    target_state: str | None
    review_receipts: list[dict[str, Any]]
    elapsed_minutes: float
    threshold_minutes: float | None = None
    blocker_code: str | None = None
    next_required_action: str | None = None
    proposal: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_watchdog(
    *,
    root: Path,
    observation: ReviewWatchdogObservation,
    now: datetime | None = None,
) -> ReviewWatchdogDecision:
    try:
        state = _read_state(root)
    except UnsupportedStateSchemaVersion:
        elapsed = _elapsed_minutes(observation, now=now)
        return _decision(
            "no_action",
            "unsupported_state_schema_version",
            observation,
            target_state=None,
            receipts=[],
            elapsed=elapsed,
        )
    target = state.get("targets", {}).get(observation.target_id, {})
    target_state = target.get("state")
    receipts = list(target.get("review_receipts", []))
    elapsed = _elapsed_minutes(observation, now=now)

    if not target:
        return _decision(
            "no_action",
            "target_missing",
            observation,
            target_state=None,
            receipts=[],
            elapsed=elapsed,
        )

    if target_state == "closed":
        return _decision("no_action", "target_closed", observation, target_state, receipts, elapsed)

    if _has_receipt(receipts, observation.review_scope):
        return _decision(
            "no_action",
            "review_receipt_already_recorded",
            observation,
            target_state,
            receipts,
            elapsed,
        )

    if observation.review_scope == "codex_cc":
        return _evaluate_codex_cc(state, observation, target_state, receipts, elapsed)

    if observation.review_scope == "gpt_pro":
        return _evaluate_gpt_pro(state, observation, target_state, receipts, elapsed)

    return _decision(
        "no_action",
        "unsupported_review_scope",
        observation,
        target_state,
        receipts,
        elapsed,
    )


def load_observation(path: Path) -> ReviewWatchdogObservation:
    return ReviewWatchdogObservation.from_payload(json.loads(path.read_text(encoding="utf-8")))


def write_proposal(path: Path, decision: ReviewWatchdogDecision) -> bool:
    if decision.proposal is None:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(decision.proposal, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return True


def _evaluate_codex_cc(
    state: dict[str, Any],
    observation: ReviewWatchdogObservation,
    target_state: str | None,
    receipts: list[dict[str, Any]],
    elapsed: float,
) -> ReviewWatchdogDecision:
    threshold = DIAGNOSTIC_TIMEOUT_MINUTES if _is_diagnostic_wait(observation) else ORDINARY_TIMEOUT_MINUTES
    if elapsed < threshold:
        return _decision(
            "in_progress",
            "codex_cc_within_timeout",
            observation,
            target_state,
            receipts,
            elapsed,
            threshold=threshold,
            next_required_action="wait_for_codex_cc_receipt",
        )

    proposal = _timeout_proposal(
        state=state,
        observation=observation,
        actor_role="codex_cc",
        target_kind=observation.target_kind or "small_chapter",
        blocker_code="codex_cc_review_timeout",
        blocker_detail=(
            f"Codex cc scoped review exceeded {threshold:g} minutes without a receipt; "
            f"last activity: {observation.last_activity_kind}"
        ),
    )
    return _decision(
        "timeout_candidate",
        "codex_cc_review_timeout_without_receipt",
        observation,
        target_state,
        receipts,
        elapsed,
        threshold=threshold,
        blocker_code="codex_cc_review_timeout",
        next_required_action="review_retry_or_repair_active",
        proposal=asdict(proposal),
    )


def _evaluate_gpt_pro(
    state: dict[str, Any],
    observation: ReviewWatchdogObservation,
    target_state: str | None,
    receipts: list[dict[str, Any]],
    elapsed: float,
) -> ReviewWatchdogDecision:
    if elapsed >= GPT_PRO_HARD_TIMEOUT_MINUTES:
        blocker_code = "gpt_pro_review_hard_timeout"
        proposal = _timeout_proposal(
            state=state,
            observation=observation,
            actor_role="gpt_pro",
            target_kind=observation.target_kind or "major_chapter",
            blocker_code=blocker_code,
            blocker_detail="GPT Pro desktop review exceeded the hard timeout without a receipt.",
        )
        return _decision(
            "timeout_candidate",
            "gpt_pro_hard_timeout_without_receipt",
            observation,
            target_state,
            receipts,
            elapsed,
            threshold=GPT_PRO_HARD_TIMEOUT_MINUTES,
            blocker_code=blocker_code,
            next_required_action="desktop_review_recovery_or_repair_active",
            proposal=asdict(proposal),
        )

    if elapsed >= GPT_PRO_UNCERTAIN_MINUTES:
        blocker_code = "gpt_pro_desktop_uncertain"
        proposal = _timeout_proposal(
            state=state,
            observation=observation,
            actor_role="gpt_pro",
            target_kind=observation.target_kind or "major_chapter",
            blocker_code=blocker_code,
            blocker_detail="GPT Pro desktop review exceeded the uncertainty threshold without a receipt.",
        )
        return _decision(
            "uncertain_candidate",
            "gpt_pro_desktop_uncertain_without_receipt",
            observation,
            target_state,
            receipts,
            elapsed,
            threshold=GPT_PRO_UNCERTAIN_MINUTES,
            blocker_code=blocker_code,
            next_required_action="desktop_review_recovery_or_typed_blocker",
            proposal=asdict(proposal),
        )

    if elapsed >= GPT_PRO_LONG_WAIT_MINUTES:
        return _decision(
            "long_wait",
            "gpt_pro_review_long_wait_without_receipt",
            observation,
            target_state,
            receipts,
            elapsed,
            threshold=GPT_PRO_LONG_WAIT_MINUTES,
            blocker_code="gpt_pro_review_long_wait",
            next_required_action="poll_existing_gpt_pro_review",
        )

    if elapsed >= GPT_PRO_WARNING_MINUTES:
        return _decision(
            "warning",
            "gpt_pro_review_warning_without_receipt",
            observation,
            target_state,
            receipts,
            elapsed,
            threshold=GPT_PRO_WARNING_MINUTES,
            next_required_action="poll_existing_gpt_pro_review",
        )

    return _decision(
        "in_progress",
        "gpt_pro_within_timeout",
        observation,
        target_state,
        receipts,
        elapsed,
        threshold=GPT_PRO_WARNING_MINUTES,
        next_required_action="wait_for_gpt_pro_receipt",
    )


def _timeout_proposal(
    *,
    state: dict[str, Any],
    observation: ReviewWatchdogObservation,
    actor_role: str,
    target_kind: str,
    blocker_code: str,
    blocker_detail: str,
) -> StateTransitionProposal:
    return StateTransitionProposal(
        proposal_id=_proposal_id(observation, blocker_code, state.get("state_revision", 0)),
        target_kind=target_kind,
        target_id=observation.target_id,
        base_state_revision=int(state.get("state_revision", 0)),
        requested_state="closure_candidate",
        actor_role=actor_role,
        evidence_refs=_evidence_refs(observation),
        review_scope=observation.review_scope,
        review_mode="watchdog_timeout",
        verdict="blocker",
        blocker_code=blocker_code,
        blocker_detail=blocker_detail,
        summary=f"Review watchdog detected {blocker_code} for {observation.target_id}.",
    )


def _proposal_id(observation: ReviewWatchdogObservation, blocker_code: str, revision: int) -> str:
    suffix = re.sub(r"[^a-zA-Z0-9_.-]+", "-", observation.target_id).strip("-").lower()
    return f"watchdog-{suffix}-{observation.review_scope}-{blocker_code}-rev{revision}"


def _evidence_refs(observation: ReviewWatchdogObservation) -> list[str]:
    refs = list(observation.evidence_refs or [])
    if observation.rollout_path:
        refs.append(f"rollout:{observation.rollout_path}")
    if observation.process_pid is not None:
        refs.append(f"process:{observation.process_pid}")
    if observation.process_command:
        refs.append(f"command:{observation.process_command}")
    if observation.last_function_call:
        refs.append(f"last_function_call:{observation.last_function_call}")
    return sorted(set(refs))


def _has_receipt(receipts: list[dict[str, Any]], scope: str) -> bool:
    # A receipt suppresses a timeout only when it marks the review as *concluded toward
    # closure* -- exactly writer.ACCEPTED_ADVISORY_VERDICTS. "blocker" is deliberately NOT
    # in that set: the writer routes a blocker into the repair loop (review_blocked ->
    # repair_active -> a fresh evidence_pending -> a NEW codex_cc review), so the target's
    # review is still in flight. Since review_receipts is append-only and carries no attempt
    # id, counting a blocker here would let one blocker permanently blind the watchdog to a
    # stalled post-repair re-review. Reuse the writer's canonical set so the two sides
    # (here and writer._has_codex_cc_receipt) can never silently drift apart again.
    for receipt in receipts:
        if receipt.get("scope") == scope and receipt.get("verdict") in ACCEPTED_ADVISORY_VERDICTS:
            return True
    return False


def _is_diagnostic_wait(observation: ReviewWatchdogObservation) -> bool:
    if observation.last_activity_kind in DIAGNOSTIC_ACTIVITY_KINDS:
        return True
    call = (observation.last_function_call or "").lower()
    return "diagnostic" in call or "lsp_" in call or "lsp." in call


def _elapsed_minutes(observation: ReviewWatchdogObservation, *, now: datetime | None) -> float:
    current = now or datetime.now(timezone.utc)
    anchor = observation.last_activity_at or observation.started_at
    then = _parse_datetime(anchor)
    return max(0.0, (current - then).total_seconds() / 60.0)


def _parse_datetime(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_state(root: Path) -> dict[str, Any]:
    path = root / ".omx" / "state" / "ao-state-writer" / "state.json"
    if not path.exists():
        return empty_state()
    return normalize_state(json.loads(path.read_text(encoding="utf-8")))


def _decision(
    decision: str,
    reason: str,
    observation: ReviewWatchdogObservation,
    target_state: str | None,
    receipts: list[dict[str, Any]],
    elapsed: float,
    *,
    threshold: float | None = None,
    blocker_code: str | None = None,
    next_required_action: str | None = None,
    proposal: dict[str, Any] | None = None,
) -> ReviewWatchdogDecision:
    return ReviewWatchdogDecision(
        decision=decision,
        reason=reason,
        target_id=observation.target_id,
        review_scope=observation.review_scope,
        state_write=False,
        target_state=target_state,
        review_receipts=receipts,
        elapsed_minutes=round(elapsed, 3),
        threshold_minutes=threshold,
        blocker_code=blocker_code,
        next_required_action=next_required_action,
        proposal=proposal,
    )
