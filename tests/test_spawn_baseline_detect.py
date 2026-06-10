"""Unit tests for the spawn-baseline DETECTION logic ported in Group E slice "b3b"
(cli._spawn_baseline_issue + its git / AO-session-metadata helpers).

WHY THIS MATTERS (intent, not just behavior)
A worker that AO spawns onto the WRONG source baseline silently does work that can never land. The
detector decides, from the worker's real git worktree, whether it shares the required source baseline:

  * a resolvable merge-base with the active-root HEAD => OK (the worker is on a related history);
  * an unrelated history OR a worktree whose object store lacks the required source commit =>
    spawn_base_commit_mismatch (a BLOCKING bad baseline);
  * any probe we cannot complete (no git, missing/unparseable session metadata, missing project id,
    worker worktree not a repo) => spawn_baseline_unverified (fail-closed, but distinguishable).

These pure detectors take their inputs explicitly, so a UNIT test can exercise the mismatch/unverified
logic directly (e.g. ``test_mismatch_when_worker_history_unrelated``) WITHOUT a contract opt-in. The
SPAWN-FLOW WIRING, however, is contract-gated (b3c): ``_dispatch_one`` only invokes ``_spawn_baseline_issue``
when the project sets ``state_writer.preflight.spawned_worker_requires_current_source_baseline`` — see the
opt-in tests below and ``test_spawn_baseline_dispatch_wire.py``. LIVE's own contract sets the token, so
that gate is behaviorally identical to LIVE's always-on call for a token-setting source project while
leaving generic adopters unencumbered. The B1 relaxation (shared-ancestor, not descendant) is faithful
to LIVE cli.py:349-357 —
AO-native workers are based on origin/<defaultBranch>, never descendants of the sibling active-root
branch. Tested here in isolation with real temp git repos + an AO_PROJECTS_ROOT override.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ao_state_writer import cli

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git binary required")


def _run_git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _git_repo(path: Path, *, content: str = "x") -> str:
    """Init a git repo at path with one commit; return its HEAD sha."""
    path.mkdir(parents=True, exist_ok=True)
    _run_git(path, "init", "-q")
    (path / "file.txt").write_text(content, encoding="utf-8")
    _run_git(path, "add", "-A")
    _run_git(path, "commit", "-q", "-m", "c1")
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def _write_session(projects_root: Path, project_id: str, sid: str, worktree: Path) -> None:
    sdir = projects_root / project_id / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / f"{sid}.json").write_text(json.dumps({"worktree": str(worktree)}), encoding="utf-8")


# --- git helpers ----------------------------------------------------------------------------------

def test_git_helpers_on_a_real_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    head = _git_repo(repo)
    assert cli._git_stdout(repo, "rev-parse", "HEAD") == head
    assert cli._git_worktree_status(repo) == (True, None)
    assert cli._git_commit_exists(repo, head) is True
    assert cli._git_commit_exists(repo, "0" * 40) is False


def test_git_helpers_on_non_repo(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert cli._git_stdout(plain, "rev-parse", "HEAD") is None
    assert cli._git_worktree_status(plain) == (False, None)


def test_session_worktree_from_payload_shapes(tmp_path: Path) -> None:
    assert cli._session_worktree_from_payload({"worktree": "/a/b"}) == Path("/a/b")
    assert cli._session_worktree_from_payload({"workspacePath": "/c/d"}) == Path("/c/d")
    assert cli._session_worktree_from_payload({"worktree": {"path": "/e/f"}}) == Path("/e/f")
    nested = {"lifecycle": {"runtime": {"handle": {"data": {"workspacePath": "/g/h"}}}}}
    assert cli._session_worktree_from_payload(nested) == Path("/g/h")
    assert cli._session_worktree_from_payload({"worktree": "   "}) is None
    assert cli._session_worktree_from_payload("not-a-dict") is None


# --- _spawn_baseline_issue: detection -------------------------------------------------------------

def test_ok_when_worker_shares_source_commit(tmp_path: Path, monkeypatch) -> None:
    # Worker is a clone of source, so source HEAD is resolvable in the worker's object store and they
    # share a merge-base => the detector returns None (no issue).
    source = tmp_path / "source"
    _git_repo(source)
    worker = tmp_path / "worker"
    subprocess.run(["git", "clone", "-q", str(source), str(worker)], check=True, capture_output=True)
    projects = tmp_path / "ao-projects"
    monkeypatch.setenv("AO_PROJECTS_ROOT", str(projects))
    _write_session(projects, "proj", "sid-1", worker)

    issue = cli._spawn_baseline_issue(root=source, ao_project_id="proj", spawn_session_id="sid-1")
    assert issue is None, issue


def test_mismatch_when_worker_history_unrelated(tmp_path: Path, monkeypatch) -> None:
    # Worker is an INDEPENDENT repo (separate object store): it does not contain the required source
    # commit, so the merge-base cannot resolve and _git_commit_exists is False => commit mismatch.
    source = tmp_path / "source"
    _git_repo(source, content="source")
    worker = tmp_path / "worker"
    _git_repo(worker, content="worker")
    projects = tmp_path / "ao-projects"
    monkeypatch.setenv("AO_PROJECTS_ROOT", str(projects))
    _write_session(projects, "proj", "sid-1", worker)

    issue = cli._spawn_baseline_issue(root=source, ao_project_id="proj", spawn_session_id="sid-1")
    assert issue is not None
    assert issue["result"] == "spawn_base_commit_mismatch", issue
    assert issue["reason"] == "spawn_base_commit_mismatch", issue
    assert issue["worker_worktree"] == str(worker)


def test_unverified_when_worker_worktree_not_git(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    _git_repo(source)
    worker = tmp_path / "worker-plain"
    worker.mkdir()
    projects = tmp_path / "ao-projects"
    monkeypatch.setenv("AO_PROJECTS_ROOT", str(projects))
    _write_session(projects, "proj", "sid-1", worker)

    issue = cli._spawn_baseline_issue(root=source, ao_project_id="proj", spawn_session_id="sid-1")
    assert issue is not None
    assert issue["result"] == "spawn_baseline_unverified", issue
    assert issue["reason"] == "spawn_worktree_not_git", issue


def test_unverified_when_session_metadata_missing(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    _git_repo(source)
    projects = tmp_path / "ao-projects"
    monkeypatch.setenv("AO_PROJECTS_ROOT", str(projects))
    # No session json written. Keep the retry loop fast.
    monkeypatch.setattr(cli, "SPAWN_SESSION_METADATA_RETRIES", 2)
    monkeypatch.setattr(cli, "SPAWN_SESSION_METADATA_RETRY_DELAY_SECONDS", 0)

    issue = cli._spawn_baseline_issue(root=source, ao_project_id="proj", spawn_session_id="sid-missing")
    assert issue is not None
    assert issue["result"] == "spawn_baseline_unverified", issue
    assert issue["reason"] == "missing_spawn_session_metadata", issue


def test_unverified_when_project_id_missing(tmp_path: Path) -> None:
    # Source is git (so the baseline resolves), but neither an explicit ao_project_id nor a contract
    # project_id is available => the AO session path cannot be located.
    source = tmp_path / "source"
    _git_repo(source)
    issue = cli._spawn_baseline_issue(root=source, ao_project_id=None, spawn_session_id="sid-1")
    assert issue is not None
    assert issue["result"] == "spawn_baseline_unverified", issue
    assert issue["reason"] == "missing_ao_project_id", issue


# --- opt-in gating (zero behavior change when not opted in) ----------------------------------------

def _write_contract(root: Path, *, requires_baseline: bool) -> None:
    section = (
        "[state_writer.preflight]\nspawned_worker_requires_current_source_baseline = true\n"
        if requires_baseline
        else ""
    )
    root.joinpath("DIRECT_PROJECT_CONTRACT.toml").write_text(
        "version = 1\n\n[owner_proxy]\nproject_id = \"example-project\"\n\n" + section,
        encoding="utf-8",
    )


def test_source_not_git_is_unverified_when_opted_in(tmp_path: Path) -> None:
    root = tmp_path / "proj-root"
    root.mkdir()
    _write_contract(root, requires_baseline=True)
    issue = cli._spawn_baseline_issue(root=root, ao_project_id="proj", spawn_session_id="sid-1")
    assert issue is not None
    assert issue["result"] == "spawn_baseline_unverified", issue
    assert issue["reason"] == "source_not_git", issue


def test_source_not_git_is_noop_when_not_opted_in(tmp_path: Path) -> None:
    # The load-bearing zero-behavior-change guarantee: a project that does NOT opt in and whose source
    # is not a git repo gets NO issue at all.
    root = tmp_path / "proj-root"
    root.mkdir()
    _write_contract(root, requires_baseline=False)
    issue = cli._spawn_baseline_issue(root=root, ao_project_id="proj", spawn_session_id="sid-1")
    assert issue is None, issue


def test_contract_requires_spawn_baseline_reads_opt_in(tmp_path: Path) -> None:
    root = tmp_path / "r1"
    root.mkdir()
    _write_contract(root, requires_baseline=True)
    assert cli._contract_requires_spawn_baseline(root) is True

    root2 = tmp_path / "r2"
    root2.mkdir()
    _write_contract(root2, requires_baseline=False)
    assert cli._contract_requires_spawn_baseline(root2) is False
