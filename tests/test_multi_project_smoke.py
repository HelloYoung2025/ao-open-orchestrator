"""M2-2 multi-project smoke: two bootstrapped projects coexist on one machine with zero bleed.

WHY THIS EXISTS (the spec, not just a check): the multi-project promise is that ONE machine (one
daemon, one engine install) serves N registered projects at the same time — the historical failure
mode being a machinery root and an active project root forced into one directory. That promise
holds only if (a) the per-project deployment artifacts are namespaced by project id (two projects'
launchd plists/logs/sidecars cannot collide), and (b) the per-project canonical state trees are
fully isolated under concurrent writes (engine activity in project A can never leak a target,
proposal, or revision bump into project B). This suite renders TWO projects via the real bootstrap
and proves both properties; the machine-global escalated-review actuator mutex (the one
deliberately SHARED resource) is covered by tests/test_actuator_machine_lock.py.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from test_public_core import _write_contract, _writer

from ao_state_writer.writer import StateTransitionProposal

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "bootstrap_project.py"

_spec = importlib.util.spec_from_file_location("bootstrap_project_m2", SCRIPT)
boot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(boot)


def _bootstrap(target: Path, project_id: str, home: Path) -> list[Path]:
    argv = []
    for k, v in dict(
        project_id=project_id,
        orchestrator_session=f"{project_id}-orchestrator",
        repo_owner="demo-owner", repo_name="demo-repo", agent="claude-code",
        worker_model="demo-worker", orchestrator_model="demo-orch",
        home=str(home), path="/usr/bin:/bin", target_dir=str(target),
    ).items():
        argv += [f"--{k.replace('_', '-')}", str(v)]
    return boot.run(boot.parse_args(argv))


def test_two_projects_render_namespaced_deployment_artifacts(tmp_path):
    """Per-project artifact namespacing: the launchd label/plist and the liveness log default to
    project-id-derived names, so two projects' deployments cannot collide under one ~ or one
    LaunchAgents dir. The rendered plists must also be VALID launchd property lists."""
    home = tmp_path / "home"
    a, b = tmp_path / "proj-a", tmp_path / "proj-b"
    _bootstrap(a, "alpha-proj", home)
    _bootstrap(b, "beta-proj", home)

    plist_a = a / "ao-orchestrator-liveness.alpha-proj.plist"
    plist_b = b / "ao-orchestrator-liveness.beta-proj.plist"
    assert plist_a.is_file() and plist_b.is_file()
    # The default log file is per-project too (a shared log would interleave two daemons).
    assert "alpha-proj-orchestrator-liveness.log" in plist_a.read_text()
    assert "beta-proj-orchestrator-liveness.log" in plist_b.read_text()

    # Zero identity bleed: neither project's rendered tree mentions the OTHER project's id.
    blob_a = "\n".join(p.read_text() for p in a.rglob("*") if p.is_file())
    blob_b = "\n".join(p.read_text() for p in b.rglob("*") if p.is_file())
    assert "beta-proj" not in blob_a
    assert "alpha-proj" not in blob_b

    plutil = shutil.which("plutil")
    if plutil is None:
        pytest.skip("plutil unavailable (non-macOS) — plist syntax check skipped")
    for plist in (plist_a, plist_b):
        proc = subprocess.run([plutil, "-lint", str(plist)], capture_output=True, text=True)
        assert proc.returncode == 0, (plist.name, proc.stdout, proc.stderr)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_sidecar_singleton_guard_is_per_project_not_machine_global(tmp_path):
    """M2 codex-review MAJOR: the sidecar's singleton pidfile must be per-project. With a
    machine-global default pidfile, project B's sidecar sees project A's LIVE sidecar pid (the
    guard matches on the script name, which is project-agnostic), prints "another instance" and
    exits 0 — and under launchd KeepAlive={SuccessfulExit:false} exit 0 is terminal: B's
    unattended self-healing never runs. So: a live sibling sidecar holding the LEGACY
    machine-global pidfile must NOT stop another project's sidecar from starting."""
    home = tmp_path / "home"
    agent_dir = home / ".agent-orchestrator"
    agent_dir.mkdir(parents=True)

    # Stand-in for project A's live sidecar: a process whose command line names the script
    # (argv[0] swap), parked in sleep — exactly what the guard's `ps ... | grep` matches.
    decoy = subprocess.Popen(["bash", "-c", "exec -a orchestrator-liveness.sh sleep 60"])
    try:
        global_pidfile = agent_dir / "orchestrator-liveness.pid"
        global_pidfile.write_text(f"{decoy.pid}\n", encoding="utf-8")

        env = {
            "HOME": str(home),
            "PATH": "/usr/bin:/bin",
            "ORCHESTRATOR_SESSION": "beta-proj-orchestrator",
            "PROJECT_ID": "beta-proj",
            "ACTIVE_ROOT": str(tmp_path / "beta-root"),
            "LOG_FILE": str(tmp_path / "beta-liveness.log"),
            # Exit after the singleton guard with zero loop-body side effects.
            "LIVENESS_MAX_TICKS": "0",
            "INTERVAL_S": "0",
        }
        proc = subprocess.run(
            ["bash", str(REPO / "examples" / "orchestrator-liveness.sh")],
            env=env, capture_output=True, text=True, timeout=30,
        )
        assert "another instance" not in proc.stderr, (
            "project B's sidecar treated project A's sidecar as a duplicate of itself:\n"
            + proc.stderr
        )
        assert proc.returncode == 0, (proc.returncode, proc.stderr)
        # B actually started (passed the guard and reached its main loop).
        assert "orchestrator-liveness started" in (tmp_path / "beta-liveness.log").read_text()
        # ... without claiming/clobbering the legacy machine-global pidfile (project A's, here).
        assert global_pidfile.read_text(encoding="utf-8").strip() == str(decoy.pid)
    finally:
        decoy.kill()
        decoy.wait()


