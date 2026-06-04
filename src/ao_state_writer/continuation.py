from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re
import shlex
import subprocess

from .compat import read_contract_active_root, read_contract_section, read_contract_string, validate_contract_compat
from .preflight import GOVERNANCE_FILES, check_governance_dirty
from .todo import COMPACT_CURRENT_STATE_FIELDS, validate_compact_current_state
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
    "gpt_pro_desktop_review",
)
# Tokens that are deliberately not executable.  The CLI preflight surfaces these as
# owner_proxy_convergence_required envelopes so AO cannot silently treat them as queues,
# gated actions, or spawnable work.
NON_EXECUTABLE_ACTIONS = (
    "repair_attempts_exhausted",
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
# <active_root> and <orchestrator_session> are substituted at render time.
DISPATCH_OBLIGATION_INSTRUCTION = (
    "Dispatch obligation (Option-2 orchestrator-poke, REQUIRED after finishing this slice):\n"
    "1. Submit the next canonical proposal for this action through `ao-state-writer apply --root <active_root> "
    "--proposal <your_proposal.json>` "
    "(do NOT pass --continue-on-next-action), then git commit your governance/TODO/session-log changes.\n"
    "2. Run `ao report needs_input` and STOP. Do NOT run `ao-state-writer continue`/`dispatch` or "
    "`ao spawn` yourself. The orchestrator-poke notifier wakes the orchestrator, which reconciles "
    "from canonical state and runs `ao-state-writer continue` to dispatch the next slice.\n"
    "Do NOT `ao spawn` successors yourself outside of `continue`."
)
CODEX_CC_REVIEW_OBLIGATION_INSTRUCTION = (
    "Dispatch obligation (Option-2 orchestrator-poke, REQUIRED after finishing this Codex cc review):\n"
    "1. Run the scoped Codex cc review exactly as specified above, capturing its transcript.\n"
    "2. Submit the Codex cc receipt proposal through `ao-state-writer apply --root <active_root> "
    "--proposal <your_receipt_proposal.json>` (do NOT pass --continue-on-next-action), then git commit "
    "the receipt/state evidence.\n"
    "3. Run `ao report needs_input` and STOP. Do NOT run `ao-state-writer continue`/`dispatch` or "
    "`ao spawn` yourself. The orchestrator-poke notifier wakes the orchestrator, which reconciles "
    "from canonical state and runs `ao-state-writer continue` to dispatch the next slice.\n"
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
        "Capture the `codex exec` output to a transcript file, compute its sha256, and include that digest "
        "as codex_cc_transcript_sha256 (exact field name) in the receipt proposal, or ao-state-writer "
        "rejects the pass-type receipt as missing_codex_cc_transcript. "
        "If the contract requires an independent reviewer, do not review your own implementation work."
    ),
    "repair_active": (
        "Repair the typed blocker for the current target recorded in TODO.md Current "
        "Execution State. Fix the root cause, re-run local verification, then submit the correct "
        "review proposal — the choice depends on target_kind: "
        "(a) major_chapter: submit a gpt_pro_review_pending proposal so the GPT Pro external review "
        "re-runs (NOT evidence_pending — that would route to Codex cc and bypass GPT Pro entirely); "
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
        "Current Execution State. The GPT Pro external review has passed (receipt accepted by "
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

    governance_blockers = _governance_blockers(root)
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
    prompt = _render_prompt_for_action(
        decision.next_required_action,
        template=template,
        current_state=current_state,
        decision=decision,
        active_root=active_root,
        orchestrator_session=orchestrator_session,
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
) -> ContinuationResult:
    candidate = evaluate_continuation(root=root, decision=decision, ao_project_id=ao_project_id)
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


def _governance_blockers(root: Path) -> list[str]:
    if not (root / ".git").exists():
        return []

    completed = subprocess.run(
        ["git", "-C", str(root), "status", "--short", "--", *sorted(GOVERNANCE_FILES), "governance"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return [f"git_status_failed:{completed.stderr.strip()}"]

    result = check_governance_dirty(completed.stdout, set())
    return [f"{blocker.code}:{blocker.path}" for blocker in result.blockers]


def _read_compact_current_state(root: Path) -> tuple[dict[str, str], list[str]]:
    todo_path = root / "TODO.md"
    if not todo_path.exists():
        return {}, ["missing_todo_file:TODO.md"]

    block = _extract_markdown_section(todo_path.read_text(encoding="utf-8"), "## Current Execution State")
    if block is None:
        return {}, ["missing_current_execution_state_section"]

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


def _read_next_step_template(root: Path) -> str | None:
    section = read_contract_section(root, "dispatch_templates.next_step_plan_mode")
    if section is None:
        return None
    return _extract_triple_quoted_value(section, "template")


def _read_orchestrator_session(root: Path) -> str:
    default = "example-orchestrator"
    value = read_contract_string(root, "continuation_policy", "orchestrator_session")
    return value or default


def _extract_markdown_section(text: str, marker: str) -> str | None:
    if marker not in text:
        return None
    rest = text.split(marker, 1)[1]
    next_heading = rest.find("\n## ")
    if next_heading == -1:
        return rest.strip()
    return rest[:next_heading].strip()


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


def _dispatch_obligation(*, action: str, active_root: Path, orchestrator_session: str) -> str:
    template = (
        CODEX_CC_REVIEW_OBLIGATION_INSTRUCTION
        if action == "codex_cc_review"
        else DISPATCH_OBLIGATION_INSTRUCTION
    )
    return template.replace("<active_root>", str(active_root)).replace(
        "<orchestrator_session>", orchestrator_session
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
        "AO_ORCHESTRATOR_OWNER_PROXY_DISPATCH_AUTHORIZATION:\n"
        "This continuation spawn is the AO orchestrator Owner-proxy authorization to execute the "
        "canonical next_locked_action. Start the dispatched next_locked_action now; do not pause "
        "for another ordinary owner prompt solely because older TODO text says not to auto-start. "
        "For non-Master-Plan actions, do NOT wait for the human owner. If required evidence is missing, "
        "stop and escalate to the Owner-Proxy with a typed blocker instead of guessing.\n"
        f"{OWNER_PROXY_ESCALATION_TAXONOMY}\n\n"
        f"{_dispatch_obligation(action=decision.next_required_action or '', active_root=active_root, orchestrator_session=orchestrator_session)}\n\n"
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
) -> str:
    if action == DISPATCH_NEXT_SLICE_ACTION:
        return _render_prompt(
            template=template,
            current_state=current_state,
            decision=decision,
            active_root=active_root,
            orchestrator_session=orchestrator_session,
        )
    return _render_action_prompt(
        action,
        current_state=current_state,
        decision=decision,
        active_root=active_root,
        orchestrator_session=orchestrator_session,
    )


def _render_action_prompt(
    action: str,
    *,
    current_state: dict[str, str],
    decision: ContinuationDecision,
    active_root: Path,
    orchestrator_session: str,
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
            "- Capture the `codex exec` output to a transcript file, compute its sha256, and include that "
            "digest as codex_cc_transcript_sha256 (exact field name) in the receipt proposal; a pass-type "
            "receipt without it is rejected as missing_codex_cc_transcript.",
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
        head_limit = AO_PROMPT_SOFT_LIMIT - len(tail) - len(separator)
        if head_limit > 1000:
            return compacted[:head_limit].rstrip() + separator + tail
    return compacted[:AO_PROMPT_SOFT_LIMIT]
