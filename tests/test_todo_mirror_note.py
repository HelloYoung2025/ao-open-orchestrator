"""Regression tests for the TODO-mirror-repair-note rendering layer (Group C step 5).

WHY (intent, not just behavior): when the engine detects that the repo-local TODO mirror is STALE or
ambiguous relative to canonical state.json, it reconciles the compact current-state from the canonical
state-writer context and surfaces a `todo_mirror_repair_note` to the spawned worker — telling it NOT to
trust the stale TODO. continuation.py owns the RENDERING of that note (the engine/driver that detects
staleness and injects the note lives in cli.py and lands later). Two invariants must hold:

  1. The note is rendered into the spawn prompt when present, and renders NOTHING when absent (so the
     current public flow — which never sets the key until the driver lands — is byte-unchanged).
  2. The note SURVIVES prompt compaction. It is load-bearing: eliding it would let the worker act on
     stale TODO data and mis-dispatch, which is exactly the stall this self-heal exists to prevent.

These tests drive the rendering layer directly (injecting the key into current_state) because the
cli.py driver that would populate it is not ported yet — testing the layer at its real boundary.
"""

from __future__ import annotations

from pathlib import Path

from ao_state_writer.continuation import (
    AO_PROMPT_SOFT_LIMIT,
    _compact_prompt,
    _render_action_prompt,
    _render_prompt,
    _todo_mirror_note,
    _todo_mirror_note_lines,
)
from ao_state_writer.writer import StateTransitionDecision

NOTE_MARKER = "TODO mirror repair context:"
NOTE_TOKEN = "canonical-state-rev-1742-supersedes-stale-todo"


def _state(**extra: str) -> dict[str, str]:
    base = {
        "current_phase": "phase-x",
        "next_locked_action": "implement-slice",
        "review_gate_state": "none",
        "latest_session_log_anchor": "anchor-1",
    }
    base.update(extra)
    return base


def _decision() -> StateTransitionDecision:
    return StateTransitionDecision(
        decision="accepted",
        proposal_id="p-1",
        reason="ready",
        state_revision=1,
        new_state="dispatch_ready",
        next_required_action="dispatch_next_slice_plan_mode",
    )


# --- helpers in isolation ----------------------------------------------------------------------

def test_todo_mirror_note_empty_when_absent() -> None:
    assert _todo_mirror_note(_state()) == ""
    assert _todo_mirror_note_lines(_state()) == []


def test_todo_mirror_note_rendered_when_present() -> None:
    note = _todo_mirror_note(_state(todo_mirror_repair_note=NOTE_TOKEN))
    assert note == f"{NOTE_MARKER}\n- {NOTE_TOKEN}\n\n"
    lines = _todo_mirror_note_lines(_state(todo_mirror_repair_note=NOTE_TOKEN))
    assert lines == ["", NOTE_MARKER, f"- {NOTE_TOKEN}"]


# --- through the real renderers ----------------------------------------------------------------

def test_render_prompt_includes_note_when_present() -> None:
    prompt = _render_prompt(
        template="Use canonical next locked action.",
        current_state=_state(todo_mirror_repair_note=NOTE_TOKEN),
        decision=_decision(),
        active_root=Path("/x"),
        orchestrator_session="example-orchestrator",
    )
    assert NOTE_MARKER in prompt
    assert NOTE_TOKEN in prompt


def test_render_prompt_no_note_is_zero_change() -> None:
    # Absent key => the marker must not appear at all (the current public flow renders identically to
    # before step 5, since _read_compact_current_state never sets todo_mirror_repair_note).
    prompt = _render_prompt(
        template="Use canonical next locked action.",
        current_state=_state(),
        decision=_decision(),
        active_root=Path("/x"),
        orchestrator_session="example-orchestrator",
    )
    assert NOTE_MARKER not in prompt


def test_render_action_prompt_includes_note_when_present() -> None:
    decision = StateTransitionDecision(
        decision="accepted",
        proposal_id="p-2",
        reason="state_writer_closure",
        state_revision=1,
        new_state="closed",
        next_required_action="state_writer_closure",
    )
    prompt = _render_action_prompt(
        "state_writer_closure",
        current_state=_state(todo_mirror_repair_note=NOTE_TOKEN),
        decision=decision,
        active_root=Path("/x"),
        orchestrator_session="example-orchestrator",
    )
    assert NOTE_MARKER in prompt
    assert NOTE_TOKEN in prompt


