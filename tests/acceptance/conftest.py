"""Fixtures for the public acceptance harness.

Everything here is BLACK-BOX against the public ``ao-state-writer`` CLI / public
``ao_state_writer`` package. It reads nothing from any private repo and writes no
host-specific absolute path into a committed file: the project root, contract,
TODO, and fake ``ao`` engine shim are all built under pytest's ``tmp_path`` at
RUNTIME (mirroring tests/test_public_core.py::_write_contract), so the committed
harness contains zero leakable paths/ids and passes scripts/public_safety_scan.py.

The harness drives the real lifecycle through the public CLI in two ways:
  * in-process (``apply`` via :class:`StateWriter`) for state/routing assertions, and
  * subprocess (``python -m ao_state_writer.cli ...``) for the spawn-attestation
    slice that must actually exec the fake ``ao`` engine.

The subprocess interpreter is :data:`sys.executable` (the interpreter pytest is
running under) so the harness never hard-codes an interpreter path — a hard-coded
machine-specific interpreter prefix would both be non-portable and trip the public
safety scan's private-interpreter-prefix rule.
"""

from __future__ import annotations

from pathlib import Path
import hashlib
import os
import subprocess
import sys

import pytest

# Public package: the same import surface tests/test_public_core.py uses.
from ao_state_writer.continuation import (
    AUTO_SPAWN_ACTIONS,
    GATED_ACTIONS,
    NON_EXECUTABLE_ACTIONS,
)
from ao_state_writer.writer import (
    CODEX_CC_MODEL,
    CODEX_CC_REASONING_EFFORT,
    StateTransitionProposal,
    StateWriter,
)


# Neutral ids only — never private session prefixes / project ids. The public
# safety scan forbids tokens like the private project slug; these are generic.
FAKE_SPAWN_SESSION_ID = "fake-sess-001"


def _state_paths(root: Path) -> tuple[Path, Path]:
    """Canonical state/ledger paths, identical to ao_state_writer.cli._state_paths."""

    base = root / ".omx" / "state" / "ao-state-writer"
    return base / "state.json", base / "state-transitions.jsonl"


def _action_array(values: tuple[str, ...]) -> str:
    """Render a tuple as a TOML string array, line-per-item.

    Built from the LIVE code tuples (continuation.AUTO_SPAWN_ACTIONS, etc.) so the
    contract's action vocabulary always equals what compat.validate_contract_compat
    matches against. Hard-coding the strings would silently rot into a
    ``contract_action_vocabulary_mismatch`` the day the code tuples change — and
    that is exactly the failure intent-tests must catch, so we derive, not copy.
    """

    items = ",\n".join(f'  "{value}"' for value in values)
    return f"[\n{items}\n]"


def write_contract(root: Path) -> Path:
    """Write a DIRECT_PROJECT_CONTRACT.toml runnable by ``continue``/``dispatch``.

    Superset of test_public_core.py::_write_contract: it additionally emits the
    [dispatch_templates.next_step_plan_mode] template (required by
    continuation._read_next_step_template for the dispatch_next_slice_plan_mode
    prompt) and derives the action arrays from the live code tuples so the public
    matcher accepts them.

    active_root is interpolated from ``root`` AT RUNTIME — never committed.
    """

    contract = root / "DIRECT_PROJECT_CONTRACT.toml"
    contract.write_text(
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
        "\n"
        "[dispatch_templates.next_step_plan_mode]\n"
        'template = """\n'
        "Use canonical next locked action. Do not modify MASTER_PLAN.md.\n"
        "No external submission, merge, release, or production side effect without "
        "exact authorization.\n"
        '"""\n',
        encoding="utf-8",
    )
    return contract


def write_todo(root: Path) -> Path:
    """Write a TODO.md whose '## Current Execution State' carries all 4 fields.

    Mirrors examples/TODO.example.md so continuation._read_compact_current_state
    parses it instead of returning invalid_compact_current_state.
    """

    todo = root / "TODO.md"
    todo.write_text(
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
    return todo


def write_fake_ao(root: Path) -> Path:
    """Write a fake ``ao`` engine onto PATH and return its containing dir.

    Contract (engine-contract map): ``ao spawn ...`` must print exactly ONE line
    ``SESSION=<id>`` on stdout (id matching continuation.AO_SPAWN_SESSION_RE) and
    exit 0; any other subcommand (report/acknowledge/...) just exits 0. This is the
    ONLY engine surface the public core parses, so this ~6-line script fully
    replaces the real @aoagents/ao engine for the spawn-attestation slice.

    It is a portable Python script invoked via ``sys.executable`` so it does not
    depend on a system ``bash`` or a hard-coded shebang interpreter path.
    """

    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    # The real engine prints human spinner/"View:" lines too; we emit one to prove
    # the core ignores everything except the single SESSION= line.
    shim = bin_dir / "ao"
    shim.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "if len(sys.argv) > 1 and sys.argv[1] == 'spawn':\n"
        "    print('View: https://example.invalid/session')\n"
        f"    print('SESSION={FAKE_SPAWN_SESSION_ID}')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return bin_dir


def git_commit_all(root: Path) -> None:
    """Init a git repo and commit governance files clean.

    continuation._governance_blockers runs ``git status --short`` over the
    governance files; uncommitted governance files make ``continue`` return
    blocked unrecognized_governance_dirty (exit 3). A clean commit clears it.
    """

    env = dict(os.environ)
    # Deterministic identity so the harness never depends on host git config.
    env.update(
        GIT_AUTHOR_NAME="harness",
        GIT_AUTHOR_EMAIL="harness@example.invalid",
        GIT_COMMITTER_NAME="harness",
        GIT_COMMITTER_EMAIL="harness@example.invalid",
    )
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True, env=env)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-m", "seed"],
        check=True,
        env=env,
    )


