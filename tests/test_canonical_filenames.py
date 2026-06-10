"""Regression tests for contract-driven canonical governance filenames (Group C step 1 / S-GAP1).

WHY (intent, not just behavior): the public engine must be project-agnostic, so the canonical
TODO / SESSION_LOG / MASTER_PLAN filenames are resolved from the project contract instead of being
hardcoded. A contract-supplied name is joined to the project root (``root / name``); therefore an
unsafe value (path traversal, absolute/drive/UNC path, control char) must FAIL LOUD with a typed
``invalid_canonical_filename`` signal, never silently fall back to a default — a silent fallback
would hide a traversal attempt as "the default was used" and could guard/read the wrong file.
Absent section/key is NOT a misconfig: it means "use the default bare name".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ao_state_writer.compat import (
    InvalidCanonicalFilename,
    _validate_canonical_filename,
    canonical_master_plan_file,
    canonical_session_log_file,
    canonical_todo_file,
)
from ao_state_writer.continuation import _read_compact_current_state
from ao_state_writer.preflight import (
    FIXED_GOVERNANCE_FILES,
    effective_governance_files,
    find_governance_blockers,
)


def _write_contract(root: Path, body: str) -> None:
    (root / "DIRECT_PROJECT_CONTRACT.toml").write_text(body, encoding="utf-8")


# --- resolver defaults / overrides -------------------------------------------------------------

def test_defaults_when_no_contract(tmp_path: Path) -> None:
    assert canonical_todo_file(tmp_path) == "TODO.md"
    assert canonical_session_log_file(tmp_path) == "SESSION_LOG.md"
    assert canonical_master_plan_file(tmp_path) == "MASTER_PLAN.md"


def test_defaults_when_section_absent(tmp_path: Path) -> None:
    _write_contract(tmp_path, '[owner_proxy]\nproject_id = "p"\n')
    assert canonical_todo_file(tmp_path) == "TODO.md"


def test_custom_filenames_respected(tmp_path: Path) -> None:
    _write_contract(
        tmp_path,
        "[canonical]\n"
        'todo_file = "ROADMAP.md"\n'
        'session_log_file = "JOURNAL.md"\n'
        'master_plan_file = "PLAN.md"\n',
    )
    assert canonical_todo_file(tmp_path) == "ROADMAP.md"
    assert canonical_session_log_file(tmp_path) == "JOURNAL.md"
    assert canonical_master_plan_file(tmp_path) == "PLAN.md"


# --- fail-loud on unsafe values ----------------------------------------------------------------

@pytest.mark.parametrize(
    "bad",
    [
        "../escape.md",   # parent ref
        "sub/dir.md",     # path separator
        "/abs.md",        # absolute (also has '/')
        "C:evil.md",      # windows drive
        " TODO.md",       # leading whitespace
        "TODO.md ",       # trailing whitespace
        "..",             # bare parent
    ],
)
def test_unsafe_value_via_contract_fails_loud(tmp_path: Path, bad: str) -> None:
    # A PRESENT but unsafe value raises — it must NOT silently become the default.
    _write_contract(tmp_path, f'[canonical]\ntodo_file = "{bad}"\n')
    with pytest.raises(InvalidCanonicalFilename) as exc:
        canonical_todo_file(tmp_path)
    assert exc.value.key == "todo_file"


@pytest.mark.parametrize("bad", ["", "   ", "a\tb.md", "a\x00b.md", "a\\b.md", "..foo.md"])
def test_validator_rejects_edge_cases(bad: str) -> None:
    # Direct validator coverage for forms awkward to embed in TOML (control chars, backslash).
    with pytest.raises(InvalidCanonicalFilename):
        _validate_canonical_filename(bad, "todo_file")


@pytest.mark.parametrize("body", ['todo_file = ""', 'todo_file = "   "', "todo_file = 123"])
def test_present_but_blank_or_nonstring_fails_loud(tmp_path: Path, body: str) -> None:
    # A PRESENT key whose value is blank/whitespace-only/non-string must FAIL LOUD, not silently
    # fall back to the default. Regression: read_contract_string collapses such values to None, which
    # the resolver must NOT mistake for "key absent" (the no-silent-fallback contract).
    _write_contract(tmp_path, f"[canonical]\n{body}\n")
    with pytest.raises(InvalidCanonicalFilename) as exc:
        canonical_todo_file(tmp_path)
    assert exc.value.key == "todo_file"


def test_validator_accepts_bare_name() -> None:
    assert _validate_canonical_filename("ROADMAP.md", "todo_file") == "ROADMAP.md"


# --- continuation todo-read wiring -------------------------------------------------------------

def test_read_compact_state_default_todo_name(tmp_path: Path) -> None:
    _, blockers = _read_compact_current_state(tmp_path)
    assert blockers == ["missing_todo_file:TODO.md"]


def test_read_compact_state_uses_custom_todo_name(tmp_path: Path) -> None:
    _write_contract(tmp_path, '[canonical]\ntodo_file = "ROADMAP.md"\n')
    # The custom file does not exist yet; the missing-file blocker must name the CUSTOM file,
    # proving the read resolved the contract rather than the hardcoded default.
    _, blockers = _read_compact_current_state(tmp_path)
    assert blockers == ["missing_todo_file:ROADMAP.md"]


def test_read_compact_state_finds_custom_todo_file(tmp_path: Path) -> None:
    _write_contract(tmp_path, '[canonical]\ntodo_file = "ROADMAP.md"\n')
    (tmp_path / "ROADMAP.md").write_text(
        "# Roadmap\n\n## Current Execution State\n\n- note: value\n", encoding="utf-8"
    )
    _, blockers = _read_compact_current_state(tmp_path)
    assert "missing_todo_file:ROADMAP.md" not in blockers


def test_read_compact_state_unsafe_canonical_fails_loud(tmp_path: Path) -> None:
    _write_contract(tmp_path, '[canonical]\ntodo_file = "../escape.md"\n')
    values, blockers = _read_compact_current_state(tmp_path)
    assert values == {}
    assert blockers == ["invalid_canonical_filename:todo_file"]


def test_read_compact_state_rejects_duplicate_sections(tmp_path: Path) -> None:
    # WHY: two "## Current Execution State" sections are ambiguous — the read must fail closed rather
    # than silently consume the first (this mirrors the TODO-repair fail-closed guard in todo.py and
    # is the hardening ported from the LIVE engine).
    (tmp_path / "TODO.md").write_text(
        "## Current Execution State\n\n- a: 1\n\n## Other\n\n## Current Execution State\n\n- b: 2\n",
        encoding="utf-8",
    )
    values, blockers = _read_compact_current_state(tmp_path)
    assert values == {}
    assert blockers == ["multiple_current_execution_state_sections"]


def test_read_compact_state_single_section_parses(tmp_path: Path) -> None:
    (tmp_path / "TODO.md").write_text(
        "# T\n\n## Current Execution State\n\n- note: ok\n\n## Next\n", encoding="utf-8"
    )
    _, blockers = _read_compact_current_state(tmp_path)
    assert "missing_current_execution_state_section" not in blockers
    assert "multiple_current_execution_state_sections" not in blockers


# --- preflight governance-file resolution ------------------------------------------------------

def test_effective_governance_files_default(tmp_path: Path) -> None:
    gov = effective_governance_files(tmp_path)
    assert {"TODO.md", "SESSION_LOG.md", "MASTER_PLAN.md"} <= gov
    assert FIXED_GOVERNANCE_FILES <= gov


def test_effective_governance_files_custom(tmp_path: Path) -> None:
    _write_contract(tmp_path, '[canonical]\ntodo_file = "ROADMAP.md"\n')
    gov = effective_governance_files(tmp_path)
    assert "ROADMAP.md" in gov
    # The default name is NO LONGER guarded once the project renames it.
    assert "TODO.md" not in gov
    # The un-overridden members of the trio keep their defaults.
    assert {"SESSION_LOG.md", "MASTER_PLAN.md"} <= gov


def test_effective_governance_files_unsafe_raises(tmp_path: Path) -> None:
    _write_contract(tmp_path, '[canonical]\nmaster_plan_file = "../x.md"\n')
    with pytest.raises(InvalidCanonicalFilename) as exc:
        effective_governance_files(tmp_path)
    assert exc.value.key == "master_plan_file"


def test_find_governance_blockers_unsafe_canonical(tmp_path: Path) -> None:
    # The unsafe-canonical check runs after the .git presence check but BEFORE shelling out to git,
    # so it surfaces as a typed blocker rather than a git error.
    (tmp_path / ".git").mkdir()
    _write_contract(tmp_path, '[canonical]\ntodo_file = "../x.md"\n')
    assert find_governance_blockers(tmp_path) == ["invalid_canonical_filename:todo_file"]


def test_find_governance_blockers_no_git_is_empty(tmp_path: Path) -> None:
    assert find_governance_blockers(tmp_path) == []
