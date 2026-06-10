from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Iterable, Set

from .compat import (
    InvalidCanonicalFilename,
    canonical_master_plan_file,
    canonical_session_log_file,
    canonical_todo_file,
)


# Framework/tooling surfaces that are the same for every project (not per-project canonical state
# docs). These stay fixed; the canonical trio below is contract-driven per project.
FIXED_GOVERNANCE_FILES = {
    "AGENTS.md",
    "CLAUDE.md",
    "DIRECT_PROJECT_CONTRACT.toml",
    "agent-orchestrator.yaml",
    "governance/AGENTS_MULTI_AGENT_APPENDIX.md",
}
# Historical default canonical trio; kept so module-level GOVERNANCE_FILES preserves the legacy
# default set for callers without a project root (e.g. the stateless check-governance-dirty CLI).
_DEFAULT_CANONICAL_FILES = {"MASTER_PLAN.md", "TODO.md", "SESSION_LOG.md"}
GOVERNANCE_FILES = FIXED_GOVERNANCE_FILES | _DEFAULT_CANONICAL_FILES
GIT_STATUS_TIMEOUT_SECONDS = 10


def effective_governance_files(root: Path) -> Set[str]:
    """FIXED framework files plus this project's contract-resolved canonical trio.

    Raises InvalidCanonicalFilename when the contract supplies an unsafe canonical filename.
    """
    return FIXED_GOVERNANCE_FILES | {
        canonical_master_plan_file(root),
        canonical_todo_file(root),
        canonical_session_log_file(root),
    }


@dataclass(frozen=True)
class PreflightBlocker:
    code: str
    path: str


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    blockers: list[PreflightBlocker]


def _status_paths(status_output: str) -> Iterable[str]:
    for line in status_output.splitlines():
        if not line or line.startswith("##"):
            continue
        path = line[3:] if len(line) > 3 else ""
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path:
            yield path


def _is_governance_path(path: str, governance_files: Set[str] = GOVERNANCE_FILES) -> bool:
    return path in governance_files or path.startswith("governance/")


def check_governance_dirty(
    status_output: str,
    recognized_paths: Set[str],
    governance_files: Set[str] = GOVERNANCE_FILES,
) -> PreflightResult:
    blockers = [
        PreflightBlocker(code="unrecognized_governance_dirty", path=path)
        for path in _status_paths(status_output)
        if _is_governance_path(path, governance_files) and path not in recognized_paths
    ]
    return PreflightResult(ok=not blockers, blockers=blockers)


def find_governance_blockers(root: Path, recognized_paths: Set[str] | None = None) -> list[str]:
    if not (root / ".git").exists():
        return []

    try:
        governance_files = effective_governance_files(root)
    except InvalidCanonicalFilename as exc:
        return [f"invalid_canonical_filename:{exc.key}"]

    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "status", "--short", "--", *sorted(governance_files), "governance"],
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_STATUS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return [f"git_status_timeout:{GIT_STATUS_TIMEOUT_SECONDS}s"]
    if completed.returncode != 0:
        return [f"git_status_failed:{completed.stderr.strip()}"]

    result = check_governance_dirty(completed.stdout, recognized_paths or set(), governance_files)
    return [f"{blocker.code}:{blocker.path}" for blocker in result.blockers]