class LifecycleProject:
    """A seeded public project root the harness drives through the lifecycle.

    Holds the tmp root + the PATH-injected env (fake ``ao`` first) and exposes:
      * ``writer()`` — an in-process StateWriter on the canonical state paths, and
      * ``cli(*args, env_overrides=...)`` — a subprocess run of the public CLI.
    """

    def __init__(self, root: Path, ao_bin_dir: Path) -> None:
        self.root = root
        self.ao_bin_dir = ao_bin_dir

    def writer(self) -> StateWriter:
        state_path, ledger_path = _state_paths(self.root)
        return StateWriter(state_path=state_path, ledger_path=ledger_path)

    def cli_env(self, **overrides: str) -> dict[str, str]:
        env = dict(os.environ)
        # Fake ao first on PATH so subprocess `continue`/`dispatch` exec it.
        env["PATH"] = os.pathsep.join([str(self.ao_bin_dir), env.get("PATH", "")])
        # Make the public package importable without install, like pyproject's
        # pytest pythonpath=['src'] does in-process.
        src_dir = Path(__file__).resolve().parents[2] / "src"
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            os.pathsep.join([str(src_dir), existing]) if existing else str(src_dir)
        )
        # Strip any inherited AO_* identity so each step controls its own env.
        for key in ("AO_CALLER_TYPE", "AO_SESSION_ID", "AO_SESSION", "AO_PROJECT_ID"):
            env.pop(key, None)
        env.update(overrides)
        return env

    def cli(self, *args: str, **env_overrides: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "ao_state_writer.cli", *args],
            cwd=str(self.root),
            capture_output=True,
            text=True,
            check=False,
            env=self.cli_env(**env_overrides),
        )


@pytest.fixture()
def lifecycle_project(tmp_path: Path) -> LifecycleProject:
    """A git-committed public project root with a runnable contract/TODO + fake ao.

    Built entirely under tmp_path at runtime. Resolve the root so active_root
    path-equality holds on macOS (/tmp -> /private/tmp realpath).
    """

    root = tmp_path.resolve()
    write_contract(root)
    write_todo(root)
    ao_bin_dir = write_fake_ao(root)
    git_commit_all(root)
    return LifecycleProject(root=root, ao_bin_dir=ao_bin_dir)


# --- Shared dispatchable-close seed (M1 forward-port regression suites) ---------------------------

# The orchestrator identity write_contract() declares (continuation_policy.orchestrator_session) —
# the env shape the CLI proof gate validates against. Neutral ids only.
ORCHESTRATOR_ENV = {"AO_CALLER_TYPE": "orchestrator", "AO_SESSION_ID": "example-orchestrator"}


def make_proposal(**overrides) -> StateTransitionProposal:
    """A small_chapter proposal with neutral defaults; override what the step needs."""
    base = dict(
        proposal_id="p-default",
        target_kind="small_chapter",
        target_id="chapter-1",
        base_state_revision=0,
        requested_state="evidence_pending",
        actor_role="implementer",
        evidence_refs=["evidence#1"],
    )
    base.update(overrides)
    return StateTransitionProposal(**base)


def seed_dispatchable_close(project: LifecycleProject, monkeypatch) -> StateWriter:
    """Drive chapter-1 to an accepted 'closed' decision whose next required action is
    dispatch_next_slice_plan_mode (proposal ``p-close-1``) and return the in-process writer.

    The shared seed the operator-pause (P5) and lease-accounting (audit-fix) regression suites
    start from: seed -> codex_cc receipt -> receipt-gated close, the same drive
    test_project_lifecycle exercises step-by-step with full intent assertions.
    """
    writer = project.writer()
    seed = writer.apply(make_proposal(proposal_id="p-seed-1"))
    assert seed.decision == "accepted", seed
    monkeypatch.setenv("AO_CALLER_TYPE", "codex_cc")
    transcript = project.root / "reports" / "codex-cc-receipts" / "p-cc-1.txt"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("codex-cc transcript ok", encoding="utf-8")
    transcript_sha = hashlib.sha256(transcript.read_bytes()).hexdigest()
    receipt = writer.apply(
        make_proposal(
            proposal_id="p-cc-1",
            base_state_revision=1,
            actor_role="codex_cc",
            evidence_refs=["transcript#1"],
            review_scope="codex_cc",
            verdict="pass",
            model=CODEX_CC_MODEL,
            reasoning_effort=CODEX_CC_REASONING_EFFORT,
            codex_cc_transcript_sha256=transcript_sha,
            codex_cc_transcript_artifact_ref="artifact:reports/codex-cc-receipts/p-cc-1.txt",
        )
    )
    assert receipt.decision == "accepted", receipt
    monkeypatch.delenv("AO_CALLER_TYPE", raising=False)
    close = writer.apply(
        make_proposal(
            proposal_id="p-close-1",
            base_state_revision=2,
            requested_state="closed",
            evidence_refs=["evidence#close"],
        )
    )
    assert close.decision == "accepted", close
    assert close.next_required_action == "dispatch_next_slice_plan_mode", close
    return writer
