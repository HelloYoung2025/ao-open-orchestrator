"""Regression tests for the contract-configurable state-writer command (Group C step 4).

WHY (intent, not just behavior): a dispatched worker's REQUIRED dispatch-obligation block tells it
which command to invoke to record the next canonical transition. The public engine ships
``ao-state-writer`` as a console script, so that is the correct brand-neutral DEFAULT — and an absent
``[state_writer] command`` MUST render the obligation byte-identically (zero behavior change). But a
project running the engine from a source checkout, or mid-migration, needs to override the invocation
(e.g. a ``PYTHONPATH``-prefixed ``python -m ao_state_writer.cli``) WITHOUT editing the engine — that
project-agnosticism is the stated end goal. The opposite failure mode also matters: porting the LIVE
engine's machine/product-coupled command form (a hardcoded interpreter path + a product src tree)
would REGRESS this generic engine and leak host/product detail, so it is deliberately NOT done; the
override is a contract value, defaulting to the neutral console script.

The substitution also closes a silent-stall risk: if the obligation rendered a literal placeholder
(``<state_writer_cmd>``) the worker would be told to run a non-existent command and the loop would
stall. These tests assert no placeholder ever survives rendering.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ao_state_writer.continuation import (
    AUTO_SPAWN_ACTIONS,
    GATED_ACTIONS,
    NON_EXECUTABLE_ACTIONS,
    _dispatch_obligation,
    _render_action_prompt,
    _state_writer_command,
    evaluate_continuation,
)
from ao_state_writer.writer import StateTransitionDecision

DEFAULT_CMD = "ao-state-writer"
CUSTOM_CMD = "PYTHONPATH=/srv/checkout/src python3 -m ao_state_writer.cli"
PLACEHOLDER = "<state_writer_cmd>"


def _action_array(values: tuple[str, ...]) -> str:
    # Derive from the live code tuples (not hardcoded) so the contract's action vocabulary always
    # equals what compat.validate_contract_compat matches — hardcoding would rot into a
    # contract_action_vocabulary_mismatch the day the tuples change.
    items = ",\n".join(f'  "{value}"' for value in values)
    return f"[\n{items}\n]"


def _write_contract(root: Path, *, state_writer_section: str = "") -> None:
    # A contract runnable by evaluate_continuation for the dispatch_next_slice_plan_mode action:
    # version + active_root + action vocabulary + the next_step_plan_mode template. The optional
    # [state_writer] block is injected verbatim so a test can omit it (default), set it, or blank it.
    root.joinpath("DIRECT_PROJECT_CONTRACT.toml").write_text(
        "version = 1\n"
        "\n"
        "[ao_clone_isolation]\n"
        f'active_root = "{root.as_posix()}"\n'
        "\n"
        "[owner_proxy]\n"
        'project_id = "example-project"\n'
        "\n"
        "[continuation_policy]\n"
        'orchestrator_session = "example-orchestrator"\n'
        f"auto_spawn_actions = {_action_array(AUTO_SPAWN_ACTIONS)}\n"
        f"gated_actions = {_action_array(GATED_ACTIONS)}\n"
        f"non_executable_actions = {_action_array(NON_EXECUTABLE_ACTIONS)}\n"
        f"{state_writer_section}"
        "\n"
        "[dispatch_templates.next_step_plan_mode]\n"
        'template = """\n'
        "Use canonical next locked action. Do not modify MASTER_PLAN.md.\n"
        '"""\n',
        encoding="utf-8",
    )


def _write_todo(root: Path) -> None:
    root.joinpath("TODO.md").write_text(
        "# Example Project TODO\n"
        "\n"
        "## Current Execution State\n"
        "\n"
        "- current_phase: example-phase\n"
        "- next_locked_action: implement-example-slice\n"
        "- review_gate_state: none\n"
        "- latest_session_log_anchor: example-session-log-anchor\n",
        encoding="utf-8",
    )


# --- resolver: default / override / degenerate -------------------------------------------------

def test_resolver_defaults_when_no_contract(tmp_path: Path) -> None:
    assert _state_writer_command(tmp_path) == DEFAULT_CMD


def test_resolver_defaults_when_section_absent(tmp_path: Path) -> None:
    _write_contract(tmp_path)
    assert _state_writer_command(tmp_path) == DEFAULT_CMD


def test_resolver_respects_override(tmp_path: Path) -> None:
    _write_contract(tmp_path, state_writer_section=f'\n[state_writer]\ncommand = "{CUSTOM_CMD}"\n')
    assert _state_writer_command(tmp_path) == CUSTOM_CMD


def test_resolver_blank_command_degrades_to_default(tmp_path: Path) -> None:
    # A blank/whitespace command is NOT a filesystem-join security surface (unlike a canonical
    # filename), so it intentionally degrades to the neutral default rather than failing loud.
    # CRITICAL cross-version intent: read_contract_string's tomllib path (Python >= 3.11) collapses a
    # blank value to None, but its regex fallback (Python < 3.11) returns the blank VERBATIM. The
    # resolver strips so this test holds on BOTH legs — guarding the exact regression codex flagged
    # (it would pass on 3.11 and silently fail on the 3.10 CI leg without the strip).
    _write_contract(tmp_path, state_writer_section='\n[state_writer]\ncommand = "   "\n')
    assert _state_writer_command(tmp_path) == DEFAULT_CMD


