from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re
import shlex
import subprocess

from .compat import (
    InvalidCanonicalFilename,
    canonical_todo_file,
    read_contract_active_root,
    read_contract_section,
    read_contract_string,
    validate_contract_compat,
)
from .preflight import find_governance_blockers
from .todo import COMPACT_CURRENT_STATE_FIELDS, CURRENT_STATE_HEADING_RE, validate_compact_current_state
from .writer import StateTransitionDecision as ContinuationDecision
from .writer import CODEX_CC_MODEL, CODEX_CC_REASONING_EFFORT


DISPATCH_NEXT_SLICE_ACTION = "dispatch_next_slice_plan_mode"
# Writer-emitted obligation tokens the `continue` command may consume by spawning the next agent.
# These are the only next_required_action values that ever reach this consumer (watchdog tokens
# flow in as repair_active via the applied timeout blocker, not directly).
AUTO_SPAWN_ACTIONS = (
    "dispatch_next_slice_plan_mode",
    "codex_cc_review",
    "repair_active",
    "state_writer_closure",
    "major_closure_candidate",
)
# Tokens that may auto-prepare but whose terminal action is owner-proxy gated: never
# auto-spawned without a live orchestrator authorization.
GATED_ACTIONS = (
    "escalated_review",
)
# Tokens that are deliberately not executable.  The CLI preflight surfaces these as
# owner_proxy_convergence_required envelopes so AO cannot silently treat them as queues,
# gated actions, or spawnable work.
NON_EXECUTABLE_ACTIONS = (
    "repair_attempts_exhausted",
)
# Persistently-broken review environment, escalated past the cumulative environment-fault bound.
# OWNER-VISIBLE and terminal-until-superseded: NOT auto-spawnable, NOT a gated authorization, NOT
# routed through convergence (that is for genuine content exhaustion); a later successful review
# receipt supersedes it by revision. Kept as its OWN category — never folded into
# NON_EXECUTABLE_ACTIONS, because compat binds non_executable_actions AND owner_proxy_convergence_actions
# to that tuple by exact equality, so reusing it would conflate an env outage with convergence.
ENVIRONMENT_ESCALATION_ACTIONS = (
    "review_environment_unavailable",
)
AO_PROMPT_LIMIT = 4096
AO_PROMPT_SOFT_LIMIT = 3800
AO_SPAWN_TIMEOUT_SECONDS = 120
AO_SPAWN_SESSION_RE = re.compile(r"^SESSION=([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})$")
CODEX_CC_COMMAND_TEXT = (
    f"codex exec -m {CODEX_CC_MODEL} "
    f"-c 'model_reasoning_effort=\"{CODEX_CC_REASONING_EFFORT}\"' -s read-only"
)

# Option-2 dispatch obligation (orchestrator-poke flow). Each spawned worker records ONLY
# its own transition (no --continue-on-next-action), commits, surfaces an unconsumed dispatch
# obligation via `ao report needs_input`, and STOPS. It never runs continue/dispatch/ao spawn
# itself: the orchestrator-poke notifier wakes the orchestrator, which reconciles from canonical
# state and runs `ao-state-writer continue`/`dispatch` to spawn the next slice.
# <active_root>, <orchestrator_session>, and <state_writer_cmd> are substituted at render time.
# <state_writer_cmd> defaults to the `ao-state-writer` console script (public packaging) and can be
# overridden via the contract `[state_writer] command` for a source-checkout / mid-migration invocation.
DISPATCH_OBLIGATION_INSTRUCTION = (
    "Dispatch obligation (Option-2 orchestrator-poke, REQUIRED after finishing this slice):\n"
    "1. Submit the next canonical proposal for this action through `<state_writer_cmd> apply --root <active_root> "
    "--proposal <your_proposal.json>` "
    "(do NOT pass --continue-on-next-action), then git commit your governance/TODO/session-log changes.\n"
    "2. Run `ao report needs_input` and STOP. Do NOT run `<state_writer_cmd> continue`/`dispatch` or "
    "`ao spawn` yourself. The orchestrator-poke notifier wakes the orchestrator, which reconciles "
    "from canonical state and runs `<state_writer_cmd> continue` to dispatch the next slice.\n"
    "Do NOT `ao spawn` successors yourself outside of `continue`."
)
CODEX_CC_REVIEW_OBLIGATION_INSTRUCTION = (
    "Dispatch obligation (Option-2 orchestrator-poke, REQUIRED after finishing this Codex cc review):\n"
    "1. Run the scoped Codex cc review exactly as specified above, capturing its transcript.\n"
    "2. Submit the Codex cc receipt proposal through `<state_writer_cmd> apply --root <active_root> "
    "--proposal <your_receipt_proposal.json>` (do NOT pass --continue-on-next-action), then git commit "
    "the receipt/state evidence.\n"
    "3. Run `ao report needs_input` and STOP. Do NOT run `<state_writer_cmd> continue`/`dispatch` or "
    "`ao spawn` yourself. The orchestrator-poke notifier wakes the orchestrator, which reconciles "
    "from canonical state and runs `<state_writer_cmd> continue` to dispatch the next slice.\n"
    "Do NOT `ao spawn` successors yourself outside of `continue`."
)

