from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re

from .config import CommanderConfig
from .state import read_state


class Mode(str, Enum):
    MISSING_SETUP = "missing_setup"
    SMALL_SLICE = "small_slice"
    OWNER_WAIT = "owner_wait"
    COMPLETION_CANDIDATE = "completion_candidate"
    EXTERNAL_REVIEW_WAIT = "external_review_wait"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class Brief:
    mode: Mode
    current_fact: str
    risk: str
    next_allowed_action: str
    forbidden_shortcut: str

    def render(self) -> str:
        return "\n".join(
            [
                f"Mode: {self.mode.value}",
                f"Current fact: {self.current_fact}",
                f"Risk: {self.risk}",
                f"Next allowed action: {self.next_allowed_action}",
                f"Forbidden shortcut: {self.forbidden_shortcut}",
            ]
        )


def classify(cfg: CommanderConfig) -> Brief:
    missing = []
    if not cfg.master_plan_path.exists():
        missing.append(str(cfg.master_plan_path))
    if not cfg.todo_path.exists():
        missing.append(str(cfg.todo_path))
    if not cfg.codex_worker_thread_id:
        missing.append("codex_worker_thread_id")
    if not cfg.reviewer_target:
        missing.append("reviewer_target")
    if missing:
        return Brief(
            Mode.MISSING_SETUP,
            f"Missing required setup: {', '.join(missing)}",
            "Commander cannot safely dispatch without complete setup.",
            "Run init/doctor, fill commander.toml, and wait for owner confirmation.",
            "Do not infer missing worker references or reviewer targets from latest state.",
        )

    state = read_state(cfg.state_path)
    if state.get("status") == "external_review_pending":
        return Brief(
            Mode.EXTERNAL_REVIEW_WAIT,
            "A review package was submitted and is waiting for external reviewer feedback.",
            "Reviewer output must not be auto-accepted.",
            "Wait for reviewer feedback, then route it through the active owner/contract process.",
            "Do not close the phase or start the next major phase automatically.",
        )

    master_text = cfg.master_plan_path.read_text(encoding="utf-8", errors="replace")
    todo_text = cfg.todo_path.read_text(encoding="utf-8", errors="replace")
    combined = (master_text + "\n" + todo_text).lower()
    # Historical checkpoints often mention older external-review states. Only
    # treat it as current when the recent checkpoint tail records an explicit
    # status, not when it merely names a state or forbidden shortcut.
    current_tail = todo_text[-20000:].lower()
    if re.search(r"status(?: is|:)?\s+(?:external_review_pending|external_review_wait|waiting for review)", current_tail):
        return Brief(
            Mode.EXTERNAL_REVIEW_WAIT,
            "Canonical files indicate external review is pending.",
            "Further execution may race reviewer feedback.",
            "Park and wait for external reviewer feedback plus contract-directed routing.",
            "Do not continue into the next major phase.",
        )

    if cfg.package_preparation_authorized:
        return Brief(
            Mode.COMPLETION_CANDIDATE,
            "Standalone Commander package-preparation guard is enabled in commander.toml.",
            "Package contents and hashes must be verified before submission.",
            "Run package-review and submit-review after local package verification passes.",
            "Do not mark the major phase closed after submission.",
        )

    current_gate_markers = [
        "owner-controlled phase-review/package closure",
        "owner decision on whether to authorize actual",
        "phase-review package preparation",
        "completion-candidate",
        "completion candidate",
    ]
    if any(marker in combined for marker in current_gate_markers):
        return Brief(
            Mode.OWNER_WAIT,
            "Canonical files mention a major-phase candidate or phase-review gate.",
            "Package preparation needs explicit owner authorization.",
            "Ask owner to authorize package preparation or continue the current small slice.",
            "Do not create or submit a phase-review package before the owner gate.",
        )

    return Brief(
        Mode.SMALL_SLICE,
        "No major-phase gate is currently active from canonical files.",
        "Small-slice execution still needs local verification and checkpoint evidence.",
        "Continue the current canonical TODO next slice through a visible worker Plan Mode.",
        "Do not infer major-phase closure from a small-slice completion.",
    )
