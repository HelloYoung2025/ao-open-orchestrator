"""Run the orchestrator-poke Node test suite inside the pytest gate.

Skips cleanly when node is unavailable so the (Python) gate never becomes node-dependent.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

POKE_DIR = Path(__file__).resolve().parents[1] / "packages" / "orchestrator-poke"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_orchestrator_poke_node_suite():
    result = subprocess.run(
        ["node", "--test"],
        cwd=str(POKE_DIR),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"node --test failed:\n{result.stdout}\n{result.stderr}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_orchestrator_poke_prepublish_leak_guard_passes():
    result = subprocess.run(
        ["node", "prepublish-check.mjs"],
        cwd=str(POKE_DIR),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"prepublish leak guard failed:\n{result.stdout}\n{result.stderr}"
