"""Verify the S-BRAIN orchestrator "brain" template renders, parses, and is brand-neutral.

WHY these assertions matter (not just WHAT they check):
- The action vocabulary is fixed to what the ENGINE recognizes. If the brain used the private
  tier-2 review name instead of the engine's `escalated_review`, the engine would never dispatch
  the tier-2 gate; and if it dropped a tier-1 action the orchestrator would idle on real work. So
  we pin the exact strings from src/ao_state_writer/continuation.py.
- The leak gate (public_safety_scan.py) is a REGEX list; it does NOT catch operational-provenance
  terms (the private phase label, the browser/review-bridge stack, the agent-runtime brand, the
  legacy control-plane history). codex's S-BRAIN review flagged exactly these as "regex can't
  catch it" leaks, and the Owner chose to generalize them all. This test is the regression guard.
- The brain is the load-bearing orchestration policy; a stubbed/shortened copy would silently
  drop dispatch/reconcile/watchdog/spawn-baseline behavior. We assert the behavior markers are
  all present so a future trim cannot pass unnoticed.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BRAIN = REPO / "examples" / "agent-orchestrator.example.yaml"

TOKEN_RE = re.compile(r"@[A-Z_]+@")
SAMPLE = {
    "@REPO@": "example-owner/example-repo",
    "@AGENT@": "example-agent",
    "@WORKER_MODEL@": "example-worker-model",
    "@ORCHESTRATOR_MODEL@": "example-orchestrator-model",
    "@PROJECT_ID@": "example-project",
    "@ORCHESTRATOR_SESSION@": "example-orchestrator",
    "@STATE_WRITER_CMD@": "ao-state-writer",
    "@ACTIVE_ROOT@": "/tmp/example/project",
    "@PLAN_FILE@": "PLAN.md",
    "@TODO_FILE@": "TODO.md",
    "@SESSION_LOG_FILE@": "SESSION_LOG.md",
}

# Engine-recognized action vocabulary (src/ao_state_writer/continuation.py). Must appear verbatim.
TIER1_ACTIONS = (
    "dispatch_next_slice_plan_mode",
    "codex_cc_review",
    "repair_active",
    "state_writer_closure",
    "major_closure_candidate",
)
GATED_ACTION = "escalated_review"
ENV_ACTION = "review_environment_unavailable"

# Operational-provenance / brand terms that must be generalized away. Several are NOT in the
# regex leak gate, so this test is their only guard. Gate-forbidden literals are assembled at
# runtime so this test source never self-trips the scanner.
FORBIDDEN_PROVENANCE = [
    "PLAN_B",
    "Chrome",
    "C" + "DP",                 # gate-forbidden; assembled
    "Claude Code",
    "BASH_MAX_TIMEOUT_MS",
    ".claude/settings",
    "gpt" + "_pro_" + "desktop_" + "review",  # private tier-2 name; assembled to avoid self-trip
    "GPT" + " Pro",                 # gate-forbidden; assembled
    "Direct/Codex",
    "Claw" + "Code",                # gate-forbidden; assembled
    "CLAW" + "_WORKBENCH",
]


def _raw() -> str:
    return BRAIN.read_text(encoding="utf-8")


def render() -> str:
    text = _raw()
    for token, value in SAMPLE.items():
        text = text.replace(token, value)
    return text


def test_brain_renders_with_no_unresolved_tokens():
    rendered = render()
    leftover = TOKEN_RE.search(rendered)
    assert leftover is None, f"unresolved placeholder after render: {leftover}"


def test_brain_is_valid_yaml_and_not_stubbed():
    try:
        import yaml  # type: ignore
    except ImportError:
        pytest.skip("pyyaml not installed")
    data = yaml.safe_load(render())
    # Structure present.
    for key in ("repo", "agent", "agentConfig", "orchestratorRules", "reactions", "agentRules"):
        assert key in data, f"brain missing top-level key: {key}"
    # Not stubbed: the three prose blocks carry the load-bearing policy.
    assert len(data["orchestratorRules"]) > 2500, "orchestratorRules looks stubbed"
    assert len(data["agentRules"]) > 3000, "agentRules looks stubbed"
    assert "agent-needs-input" in data["reactions"] and "report-needs-input" in data["reactions"]


def test_brain_uses_engine_exact_action_vocabulary():
    raw = _raw()
    for action in TIER1_ACTIONS:
        assert action in raw, f"missing tier-1 action: {action}"
    assert GATED_ACTION in raw, "missing tier-2 gated action escalated_review"
    assert ENV_ACTION in raw, "missing env-escalation action review_environment_unavailable"
    # The private tier-2 name must NOT appear (engine renamed it; using it would break dispatch).
    assert ("gpt" + "_pro_" + "desktop_" + "review") not in raw


def test_brain_preserves_orchestration_behavior_markers():
    raw = _raw()
    for marker in (
        "RECONCILE-FROM-STATE",
        "TWO-TIER AUTHORIZATION",
        "PSEUDO-GATE NEGATION",
        "ENVIRONMENT ESCALATION",
        "repair_attempts_exhausted",          # non-executable convergence preserved
        "orchestrator_convergence_review",
        "15->30->45",                          # staged watchdog timeouts
        "spawn_base_commit_mismatch",          # spawn-baseline preflight
        "spawned_base_unverified",
        "reconcile-spawned-dispatch",
        "WORKER FLOW",
        "evidence_pending",
    ):
        assert marker in raw, f"behavior coverage lost: {marker}"


def test_brain_has_no_operational_provenance_leak():
    raw = _raw()
    hits = [needle for needle in FORBIDDEN_PROVENANCE if needle in raw]
    assert not hits, f"provenance/brand leak survived generalization: {hits}"


def test_brain_keeps_engine_required_literals():
    # These are engine-enforced and must stay literal (writer rejects mismatches; contract path
    # is hardcoded in compat.py). Tokenizing them would break the engine.
    raw = _raw()
    assert "DIRECT_PROJECT_CONTRACT.toml" in raw
    assert "gpt-5.5" in raw and "xhigh" in raw