# Shared escalation taxonomy for both authorization blocks. The only hard human-owner gate is a
# MASTER_PLAN.md edit; merge/release/external-submission/production side effects ride on
# AO orchestrator Owner-Proxy authorization (not a human wait) given exact target identity + proof; any
# other situation where authority or evidence cannot be verified escalates to the Owner-Proxy.
OWNER_PROXY_ESCALATION_TAXONOMY = (
    "The only hard human-owner gate is editing MASTER_PLAN.md; wait for the human owner "
    "ONLY for that exact edit. Merge/release, external/review-package submission, and "
    "production/external side effects do NOT wait for the human owner: they require this AO orchestrator "
    "Owner-Proxy authorization plus exact target identity and action-specific proof — proceed when that "
    "proof is present, otherwise fail closed and report the missing proof to the Owner-Proxy (still not "
    "a human wait unless it is a Master Plan edit). For any other novel or unanticipated situation where "
    "you cannot verify you have the authority or evidence to proceed, do NOT push through and do NOT "
    "fabricate a result: stop and escalate to the Owner-Proxy by emitting a typed blocker or running "
    "`ao report needs-input`. When in doubt, escalate rather than guess."
)

ACTION_DIRECTIVES = {
    "codex_cc_review": (
        "Run the scoped Codex cc review for the current target recorded in TODO.md "
        "Current Execution State. Record a receipt proposal with the model and reasoning_effort below. "
        "Capture the `codex exec` output to a transcript file under reports/codex-cc-receipts/, compute "
        "its bare lowercase sha256 hexdigest with no `sha256:` prefix, and include that digest as "
        "codex_cc_transcript_sha256 plus the repo-local artifact ref as codex_cc_transcript_artifact_ref "
        "(exact field names) in the receipt proposal, or ao-state-writer "
        "rejects the pass-type receipt as missing_codex_cc_transcript or "
        "missing_codex_cc_transcript_artifact. "
        "If the contract requires an independent reviewer, do not review your own implementation work."
    ),
    "repair_active": (
        "Repair the typed blocker for the current target recorded in TODO.md Current "
        "Execution State. Fix the root cause, re-run local verification, then submit the correct "
        "review proposal — the choice depends on target_kind: "
        "(a) major_chapter: submit a escalated_review_pending proposal so the escalated review external review "
        "re-runs (NOT evidence_pending — that would route to Codex cc and bypass escalated review entirely); "
        "(b) small_chapter: submit an evidence_pending proposal so the Codex cc review re-runs. "
        "Do not close the chapter yourself."
    ),
    "state_writer_closure": (
        "Perform the small-chapter closure sync for the current target recorded in TODO.md "
        "Current Execution State. The canonical close is receipt-gated by ao-state-writer (it rejects a "
        "close without an accepted codex_cc receipt), so confirm the receipt then submit the closed "
        "proposal. Closure scope is todo_only unless the contract authorizes more."
    ),
    "major_closure_candidate": (
        "Perform the major-chapter closure for the current target recorded in TODO.md "
        "Current Execution State. The escalated review external review has passed (receipt accepted by "
        "ao-state-writer). Submit the closed proposal and sync governance. Do not modify the Master Plan "
        "beyond the scope of this chapter. Closure scope is per contract."
    ),
}


