"""Regression tests for the fail-closed contract of ``repair_compact_current_state``.

WHY this behavior matters (intent, not just output):
  ``repair-todo`` rewrites the ``## Current Execution State`` block that the
  orchestrator later READS BACK to decide its next dispatch. If the section is
  missing (renamed/dropped) or DUPLICATED, the correct anti-stall move is to
  REFUSE to guess — silently appending a second section, or appending one to a
  TODO whose section was lost, would corrupt that governance mirror and let the
  orchestrator act on a malformed read. So the repair fails CLOSED (raises), the
  CLI surfaces ``todo_mirror_repair_failed`` with exit 3, and — critically — the
  TODO file is left BYTE-UNCHANGED so a human/owner can reconcile it. These tests
  pin both the library raise-contract and the CLI's no-write/exit-3 path; a
  silent regression back to auto-append would re-open a known stall class.
"""

from __future__ import annotations

from pathlib import Path
import json

import pytest

from ao_state_writer.todo import repair_compact_current_state


HEADING = "## Current Execution State"


def _well_formed_todo() -> str:
    return (
        "# TODO\n\n"
        f"{HEADING}\n\n"
        "- current_phase: old_phase\n"
        "- next_locked_action: old_action\n"
        "- review_gate_state: old_gate\n"
        "- latest_session_log_anchor: old_anchor\n\n"
        "## Backlog\n\n"
        "- keep me\n"
    )


def _repair(todo_text: str) -> str:
    return repair_compact_current_state(
        todo_text,
        current_phase="new_phase",
        next_locked_action="new_action",
        review_gate_state="new_gate",
        latest_session_log_anchor="new_anchor",
    )


# --- library raise-contract -------------------------------------------------


def test_exactly_one_section_is_rewritten_in_place() -> None:
    result = _repair(_well_formed_todo())
    # New values replace the old block, the heading is preserved exactly once, and
    # content after the next heading survives (no truncation of unrelated sections).
    assert result.count(HEADING) == 1
    assert "- current_phase: new_phase" in result
    assert "- latest_session_log_anchor: new_anchor" in result
    assert "old_phase" not in result
    assert "## Backlog\n\n- keep me\n" in result


def test_missing_section_raises_not_appends() -> None:
    todo = "# TODO\n\n## Backlog\n\n- something\n"
    with pytest.raises(ValueError, match="missing_current_execution_state_section"):
        _repair(todo)


def test_duplicate_section_raises() -> None:
    todo = (
        "# TODO\n\n"
        f"{HEADING}\n\n- current_phase: a\n\n"
        f"{HEADING}\n\n- current_phase: b\n"
    )
    with pytest.raises(ValueError, match="multiple_current_execution_state_sections"):
        _repair(todo)


def test_heading_match_is_anchored_to_whole_line() -> None:
    # A '## Current Execution State' that appears as a substring of a LONGER heading
    # (e.g. a prose mention) must NOT count as the section marker — the anchored,
    # multiline regex requires the heading to own the entire line.
    todo = (
        "# TODO\n\n"
        "## Current Execution State Notes\n\n"
        "- this is not the real compact block\n"
    )
    with pytest.raises(ValueError, match="missing_current_execution_state_section"):
        _repair(todo)


# --- CLI no-write / exit-3 path ---------------------------------------------


def _run_repair_cli(tmp_path: Path, todo_text: str) -> tuple[int, dict, str]:
    from ao_state_writer.cli import main

    todo_file = tmp_path / "TODO.md"
    todo_file.write_text(todo_text, encoding="utf-8")
    rc = main(
        [
            "repair-todo",
            "--todo-file",
            str(todo_file),
            "--current-phase",
            "new_phase",
            "--next-locked-action",
            "new_action",
            "--review-gate-state",
            "new_gate",
            "--latest-session-log-anchor",
            "new_anchor",
        ]
    )
    return rc, todo_file, todo_file.read_text(encoding="utf-8")


def test_cli_repair_todo_success_exit_0(tmp_path: Path, capsys) -> None:
    rc, _todo_file, after = _run_repair_cli(tmp_path, _well_formed_todo())
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["ok"] is True
    assert "- current_phase: new_phase" in after


def test_cli_repair_todo_missing_section_fails_closed(tmp_path: Path, capsys) -> None:
    original = "# TODO\n\n## Backlog\n\n- something\n"
    rc, _todo_file, after = _run_repair_cli(tmp_path, original)
    out = json.loads(capsys.readouterr().out)
    assert rc == 3
    assert out["ok"] is False
    assert out["result"] == "todo_mirror_repair_failed"
    assert out["reason"] == "missing_current_execution_state_section"
    # The malformed TODO must be left BYTE-UNCHANGED (no partial write).
    assert after == original


def test_cli_repair_todo_duplicate_section_fails_closed(tmp_path: Path, capsys) -> None:
    original = (
        "# TODO\n\n"
        f"{HEADING}\n\n- current_phase: a\n\n"
        f"{HEADING}\n\n- current_phase: b\n"
    )
    rc, _todo_file, after = _run_repair_cli(tmp_path, original)
    out = json.loads(capsys.readouterr().out)
    assert rc == 3
    assert out["result"] == "todo_mirror_repair_failed"
    assert out["reason"] == "multiple_current_execution_state_sections"
    assert after == original
