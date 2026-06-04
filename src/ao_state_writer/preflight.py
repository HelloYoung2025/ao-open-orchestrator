from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Set


GOVERNANCE_FILES = {
    "AGENTS.md",
    "CLAUDE.md",
    "MASTER_PLAN.md",
    "TODO.md",
    "SESSION_LOG.md",
    "DIRECT_PROJECT_CONTRACT.toml",
    "agent-orchestrator.yaml",
    "governance/AGENTS_MULTI_AGENT_APPENDIX.md",
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


def _is_governance_path(path: str) -> bool:
    return path in GOVERNANCE_FILES or path.startswith("governance/")


def check_governance_dirty(status_output: str, recognized_paths: Set[str]) -> PreflightResult:
    blockers = [
        PreflightBlocker(code="unrecognized_governance_dirty", path=path)
        for path in _status_paths(status_output)
        if _is_governance_path(path) and path not in recognized_paths
    ]
    return PreflightResult(ok=not blockers, blockers=blockers)