@dataclass(frozen=True)
class ContinuationResult:
    decision: str
    reason: str
    next_required_action: str | None = None
    ao_project_id: str | None = None
    would_run: list[str] | None = None
    spawn_cwd: str | None = None
    prompt_preview: str | None = None
    blockers: list[str] | None = None
    returncode: int | None = None
    stdout: str | None = None
    stderr: str | None = None
    spawn_session_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def evaluate_continuation(
    *,
    root: Path,
    decision: ContinuationDecision,
    ao_project_id: str | None = None,
    current_state_override: dict[str, str] | None = None,
) -> ContinuationResult:
    """Build one AO-native continuation action from an accepted state-writer decision."""

    if decision.decision != "accepted":
        return ContinuationResult(
            decision="skipped",
            reason="state_transition_not_accepted",
            next_required_action=decision.next_required_action,
            ao_project_id=ao_project_id,
        )

    if decision.replayed:
        return ContinuationResult(
            decision="skipped",
            reason="already_consumed",
            next_required_action=decision.next_required_action,
            ao_project_id=ao_project_id,
        )

    contract_error = validate_contract_compat(
        root,
        auto_spawn_actions=AUTO_SPAWN_ACTIONS,
        gated_actions=GATED_ACTIONS,
        non_executable_actions=NON_EXECUTABLE_ACTIONS,
        environment_escalation_actions=ENVIRONMENT_ESCALATION_ACTIONS,
    )
    if contract_error is not None:
        return ContinuationResult(
            decision="blocked",
            reason=contract_error,
            next_required_action=decision.next_required_action,
            ao_project_id=ao_project_id,
            blockers=[contract_error],
        )

    # Root identity is a compatibility boundary, not merely a spawn detail:
    # check it before even returning gated so missing contract proof cannot
    # masquerade as a normal owner-proxy authorization wait.
    active_root = read_contract_active_root(root)
    if active_root is None:
        return ContinuationResult(
            decision="blocked",
            reason="missing_ao_active_root",
            next_required_action=decision.next_required_action,
            ao_project_id=ao_project_id,
            blockers=["missing_ao_active_root"],
        )

    if decision.next_required_action in GATED_ACTIONS:
        return ContinuationResult(
            decision="gated",
            reason="requires_owner_or_human_gate",
            next_required_action=decision.next_required_action,
            ao_project_id=ao_project_id,
        )

    if decision.next_required_action in ENVIRONMENT_ESCALATION_ACTIONS:
        # Persistently-broken review environment: owner-visible, NOT convergence, NOT spawnable.
        # Surfaced as a blocked obligation so `continue` does not mistake it for nothing_to_continue;
        # a later successful review receipt supersedes it by revision.
        return ContinuationResult(
            decision="blocked",
            reason="review_environment_unavailable",
            next_required_action=decision.next_required_action,
            ao_project_id=ao_project_id,
            blockers=["review_environment_unavailable"],
        )

    if decision.next_required_action in NON_EXECUTABLE_ACTIONS:
        return ContinuationResult(
            decision="blocked",
            reason="owner_proxy_convergence_required",
            next_required_action=decision.next_required_action,
            ao_project_id=ao_project_id,
            blockers=["owner_proxy_convergence_required"],
        )

    if decision.next_required_action not in AUTO_SPAWN_ACTIONS:
        return ContinuationResult(
            decision="blocked",
            reason="unsupported_next_required_action",
            next_required_action=decision.next_required_action,
            ao_project_id=ao_project_id,
            blockers=["unsupported_next_required_action"],
        )

    governance_blockers = find_governance_blockers(root)
    if governance_blockers:
        return ContinuationResult(
            decision="blocked",
            reason="unrecognized_governance_dirty",
            next_required_action=decision.next_required_action,
            ao_project_id=ao_project_id,
            blockers=governance_blockers,
        )

    current_state, current_state_errors = _read_compact_current_state(root)
    if current_state_errors:
        return ContinuationResult(
            decision="blocked",
            reason="invalid_compact_current_state",
            next_required_action=decision.next_required_action,
            ao_project_id=ao_project_id,
            blockers=current_state_errors,
        )

    # Stale-TODO-mirror guard + override acceptance (the engine-side driver that DETECTS staleness and
    # BUILDS the canonical override lives in cli.py and lands with Group E). The guard only fires when
    # the TODO's current-state values carry an explicit "state-writer rev: N" token older than the
    # accepted decision revision: with no token (the current public flow) current_state_revision is None
    # and this whole block is skipped — zero behavior change. When a stale rev IS present, fail closed
    # ("stale_compact_current_state") rather than dispatch on stale data, unless a canonical override is
    # supplied to reconcile it. An override is only valid when it actually superseded a stale read.
    current_state_revision = _compact_current_state_revision(current_state)
    override_applied = False
    if (
        decision.decision == "accepted"
        and isinstance(decision.state_revision, int)
        and current_state_revision is not None
        and current_state_revision < decision.state_revision
    ):
        if current_state_override is not None:
            current_state = dict(current_state_override)
            override_applied = True
        else:
            return ContinuationResult(
                decision="blocked",
                reason="stale_compact_current_state",
                next_required_action=decision.next_required_action,
                ao_project_id=ao_project_id,
                blockers=[
                    f"todo_current_state_rev:{current_state_revision}",
                    f"state_writer_rev:{decision.state_revision}",
                ],
            )

    if current_state_override is not None:
        if not override_applied:
            return ContinuationResult(
                decision="blocked",
                reason="unexpected_current_state_override",
                next_required_action=decision.next_required_action,
                ao_project_id=ao_project_id,
                blockers=["unexpected_current_state_override"],
            )
        override_errors = _validate_current_state_override(current_state)
        if override_errors:
            return ContinuationResult(
                decision="blocked",
                reason="invalid_compact_current_state_override",
                next_required_action=decision.next_required_action,
                ao_project_id=ao_project_id,
                blockers=override_errors,
            )

    # Clip verbose canonical field values so the critical structural instructions
    # (owner-proxy authorization, dispatch obligation, hard boundary) always fit within the AO
    # prompt budget and are never truncated away. The spawned agent reads the full canonical files anyway.
    current_state = {key: _clip(value) for key, value in current_state.items()}

    template = _read_next_step_template(root)
    if template is None:
        return ContinuationResult(
            decision="blocked",
            reason="missing_next_step_plan_mode_template",
            next_required_action=decision.next_required_action,
            ao_project_id=ao_project_id,
            blockers=["missing_contract_template:dispatch_templates.next_step_plan_mode.template"],
        )

    orchestrator_session = _read_orchestrator_session(root)
    # Resolve the engine command once here (root is in scope) and thread the string down; the
    # obligation block defaults to the `ao-state-writer` console script when [state_writer] command
    # is absent. Resolving from `root` (the contract location), not `active_root`, is intentional:
    # the contract that authorizes this continuation is the one that names the command.
    state_writer_cmd = _state_writer_command(root)
    prompt = _render_prompt_for_action(
        decision.next_required_action,
        template=template,
        current_state=current_state,
        decision=decision,
        active_root=active_root,
        orchestrator_session=orchestrator_session,
        state_writer_cmd=state_writer_cmd,
    )
    if len(prompt) > AO_PROMPT_LIMIT:
        return ContinuationResult(
            decision="blocked",
            reason="prompt_too_long",
            next_required_action=decision.next_required_action,
            ao_project_id=ao_project_id,
            blockers=[f"prompt_length:{len(prompt)}"],
        )
    return ContinuationResult(
        decision="candidate",
        reason="ready",
        next_required_action=decision.next_required_action,
        ao_project_id=ao_project_id,
        would_run=["ao", "spawn", "--prompt", prompt],
        spawn_cwd=str(active_root),
        prompt_preview=_preview(prompt),
    )


