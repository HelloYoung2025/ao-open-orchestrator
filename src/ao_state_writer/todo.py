from __future__ import annotations

import re


COMPACT_CURRENT_STATE_FIELDS = (
    "current_phase",
    "next_locked_action",
    "review_gate_state",
    "latest_session_log_anchor",
)
CURRENT_STATE_HEADING = "## Current Execution State"
CURRENT_STATE_HEADING_RE = re.compile(r"(?m)^## Current Execution State\s*$")

LEGACY_CURRENT_STATE_FIELDS = {
    "current_next_slice",
    "Current next locked action",
    "Current review-package target",
    "next_slice_end_retro_gate",
    "next_slice_retro_items",
    "execution_gate_note",
}


def render_compact_current_state(
    *,
    current_phase: str,
    next_locked_action: str,
    review_gate_state: str,
    latest_session_log_anchor: str,
) -> str:
    values = {
        "current_phase": current_phase,
        "next_locked_action": next_locked_action,
        "review_gate_state": review_gate_state,
        "latest_session_log_anchor": latest_session_log_anchor,
    }
    return "\n".join(f"- {field}: {values[field]}" for field in COMPACT_CURRENT_STATE_FIELDS) + "\n"


def validate_compact_current_state(text: str) -> list[str]:
    seen = set()
    errors: list[str] = []
    for line in text.splitlines():
        if not line.startswith("- "):
            continue
        name = line[2:].split(":", 1)[0].strip()
        if name in LEGACY_CURRENT_STATE_FIELDS:
            errors.append(f"unexpected_current_state_field:{name}")
        if name in COMPACT_CURRENT_STATE_FIELDS:
            seen.add(name)
    for field in COMPACT_CURRENT_STATE_FIELDS:
        if field not in seen:
            errors.append(f"missing_current_state_field:{field}")
    return errors


def repair_compact_current_state(
    todo_text: str,
    *,
    current_phase: str,
    next_locked_action: str,
    review_gate_state: str,
    latest_session_log_anchor: str,
) -> str:
    matches = list(CURRENT_STATE_HEADING_RE.finditer(todo_text))
    marker_count = len(matches)
    if marker_count == 0:
        raise ValueError("missing_current_execution_state_section")
    if marker_count > 1:
        raise ValueError("multiple_current_execution_state_sections")
    compact = render_compact_current_state(
        current_phase=current_phase,
        next_locked_action=next_locked_action,
        review_gate_state=review_gate_state,
        latest_session_log_anchor=latest_session_log_anchor,
    )
    replacement = f"{CURRENT_STATE_HEADING}\n\n{compact}\n"

    match = matches[0]
    before = todo_text[: match.start()]
    rest = todo_text[match.end() :]
    next_heading = rest.find("\n## ")
    if next_heading == -1:
        return before + replacement
    return before + replacement + rest[next_heading + 1 :]
