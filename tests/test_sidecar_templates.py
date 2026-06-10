"""Verify the S-TMPL examples (sidecar .sh, launchd .plist.tmpl, AO config) are valid and faithful.

Non-vacuity guards (codex S-TMPL review): the plist is rendered with sample tokens BEFORE linting (an
unresolved-placeholder lint would be meaningless), the sidecar is checked for its load-bearing logic so
a stubbed/shortened script cannot pass, and the forbidden-token scan runs over the shipped templates.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "examples"
SIDECAR = EXAMPLES / "orchestrator-liveness.sh"
PLIST_TMPL = EXAMPLES / "com.example.ao-orchestrator-liveness.plist.tmpl"
CONFIG = EXAMPLES / "agent-orchestrator.config.example.yaml"
SCANNER = REPO / "scripts" / "public_safety_scan.py"

TOKEN_RE = re.compile(r"@[A-Z_]+@")
SAMPLE = {
    "@LABEL@": "com.example.ao-orchestrator-liveness",
    "@SIDECAR_PATH@": "/opt/example/orchestrator-liveness.sh",
    "@LOG_FILE@": "/tmp/example/orchestrator-liveness.log",
    "@HOME@": "/tmp/example-home",
    "@PATH@": "/usr/local/bin:/usr/bin:/bin",
    "@ACTIVE_ROOT@": "/tmp/example/project",
    "@ORCHESTRATOR_SESSION@": "demo-orchestrator",
    "@PROJECT_ID@": "demo-project",
    "@STATE_WRITER_CMD@": "ao-state-writer",
    "@PYTHON_BIN@": "python3",
    "@REPO_OWNER@": "example-owner",
    "@REPO_NAME@": "example-repo",
    "@SESSION_PREFIX@": "demo",
}


def render(text: str) -> str:
    for token, value in SAMPLE.items():
        text = text.replace(token, value)
    return text


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_sidecar_bash_syntax_ok():
    result = subprocess.run(["bash", "-n", str(SIDECAR)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_sidecar_requires_identity_env_and_is_not_stubbed():
    text = SIDECAR.read_text(encoding="utf-8")
    # Required identity/state config must fail loud (no brand defaults).
    for var in ("ORCHESTRATOR_SESSION", "PROJECT_ID", "ACTIVE_ROOT"):
        assert re.search(rf'"\$\{{{var}:\?', text), f"{var} must be a required (fail-loud) env"
    # MACHINERY/ACTIVE split: engine invoked via the helper, never a hardcoded source path.
    assert "run_state_writer()" in text and text.count("run_state_writer ") >= 3
    assert 'STATE_WRITER_CMD:-ao-state-writer' in text
    # Not stubbed/shortened: the load-bearing watchdog logic is all present.
    for marker in (
        "reclaim_orphaned_leases",
        "reconcile-leases",
        "send_reconcile_poke",
        "canonical_obligations_present",
        "singleton guard",
        "CANON_CATEGORY",
        "MAX_CONSECUTIVE_FAILURES",
    ):
        assert marker in text, f"missing load-bearing logic: {marker}"
    assert len(text.splitlines()) >= 540, "sidecar appears truncated/stubbed"


@pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil not available (non-macOS)")
def test_plist_renders_to_valid_plist_with_required_env(tmp_path):
    rendered = render(PLIST_TMPL.read_text(encoding="utf-8"))
    assert TOKEN_RE.search(rendered) is None, "unresolved @TOKEN@ after render"
    out = tmp_path / "rendered.plist"
    out.write_text(rendered, encoding="utf-8")
    result = subprocess.run(["plutil", "-lint", str(out)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    # T2 must supply every required identity env so the fail-loud sidecar runs under launchd.
    # LOG_FILE is included so the sidecar's internal log() writes to the SAME per-project file
    # launchd captures stdout/stderr into (multi-project: no shared-default log interleaving).
    for key in ("ORCHESTRATOR_SESSION", "PROJECT_ID", "ACTIVE_ROOT", "STATE_WRITER_CMD", "PYTHON_BIN", "LOG_FILE"):
        assert f"<key>{key}</key>" in rendered, f"plist missing EnvironmentVariables.{key}"
    assert "<key>RunAtLoad</key>" in rendered and "<key>KeepAlive</key>" in rendered


def test_config_renders_and_wires_orchestrator_poke():
    rendered = render(CONFIG.read_text(encoding="utf-8"))
    assert TOKEN_RE.search(rendered) is None, "unresolved @TOKEN@ after render"
    for needle in ("projects:", "notifiers:", "orchestrator-poke", "stateWriterCommand", "projectIds"):
        assert needle in rendered, needle
    try:
        import yaml  # type: ignore
    except ImportError:
        pytest.skip("pyyaml not installed; structural string checks already passed")
    data = yaml.safe_load(rendered)
    assert data["notifiers"]["orchestrator-poke"]["plugin"] == "orchestrator-poke"
    assert "demo-project" in data["projects"]


def test_examples_pass_leak_scan():
    # Scan the whole examples/ directory (not just the three S-TMPL files) so any future
    # example file is covered too — matches how the gate is invoked in CI/pre-push.
    result = subprocess.run(
        [sys.executable, str(SCANNER), str(EXAMPLES)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