def continue_after_apply(
    *,
    root: Path,
    decision: ContinuationDecision,
    ao_project_id: str | None = None,
    dry_run: bool = False,
    current_state_override: dict[str, str] | None = None,
) -> ContinuationResult:
    candidate = evaluate_continuation(
        root=root,
        decision=decision,
        ao_project_id=ao_project_id,
        current_state_override=current_state_override,
    )
    if candidate.decision != "candidate":
        return candidate

    if dry_run:
        return ContinuationResult(
            decision="would_spawn",
            reason="dry_run",
            next_required_action=candidate.next_required_action,
            ao_project_id=ao_project_id,
            would_run=candidate.would_run,
            spawn_cwd=candidate.spawn_cwd,
            prompt_preview=candidate.prompt_preview,
        )

    try:
        completed = subprocess.run(
            candidate.would_run or [],
            cwd=Path(candidate.spawn_cwd or root),
            capture_output=True,
            text=True,
            check=False,
            timeout=AO_SPAWN_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        return ContinuationResult(
            decision="spawn_failed",
            reason="ao_command_not_found",
            next_required_action=candidate.next_required_action,
            ao_project_id=ao_project_id,
            would_run=candidate.would_run,
            spawn_cwd=candidate.spawn_cwd,
            prompt_preview=candidate.prompt_preview,
            returncode=127,
            stderr=str(exc),
        )
    except subprocess.TimeoutExpired as exc:
        return ContinuationResult(
            decision="spawn_failed",
            reason="ao_spawn_timeout",
            next_required_action=candidate.next_required_action,
            ao_project_id=ao_project_id,
            would_run=candidate.would_run,
            spawn_cwd=candidate.spawn_cwd,
            prompt_preview=candidate.prompt_preview,
            returncode=124,
            stderr=str(exc),
        )

    if completed.returncode != 0:
        return ContinuationResult(
            decision="spawn_failed",
            reason="ao_spawn_failed",
            next_required_action=candidate.next_required_action,
            ao_project_id=ao_project_id,
            would_run=candidate.would_run,
            spawn_cwd=candidate.spawn_cwd,
            prompt_preview=candidate.prompt_preview,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    spawn_session_id = _extract_spawn_session_id(completed.stdout, completed.stderr)
    if spawn_session_id is None:
        return ContinuationResult(
            decision="spawned_without_session_attestation",
            reason="ao_spawn_missing_session_attestation",
            next_required_action=candidate.next_required_action,
            ao_project_id=ao_project_id,
            would_run=candidate.would_run,
            spawn_cwd=candidate.spawn_cwd,
            prompt_preview=candidate.prompt_preview,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    return ContinuationResult(
        decision="spawned",
        reason="ao_spawn_completed",
        next_required_action=candidate.next_required_action,
        ao_project_id=ao_project_id,
        would_run=candidate.would_run,
        spawn_cwd=candidate.spawn_cwd,
        prompt_preview=candidate.prompt_preview,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        spawn_session_id=spawn_session_id,
    )


def _extract_spawn_session_id(stdout: str | None, stderr: str | None = None) -> str | None:
    matches: list[str] = []
    for stream in (stdout, stderr):
        if not stream:
            continue
        for line in stream.splitlines():
            match = AO_SPAWN_SESSION_RE.fullmatch(line.strip())
            if match:
                matches.append(match.group(1))
    if len(matches) != 1:
        return None
    return matches[0]


def _read_compact_current_state(root: Path) -> tuple[dict[str, str], list[str]]:
    try:
        todo_name = canonical_todo_file(root)
    except InvalidCanonicalFilename as exc:
        return {}, [f"invalid_canonical_filename:{exc.key}"]
    todo_path = root / todo_name
    if not todo_path.exists():
        return {}, [f"missing_todo_file:{todo_name}"]

    todo_text = todo_path.read_text(encoding="utf-8")
    marker_matches = list(CURRENT_STATE_HEADING_RE.finditer(todo_text))
    if len(marker_matches) == 0:
        return {}, ["missing_current_execution_state_section"]
    if len(marker_matches) > 1:
        # Reject ambiguity: two "## Current Execution State" sections must fail closed rather than
        # silently reading the first (matches the TODO-repair fail-closed guard in todo.py).
        return {}, ["multiple_current_execution_state_sections"]
    rest = todo_text[marker_matches[0].end() :]
    next_heading = rest.find("\n## ")
    block = rest.strip() if next_heading == -1 else rest[:next_heading].strip()

    errors = validate_compact_current_state(block)
    values: dict[str, str] = {}
    for line in block.splitlines():
        if not line.startswith("- ") or ":" not in line:
            continue
        name, value = line[2:].split(":", 1)
        name = name.strip()
        if name in COMPACT_CURRENT_STATE_FIELDS:
            values[name] = value.strip()

    for field in COMPACT_CURRENT_STATE_FIELDS:
        if not values.get(field) and f"missing_current_state_field:{field}" not in errors:
            errors.append(f"empty_current_state_field:{field}")

    return values, errors


def _validate_current_state_override(values: dict[str, str]) -> list[str]:
    # A canonical override (built by the Group E driver from state.json when the TODO mirror is stale)
    # must still carry every required compact field, non-empty. Extra keys (e.g. todo_mirror_repair_note)
    # are allowed and pass through to rendering.
    errors: list[str] = []
    for field in COMPACT_CURRENT_STATE_FIELDS:
        if field not in values:
            errors.append(f"missing_current_state_override_field:{field}")
        elif not values[field].strip():
            errors.append(f"empty_current_state_override_field:{field}")
    return errors


def _compact_current_state_revision(values: dict[str, str]) -> int | None:
    revisions: list[int] = []
    for value in values.values():
        for match in re.finditer(
            r"\bstate-writer\s+rev(?:ision)?\s*[:#-]?\s*(\d+)\b",
            value,
            flags=re.IGNORECASE,
        ):
            revisions.append(int(match.group(1)))
    # Best-effort TODO mirror freshness check: use the highest explicit state-writer
    # token so historical lower-rev mentions do not mask a current-rev marker.
    return max(revisions) if revisions else None


def _read_next_step_template(root: Path) -> str | None:
    section = read_contract_section(root, "dispatch_templates.next_step_plan_mode")
    if section is None:
        return None
    return _extract_triple_quoted_value(section, "template")


def _read_orchestrator_session(root: Path) -> str:
    default = "example-orchestrator"
    value = read_contract_string(root, "continuation_policy", "orchestrator_session")
    return value or default


def _state_writer_command(root: Path) -> str:
    # The dispatched worker invokes the engine through this command in the obligation block. Public
    # ships `ao-state-writer` as a console_script (see pyproject.toml), so a normal `pip install`
    # makes that the correct, brand-neutral default. A project running the engine from a source
    # checkout (or mid-migration) may override it via the contract `[state_writer] command` — e.g. a
    # `PYTHONPATH=<src> python -m ao_state_writer.cli` invocation — WITHOUT editing the engine. An
    # absent key falls back to the default, which renders the obligation identically (zero behavior
    # change). Porting LIVE's machine/product-coupled command form here would regress the public
    # engine and is deliberately NOT done.
    value = read_contract_string(root, "state_writer", "command")
    # Strip here (not just `value or default`): read_contract_string's tomllib path collapses a
    # blank/whitespace value to None, but its regex fallback (used on Python < 3.11, where tomllib is
    # absent) returns that blank value VERBATIM. Stripping makes a whitespace-only command degrade to
    # the default on BOTH read paths — version-independent — and is a no-op for a real command.
    command = value.strip() if isinstance(value, str) else ""
    return command or "ao-state-writer"


def _extract_triple_quoted_value(section: str, key: str) -> str | None:
    lines = section.splitlines()
    prefix = f"{key} = "
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith(prefix):
            continue
        value = stripped[len(prefix) :]
        if not value.startswith('"""'):
            return None
        value = value[3:]
        if '"""' in value:
            return value.split('"""', 1)[0]
        captured = [value] if value else []
        for follow in lines[index + 1 :]:
            if '"""' in follow:
                captured.append(follow.split('"""', 1)[0])
                return "\n".join(captured).strip()
            captured.append(follow)
        return None
    return None


def _dispatch_obligation(
    *,
    action: str,
    active_root: Path,
    orchestrator_session: str,
    state_writer_cmd: str = "ao-state-writer",
) -> str:
    template = (
        CODEX_CC_REVIEW_OBLIGATION_INSTRUCTION
        if action == "codex_cc_review"
        else DISPATCH_OBLIGATION_INSTRUCTION
    )
    return (
        template.replace("<active_root>", str(active_root))
        .replace("<orchestrator_session>", orchestrator_session)
        .replace("<state_writer_cmd>", state_writer_cmd)
    )


def _git_output(root: Path, *args: str) -> str | None:
    try:
        process = subprocess.Popen(
            ["git", "-C", str(root), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        stdout, _ = process.communicate(timeout=10)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        try:
            process.kill()
        except UnboundLocalError:
            pass
        return None
    if process.returncode != 0:
        return None
    return stdout.strip() or None


def _source_baseline_boundary(active_root: Path) -> str:
    commit = _git_output(active_root, "rev-parse", "HEAD") or "unknown"
    branch = _git_output(active_root, "branch", "--show-current")
    required_ref = branch or "HEAD"
    fetch_ref = shlex.quote(required_ref)
    active_root_arg = shlex.quote(str(active_root))
    if branch:
        repair_command = (
            f"`git fetch {active_root_arg} {fetch_ref}` then "
            f"`git checkout -B {shlex.quote(branch)} FETCH_HEAD`; reverify."
        )
    else:
        repair_command = (
            f"`git fetch {active_root_arg} HEAD` then "
            "`git switch --detach FETCH_HEAD`; reverify."
        )
    return (
        "Source baseline boundary (mandatory before reading repo-local TODO/state files):\n"
        f"- canonical_active_root: {active_root}\n"
        f"- required_source_ref: {required_ref}\n"
        f"- required_source_commit: {commit}\n"
        "- Before reading repo-local TODO/state: verify `git merge-base --is-ancestor "
        "<required_source_commit> HEAD`.\n"
        f"- If missing and clean: {repair_command}\n"
        "- If dirty/fetch fails/still missing: typed blocker `spawn_base_commit_mismatch`; do not read stale files.\n"
        "- This aligns source bytes only; ao-state-writer at canonical_active_root remains the sole writer.\n"
    )


def _render_prompt(
    *,
    template: str,
    current_state: dict[str, str],
    decision: ContinuationDecision,
    active_root: Path,
    orchestrator_session: str,
    state_writer_cmd: str = "ao-state-writer",
) -> str:
    template_summary = _compact_template_summary(template)
    prompt = (
        "Execute the current AO next_locked_action now.\n\n"
        "Source of truth:\n"
        "- Read DIRECT_PROJECT_CONTRACT.toml, TODO.md Current Execution State, "
        "and SESSION_LOG.md only as needed for evidence.\n"
        "- Do not infer from stale rollout titles, old chapter templates, or historical Direct/Codex residue.\n\n"
        f"{_source_baseline_boundary(active_root)}\n"
        f"Contract dispatch boundary summary: {template_summary}\n\n"
        "AO state-writer continuation context:\n"
        f"- proposal_id: {decision.proposal_id}\n"
        f"- next_required_action: {decision.next_required_action}\n"
        f"- current_phase: {current_state['current_phase']}\n"
        f"- next_locked_action: {current_state['next_locked_action']}\n"
        f"- review_gate_state: {current_state['review_gate_state']}\n"
        f"- latest_session_log_anchor: {current_state['latest_session_log_anchor']}\n\n"
        f"{_todo_mirror_note(current_state)}"
        "AO_ORCHESTRATOR_OWNER_PROXY_DISPATCH_AUTHORIZATION:\n"
        "This continuation spawn is the AO orchestrator Owner-proxy authorization to execute the "
        "canonical next_locked_action. Start the dispatched next_locked_action now; do not pause "
        "for another ordinary owner prompt solely because older TODO text says not to auto-start. "
        "For non-Master-Plan actions, do NOT wait for the human owner. If required evidence is missing, "
        "stop and escalate to the Owner-Proxy with a typed blocker instead of guessing.\n"
        f"{OWNER_PROXY_ESCALATION_TAXONOMY}\n\n"
        f"{_dispatch_obligation(action=decision.next_required_action or '', active_root=active_root, orchestrator_session=orchestrator_session, state_writer_cmd=state_writer_cmd)}\n\n"
        "Hard boundary: do not modify MASTER_PLAN.md unless the current AO issue "
        "contains explicit human-owner authorization for that exact Master Plan edit. Use AO native "
        "issue-driven worktree flow and close future state transitions only through ao-state-writer."
    )
    return prompt if len(prompt) <= AO_PROMPT_SOFT_LIMIT else _compact_prompt(prompt)


def _clip(value: str, limit: int = 300) -> str:
    value = (value or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _render_prompt_for_action(
    action: str,
    *,
    template: str,
    current_state: dict[str, str],
    decision: ContinuationDecision,
    active_root: Path,
    orchestrator_session: str,
    state_writer_cmd: str = "ao-state-writer",
) -> str:
    if action == DISPATCH_NEXT_SLICE_ACTION:
        return _render_prompt(
            template=template,
            current_state=current_state,
            decision=decision,
            active_root=active_root,
            orchestrator_session=orchestrator_session,
            state_writer_cmd=state_writer_cmd,
        )
    return _render_action_prompt(
        action,
        current_state=current_state,
        decision=decision,
        active_root=active_root,
        orchestrator_session=orchestrator_session,
        state_writer_cmd=state_writer_cmd,
    )


def _render_action_prompt(
    action: str,
    *,
    current_state: dict[str, str],
    decision: ContinuationDecision,
    active_root: Path,
    orchestrator_session: str,
    state_writer_cmd: str = "ao-state-writer",
) -> str:
    parts = [
        f"Execute the current AO next_required_action now: {action}.",
        "",
        ACTION_DIRECTIVES[action],
        "",
        "Source of truth:",
        "- Read DIRECT_PROJECT_CONTRACT.toml, TODO.md Current Execution State, and "
        "SESSION_LOG.md only as needed for evidence.",
        "- Resolve the target from the canonical Current Execution State, not from stale rollout titles "
        "or historical Direct/Codex residue.",
        "",
        _source_baseline_boundary(active_root).rstrip(),
        "",
        "AO state-writer continuation context:",
        f"- proposal_id: {decision.proposal_id}",
        f"- next_required_action: {decision.next_required_action}",
        f"- current_phase: {current_state['current_phase']}",
        f"- next_locked_action: {current_state['next_locked_action']}",
        f"- review_gate_state: {current_state['review_gate_state']}",
        f"- latest_session_log_anchor: {current_state['latest_session_log_anchor']}",
        *_todo_mirror_note_lines(current_state),
        *(
            [f"- blocker_code: {decision.blocker_code}"]
            if action == "repair_active" and decision.blocker_code
            else []
        ),
    ]
    if action == "codex_cc_review":
        parts += [
            "",
            "Codex cc scoped review requirement:",
            f"- Use exactly `{CODEX_CC_COMMAND_TEXT}` for the Codex cc review.",
            f"- The receipt/proposal must record model={CODEX_CC_MODEL} and "
            f"reasoning_effort={CODEX_CC_REASONING_EFFORT}; state-writer rejects silent default inheritance.",
            "- Capture the `codex exec` output to a transcript file under reports/codex-cc-receipts/, "
            "compute its bare lowercase sha256 hexdigest with no `sha256:` prefix, and include that digest "
            "as codex_cc_transcript_sha256 plus the repo-local artifact ref as "
            "codex_cc_transcript_artifact_ref (exact field names) in the receipt proposal; "
            "a pass-type receipt without these is rejected as missing_codex_cc_transcript or "
            "missing_codex_cc_transcript_artifact.",
        ]
    parts += [
        "",
        "AO_ORCHESTRATOR_OWNER_PROXY_DISPATCH_AUTHORIZATION:",
        "This continuation spawn is the AO orchestrator Owner-proxy authorization to execute the "
        f"canonical {action} now. Start it now; do not pause for another ordinary owner prompt.",
        OWNER_PROXY_ESCALATION_TAXONOMY,
        "",
        _dispatch_obligation(
            action=action,
            active_root=active_root,
            orchestrator_session=orchestrator_session,
            state_writer_cmd=state_writer_cmd,
        ),
        "",
        "Hard boundary: do not modify MASTER_PLAN.md unless the current AO issue contains "
        "explicit human-owner authorization for that exact edit. Use AO native issue-driven worktree flow "
        "and close future state transitions only through ao-state-writer.",
    ]
    prompt = "\n".join(parts)
    return prompt if len(prompt) <= AO_PROMPT_SOFT_LIMIT else _compact_prompt(prompt)


def _preview(prompt: str, limit: int = 2000) -> str:
    if len(prompt) <= limit:
        return prompt
    return prompt[: limit - 3] + "..."


def _todo_mirror_note(current_state: dict[str, str]) -> str:
    # Rendering layer for the stale-TODO-mirror self-heal. When the engine (cli.py, Group E) detects
    # the TODO mirror is stale/ambiguous vs canonical state.json, it reconciles current_state from the
    # canonical state-writer context and injects a `todo_mirror_repair_note` so the spawned worker is
    # told NOT to trust the stale TODO. Until that driver lands, the public flow never sets the key, so
    # this renders nothing (no-op / zero behavior change).
    note = current_state.get("todo_mirror_repair_note")
    if not note:
        return ""
    return f"TODO mirror repair context:\n- {note}\n\n"


def _todo_mirror_note_lines(current_state: dict[str, str]) -> list[str]:
    # List form of _todo_mirror_note for the parts-joined action prompt; same semantics, empty when no
    # repair note is present.
    note = current_state.get("todo_mirror_repair_note")
    if not note:
        return []
    return ["", "TODO mirror repair context:", f"- {note}"]


def _compact_template_summary(template: str) -> str:
    important = [
        "use canonical next locked action",
        "do not modify MASTER_PLAN.md",
        "no external submission, merge, release, or production side effect without exact authorization",
        "final report must include completed work, artifacts, verification, current state, next step, and closure handshake",
    ]
    lowered = template.lower()
    if "next locked action" not in lowered:
        important.insert(0, "read the contract-defined TODO before deciding scope")
    return "; ".join(important)


def _compact_prompt(prompt: str) -> str:
    marker = "- next_locked_action: "
    if len(prompt) <= AO_PROMPT_SOFT_LIMIT or marker not in prompt:
        return prompt[:AO_PROMPT_SOFT_LIMIT]
    before, after = prompt.split(marker, 1)
    next_line, rest = after.split("\n", 1)
    max_next = 900
    if len(next_line) > max_next:
        next_line = next_line[: max_next - 3] + "..."
    compacted = before + marker + next_line + "\n" + rest
    if len(compacted) <= AO_PROMPT_SOFT_LIMIT:
        return compacted
    tail_marker = "Dispatch obligation (Option-2 orchestrator-poke"
    if tail_marker in compacted:
        tail_start = compacted.index(tail_marker)
        tail = compacted[tail_start:]
        separator = "\n...[prompt compacted: middle authority prose elided]...\n"
        # Preserve the stale-TODO repair note across compaction: it is load-bearing — it tells the
        # worker the TODO mirror is stale and to reconcile from canonical state, so eliding it could let
        # the worker act on stale TODO data and mis-dispatch. Reserve room for the note and re-inject it
        # between head and separator, unless it already survived inside the kept head.
        preserved_block = ""
        note_marker = "TODO mirror repair context:"
        auth_marker = "\nAO_ORCHESTRATOR_OWNER_PROXY_DISPATCH_AUTHORIZATION:"
        if note_marker in compacted:
            note_start = compacted.index(note_marker)
            note_end = compacted.find(auth_marker, note_start)
            if note_end == -1:
                note_end = compacted.find("\n\n", note_start)
            if note_end == -1:
                note_end = tail_start
            if note_end != -1:
                preserved_block = "\n" + compacted[note_start:note_end].strip() + "\n"
        head_limit = AO_PROMPT_SOFT_LIMIT - len(tail) - len(separator) - len(preserved_block)
        if head_limit > 800:
            head = compacted[:head_limit].rstrip()
            if preserved_block and note_marker in head:
                preserved_block = ""
            return head + preserved_block + separator + tail
    return compacted[:AO_PROMPT_SOFT_LIMIT]