def test_concurrent_engine_writes_stay_fully_isolated(tmp_path):
    """Two projects' engines running CONCURRENTLY (threads stand in for two daemon-driven
    orchestrators) must keep their canonical state trees fully isolated: each project's
    state.json carries ONLY its own targets/proposals and its own independent revision chain."""
    roots = {pid: tmp_path / pid for pid in ("alpha-proj", "beta-proj")}
    for root in roots.values():
        root.mkdir()
        _write_contract(root)
    writers = {pid: _writer(root) for pid, root in roots.items()}
    steps = 5
    barrier = threading.Barrier(2)
    errors: list[tuple[str, BaseException]] = []

    def drive(pid: str) -> None:
        try:
            barrier.wait()
            for i in range(steps):
                decision = writers[pid].apply(
                    StateTransitionProposal(
                        proposal_id=f"{pid}-p{i}",
                        target_kind="small_chapter",
                        target_id=f"{pid}-chapter-{i}",
                        base_state_revision=i,
                        requested_state="evidence_pending",
                        actor_role="worker",
                        evidence_refs=[f"evidence:{pid}-{i}"],
                    )
                )
                assert decision.decision == "accepted", (pid, i, decision)
        except BaseException as exc:  # noqa: BLE001 - surfaced to the main thread below
            errors.append((pid, exc))

    threads = [threading.Thread(target=drive, args=(pid,)) for pid in roots]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors

    for pid, root in roots.items():
        other = next(p for p in roots if p != pid)
        state = json.loads(writers[pid].state_path.read_text(encoding="utf-8"))
        # Own chain complete and independent.
        assert state["state_revision"] == steps, (pid, state["state_revision"])
        assert set(state["targets"]) == {f"{pid}-chapter-{i}" for i in range(steps)}
        assert set(state["proposal_results"]) == {f"{pid}-p{i}" for i in range(steps)}
        # Zero bleed from the sibling project.
        assert not any(other in t for t in state["targets"]), (pid, sorted(state["targets"]))
        # Each project's ledger likewise carries only its own proposals.
        ledger_pids = {
            json.loads(line)["proposal"]["proposal_id"]
            for line in writers[pid].ledger_path.read_text(encoding="utf-8").splitlines()
        }
        assert ledger_pids == {f"{pid}-p{i}" for i in range(steps)}, (pid, ledger_pids)
