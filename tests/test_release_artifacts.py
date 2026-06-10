from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def test_release_workflows_are_manual_and_fail_closed() -> None:
    ci = ROOT / ".github" / "workflows" / "ci.yml"
    release = ROOT / ".github" / "workflows" / "release.yml"

    assert ci.exists()
    assert release.exists()

    release_text = release.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in release_text
    assert "push:" not in release_text
    assert "schedule:" not in release_text
    assert "target_sha" in release_text
    assert "tag already exists" in release_text
    assert "version mismatch" in release_text


def test_wheel_contains_escalated_review_actuator(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()

    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            str(ROOT),
            "--no-deps",
            "--wheel-dir",
            str(wheelhouse),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    wheels = list(wheelhouse.glob("ao_open_orchestrator-0.2.0-*.whl"))
    assert len(wheels) == 1

    with zipfile.ZipFile(wheels[0]) as wheel:
        names = set(wheel.namelist())

    assert "ao_state_writer/escalated_review_actuator.py" in names
    # The brand-neutral actuator shells out to a user-configured reviewer; no driver shell
    # scripts ship in the public wheel (the retired product bridge was a .sh driver).
    assert not any(name.endswith(".sh") for name in names)
    assert "ao_open_orchestrator-0.2.0.dist-info/entry_points.txt" in names