def test_render_action_prompt_no_note_is_zero_change() -> None:
    decision = StateTransitionDecision(
        decision="accepted",
        proposal_id="p-2",
        reason="state_writer_closure",
        state_revision=1,
        new_state="closed",
        next_required_action="state_writer_closure",
    )
    prompt = _render_action_prompt(
        "state_writer_closure",
        current_state=_state(),
        decision=decision,
        active_root=Path("/x"),
        orchestrator_session="example-orchestrator",
    )
    assert NOTE_MARKER not in prompt


# --- compaction preservation -------------------------------------------------------------------

def _oversized_prompt_with_note(note_token: str, *, lead_padding: int) -> str:
    # Build a prompt that exceeds the soft limit and places the note in the elide-prone middle: after
    # the next_locked_action marker, before the AUTH block and the dispatch-obligation tail.
    return (
        "- next_locked_action: x\n"
        + ("PAD " * lead_padding)
        + f"{NOTE_MARKER}\n- {note_token}\n\n"
        + "AO_ORCHESTRATOR_OWNER_PROXY_DISPATCH_AUTHORIZATION:\nauthority prose here\n\n"
        + "Dispatch obligation (Option-2 orchestrator-poke, REQUIRED):\nobligation tail body\n"
    )


def test_compact_prompt_reinjects_note_when_elided_from_head() -> None:
    # Heavy lead padding pushes the note beyond the kept head, so it can only appear via the explicit
    # preservation/re-injection path. Without that path the note (and the stale-TODO warning) would be
    # silently dropped during compaction — the exact regression this guards.
    prompt = _oversized_prompt_with_note(NOTE_TOKEN, lead_padding=1400)
    assert len(prompt) > AO_PROMPT_SOFT_LIMIT
    result = _compact_prompt(prompt)
    assert len(result) <= AO_PROMPT_SOFT_LIMIT
    assert NOTE_MARKER in result
    assert NOTE_TOKEN in result
    assert "Dispatch obligation (Option-2 orchestrator-poke" in result  # tail still preserved


def test_compact_prompt_keeps_note_once_when_it_survives_in_head() -> None:
    # Note appears EARLY (stays inside the kept head); the bulk that forces compaction sits AFTER the
    # AUTH block but before the dispatch-obligation tail (mirroring the real prompt, where nothing pads
    # the gap between the note and AUTH, so preserved_block is just the note). The re-injection branch
    # must detect the note already survived in the head and NOT duplicate it.
    prompt = (
        "- next_locked_action: x\n"
        + f"{NOTE_MARKER}\n- {NOTE_TOKEN}\n\n"
        + "AO_ORCHESTRATOR_OWNER_PROXY_DISPATCH_AUTHORIZATION:\nauthority prose\n\n"
        + ("PAD " * 1100)
        + "Dispatch obligation (Option-2 orchestrator-poke, REQUIRED):\ntail body\n"
    )
    assert len(prompt) > AO_PROMPT_SOFT_LIMIT
    result = _compact_prompt(prompt)
    assert len(result) <= AO_PROMPT_SOFT_LIMIT
    assert result.count(NOTE_MARKER) == 1


def test_compact_prompt_no_note_still_compacts() -> None:
    # No note present: compaction still works (preserved_block is empty) and the tail is preserved.
    prompt = (
        "- next_locked_action: x\n"
        + ("PAD " * 1400)
        + "AO_ORCHESTRATOR_OWNER_PROXY_DISPATCH_AUTHORIZATION:\nauthority prose\n\n"
        + "Dispatch obligation (Option-2 orchestrator-poke, REQUIRED):\nobligation tail\n"
    )
    assert len(prompt) > AO_PROMPT_SOFT_LIMIT
    result = _compact_prompt(prompt)
    assert len(result) <= AO_PROMPT_SOFT_LIMIT
    assert NOTE_MARKER not in result
    assert "Dispatch obligation (Option-2 orchestrator-poke" in result
