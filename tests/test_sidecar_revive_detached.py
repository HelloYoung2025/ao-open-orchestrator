"""Sidecar revive must DETACH the runtime launcher — the patrol loop must outlive a revive.

WHY THIS EXISTS (root cause from a production cutover day): `ao start <project>` runs the AO
runtime as a FOREGROUND daemon and never exits on success. The old revive path called it
synchronously (`if ao start ...; then`), so the FIRST successful revive permanently parked the
sidecar inside that call: no further ticks, no further sweep pokes — and the success branch
(including the post-revival reconcile poke that un-parks a freshly restored orchestrator from
its resume prompt) was DEAD CODE, observable only on failure. Production symptom: one revive in
the morning, then a whole day of silence while the orchestrator sat at a resume menu.

The contract pinned here: a revive (a) launches the runtime detached, (b) confirms success by
the orchestrator session APPEARING (bounded wait) rather than by an exit code that success never
produces, (c) actually sends the post-revival reconcile-sweep poke, and (d) the loop reaches the
NEXT tick. A fake `ao start` that blocks forever (faithful daemon behavior) makes the old code
time out — this suite fails against it and passes against the detached revive.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SIDECAR = REPO / "examples" / "orchestrator-liveness.sh"
PYTHON_BIN = sys.executable


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _fake_bin(tmp_path: Path) -> Path:
    """Stub `ao`, `tmux`, and the engine. The fake `ao start` is a faithful daemon: it marks the
    orchestrator session as present, records its pid, then blocks (sleep) and never exits —
    exactly the behavior that wedged the synchronous revive."""
    fake = tmp_path / "bin"
    fake.mkdir()
    marker = tmp_path / "session-up"
    _write_exec(
        fake / "ao",
        f"""#!/bin/bash
case "$1" in
  start)
    touch "{marker}"
    echo "$$" > "{tmp_path}/ao-start.pid"
    echo "fake ao runtime supervising (never exits)"
    sleep 120
    ;;
  send) echo "Message sent and processing" ;;
  session) : ;;
esac
exit 0
""",
    )
    _write_exec(
        fake / "tmux",
        f"""#!/bin/bash
case "$1" in
  has-session) [ -f "{marker}" ]; exit $? ;;
  ls) exit 0 ;;
  send-keys) exit 0 ;;
  kill-session) rm -f "{marker}"; exit 0 ;;
esac
exit 0
""",
    )
    _write_exec(
        fake / "fake-engine",
        """#!/bin/bash
case "$1" in
  reconcile-leases) echo '{"reclaimed": []}' ;;
  list-ready) echo '{"candidates": ["p-1"], "result": "ready"}' ;;
  list-gated) echo '{"candidates": [], "result": "gated"}' ;;
  *) echo '{}' ;;
esac
exit 0
""",
    )
    return fake


def _kill_fake_daemon(tmp_path: Path) -> None:
    pid_file = tmp_path / "ao-start.pid"
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text().strip()), signal.SIGKILL)
        except (ProcessLookupError, ValueError):
            pass


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_revive_detaches_pokes_and_keeps_patrolling(tmp_path):
    fake = _fake_bin(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    log = tmp_path / "liveness.log"
    env = {
        "HOME": str(home),
        "PATH": f"{fake}:/usr/bin:/bin",
        "ORCHESTRATOR_SESSION": "demo-proj-orchestrator",
        "PROJECT_ID": "demo-proj",
        "ACTIVE_ROOT": str(tmp_path / "root"),
        "STATE_WRITER_CMD": str(fake / "fake-engine"),
        "PYTHON_BIN": PYTHON_BIN,
        "LOG_FILE": str(log),
        # Two ticks, zero waits: tick 1 must revive (orchestrator absent + ready obligations),
        # tick 2 proves the loop SURVIVED the revive (the old synchronous code never gets there).
        "LIVENESS_MAX_TICKS": "2",
        "INTERVAL_S": "0",
        "COOLDOWN_S": "0",
        "POKE_ESCAPE_SETTLE_S": "0",
        "POKE_BOOT_DELAY_S": "0",
        # Small but NONZERO: with 0 the 12-iteration bounded wait completes in microseconds,
        # before the detached launcher has even touched the session marker.
        "REVIVE_WAIT_INTERVAL_S": "0.2",
    }
    try:
        # No capture pipes: the (legitimately) surviving detached runtime must not be able to hold
        # the test hostage via an inherited fd, and on the RED timeout-kill path an orphaned fake
        # daemon must not wedge subprocess' pipe drain.
        proc = subprocess.run(
            ["bash", str(SIDECAR)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,  # the synchronous-revive bug exhausts this; the detached revive is sub-second
        )
        text = log.read_text(encoding="utf-8")
        assert proc.returncode == 0, text[-800:]
        # (a)+(b) revive succeeded via session-appearance, not via an exit code success never yields.
        assert "revive succeeded" in text, text[-800:]
        # (c) the post-revival poke — dead code under the synchronous revive — actually fired.
        assert "reconcile-sweep poke delivered for post-liveness-revival" in text, text[-800:]
        # (d) the patrol loop reached the next tick after reviving.
        assert text.count("canonical obligations present") >= 2, (
            "loop did not survive the revive:\n" + text[-800:]
        )
    finally:
        _kill_fake_daemon(tmp_path)