def test_resolver_blank_command_degrades_to_default_on_regex_fallback(
    tmp_path: Path, monkeypatch: "pytest.MonkeyPatch"
) -> None:
    # Force the Python < 3.11 read path (tomllib absent) so this runner exercises the regex fallback
    # regardless of its own Python version. WITHOUT the resolver's strip, the fallback returns the
    # whitespace verbatim and this assertion fails — i.e. this test would have caught codex's finding
    # on a 3.11-only CI. Also confirm a REAL command still reads correctly on the same path.
    import ao_state_writer.compat as _compat

    monkeypatch.setattr(_compat, "tomllib", None)
    _write_contract(tmp_path, state_writer_section='\n[state_writer]\ncommand = "   "\n')
    assert _state_writer_command(tmp_path) == DEFAULT_CMD

    _write_contract(tmp_path, state_writer_section=f'\n[state_writer]\ncommand = "{CUSTOM_CMD}"\n')
    assert _state_writer_command(tmp_path) == CUSTOM_CMD


# --- obligation rendering: default is byte-stable, override flows, no placeholder survives ------

def test_dispatch_obligation_default_renders_console_script() -> None:
    text = _dispatch_obligation(
        action="dispatch_next_slice_plan_mode",
        active_root=Path("/x"),
        orchestrator_session="example-orchestrator",
    )
    assert f"`{DEFAULT_CMD} apply --root /x" in text
    assert f"`{DEFAULT_CMD} continue`" in text
    assert PLACEHOLDER not in text


def test_dispatch_obligation_override_replaces_command() -> None:
    text = _dispatch_obligation(
        action="dispatch_next_slice_plan_mode",
        active_root=Path("/x"),
        orchestrator_session="example-orchestrator",
        state_writer_cmd=CUSTOM_CMD,
    )
    assert f"`{CUSTOM_CMD} apply --root /x" in text
    assert f"`{CUSTOM_CMD} continue`" in text
    # The default token must not appear once overridden, proving a real substitution (not an append).
    assert DEFAULT_CMD not in text
    assert PLACEHOLDER not in text


def test_codex_cc_obligation_override_replaces_command() -> None:
    # The codex_cc_review action selects the OTHER obligation template; it must thread the cmd too.
    text = _dispatch_obligation(
        action="codex_cc_review",
        active_root=Path("/x"),
        orchestrator_session="example-orchestrator",
        state_writer_cmd=CUSTOM_CMD,
    )
    assert "Codex cc receipt proposal" in text
    assert f"`{CUSTOM_CMD} apply --root /x" in text
    assert DEFAULT_CMD not in text
    assert PLACEHOLDER not in text


def test_render_action_prompt_threads_command() -> None:
    decision = StateTransitionDecision(
        decision="accepted",
        proposal_id="p-1",
        reason="state_writer_closure",
        state_revision=1,
        new_state="closed",
        next_required_action="state_writer_closure",
    )
    prompt = _render_action_prompt(
        "state_writer_closure",
        current_state={
            "current_phase": "phase-x",
            "next_locked_action": "state_writer_closure",
            "review_gate_state": "none",
            "latest_session_log_anchor": "anchor-1",
        },
        decision=decision,
        active_root=Path("/x"),
        orchestrator_session="example-orchestrator",
        state_writer_cmd=CUSTOM_CMD,
    )
    obligation = prompt.split("Dispatch obligation (Option-2 orchestrator-poke", 1)[1]
    assert f"`{CUSTOM_CMD} apply --root /x" in obligation
    assert PLACEHOLDER not in prompt


# --- end-to-end: the contract value flows through evaluate_continuation into the spawn prompt ----

def _dispatch_decision() -> StateTransitionDecision:
    return StateTransitionDecision(
        decision="accepted",
        proposal_id="p-dispatch-1",
        reason="ready",
        state_revision=1,
        new_state="dispatch_ready",
        next_required_action="dispatch_next_slice_plan_mode",
    )


def test_evaluate_continuation_default_command_zero_behavior_change(tmp_path: Path) -> None:
    # No [state_writer] section: the spawn prompt's obligation must name the console script exactly,
    # i.e. the pre-step-4 bytes. (No .git -> find_governance_blockers == [], so the happy path runs.)
    root = tmp_path.resolve()
    _write_contract(root)
    _write_todo(root)
    result = evaluate_continuation(
        root=root, decision=_dispatch_decision(), ao_project_id="example-project"
    )
    assert result.decision == "candidate", result.reason
    full_prompt = result.would_run[3]  # ["ao","spawn","--prompt", <full prompt>]
    obligation = full_prompt.split("Dispatch obligation (Option-2 orchestrator-poke", 1)[1]
    assert f"`{DEFAULT_CMD} apply --root" in obligation
    assert PLACEHOLDER not in full_prompt


def test_evaluate_continuation_override_flows_into_spawn_prompt(tmp_path: Path) -> None:
    # The override is read from the contract at `root` and threaded all the way into the spawn prompt;
    # _compact_prompt always preserves the "Dispatch obligation ..." tail, so the assertion is
    # compaction-robust regardless of active_root length.
    root = tmp_path.resolve()
    _write_contract(root, state_writer_section=f'\n[state_writer]\ncommand = "{CUSTOM_CMD}"\n')
    _write_todo(root)
    result = evaluate_continuation(
        root=root, decision=_dispatch_decision(), ao_project_id="example-project"
    )
    assert result.decision == "candidate", result.reason
    full_prompt = result.would_run[3]
    obligation = full_prompt.split("Dispatch obligation (Option-2 orchestrator-poke", 1)[1]
    assert f"`{CUSTOM_CMD} apply --root" in obligation
    assert PLACEHOLDER not in full_prompt
