from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shlex
import shutil
import subprocess


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


def run_command(command: list[str] | tuple[str, ...], cwd: Path | None = None, timeout: int = 30) -> CommandResult:
    proc = subprocess.run(
        list(command),
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return CommandResult(tuple(command), proc.returncode, proc.stdout.strip(), proc.stderr.strip())


class CodexAdapter:
    """Default worker adapter: a Codex CLI session resumed via the ``codex-sync`` helper.

    ``worker_ref`` is the opaque session reference the helper resumes. The public package ships
    this single concrete worker; a different worker is wired by replacing this class at the CLI
    call site (no speculative multi-worker framework is shipped).
    """

    def __init__(self, project_root: Path, worker_ref: str):
        self.project_root = project_root
        self.worker_ref = worker_ref

    def observe(self) -> dict:
        if not shutil.which("codex-sync"):
            return {"ok": False, "error": "codex-sync not found"}
        path = run_command(["codex-sync", "path", self.worker_ref], cwd=self.project_root)
        status = run_command(["codex-sync", "status"], cwd=self.project_root)
        return {
            "ok": path.returncode == 0,
            "path": path.stdout,
            "path_error": path.stderr,
            "status_preview": status.stdout[:2000],
        }

    def visible_terminal_command(self) -> str:
        return f"cd {self.project_root} && codex-sync resume {self.worker_ref}"


class ManualReviewer:
    """Default reviewer: fail-loud, no GUI/app automation baked in.

    It validates that the package and prompt exist and records the submission intent. The operator
    completes the actual hand-off to ``reviewer_target``. This mirrors the brand-neutral, no-default
    reviewer line of ``ao_state_writer.escalated_review_actuator``.
    """

    mode = "manual"

    def __init__(self, reviewer_target: str):
        self.reviewer_target = reviewer_target

    def submit_package(self, package_path: Path, prompt_path: Path, dry_run: bool = False) -> dict:
        if dry_run:
            return {
                "dry_run": True,
                "mode": self.mode,
                "reviewer_target": self.reviewer_target,
                "package_path": str(package_path),
                "prompt_path": str(prompt_path),
            }
        if not package_path.exists():
            raise FileNotFoundError(package_path)
        if not prompt_path.exists():
            raise FileNotFoundError(prompt_path)
        return {
            "dry_run": False,
            "mode": self.mode,
            "reviewer_target": self.reviewer_target,
            "package_path": str(package_path),
            "prompt_path": str(prompt_path),
            "instructions": (
                f"Manual submission required: deliver {package_path.name} and the prompt to "
                f"reviewer target {self.reviewer_target!r}. Set reviewer_submit_command in "
                "commander.toml to automate this step."
            ),
        }


class CommandReviewer:
    """Optional reviewer: shells out to an operator-configured submit command.

    Invoked as ``<command> <package_path> <prompt_path>``. The command owns its own transport
    (CLI, HTTP, GUI wrapper, ...); the public package hardcodes no app, brand, or protocol.
    """

    mode = "command"

    def __init__(self, reviewer_target: str, submit_command: str, timeout: int = 120):
        self.reviewer_target = reviewer_target
        self.submit_command = submit_command
        self.timeout = timeout

    def submit_package(self, package_path: Path, prompt_path: Path, dry_run: bool = False) -> dict:
        tokens = shlex.split(self.submit_command)
        if not tokens:
            raise RuntimeError("reviewer_submit_command is empty")
        if dry_run:
            return {
                "dry_run": True,
                "mode": self.mode,
                "reviewer_target": self.reviewer_target,
                "command": tokens,
                "package_path": str(package_path),
                "prompt_path": str(prompt_path),
            }
        if not package_path.exists():
            raise FileNotFoundError(package_path)
        if not prompt_path.exists():
            raise FileNotFoundError(prompt_path)
        first = Path(tokens[0]).expanduser()
        if not (shutil.which(tokens[0]) or first.exists()):
            raise FileNotFoundError(f"reviewer submit command not found: {tokens[0]}")
        result = run_command([*tokens, str(package_path), str(prompt_path)], timeout=self.timeout)
        if result.returncode != 0:
            raise RuntimeError(result.stderr or result.stdout or "reviewer submit command failed")
        return {
            "dry_run": False,
            "mode": self.mode,
            "reviewer_target": self.reviewer_target,
            "command": tokens,
            "package_path": str(package_path),
            "stdout": result.stdout[:500],
        }


def make_reviewer(reviewer_target: str, submit_command: str = ""):
    if submit_command.strip():
        return CommandReviewer(reviewer_target, submit_command)
    return ManualReviewer(reviewer_target)


def doctor(project_root: Path, worker_ref: str | None = None, reviewer_command: str | None = None) -> list[tuple[str, bool, str]]:
    checks: list[tuple[str, bool, str]] = []
    for name in ["python3", "codex-sync", "codex", "zip", "unzip", "shasum"]:
        path = shutil.which(name)
        checks.append((name, bool(path), path or "not found"))
    if reviewer_command and reviewer_command.strip():
        tokens = shlex.split(reviewer_command)
        first = tokens[0] if tokens else ""
        resolved = shutil.which(first) if first else None
        if not resolved and first and Path(first).expanduser().exists():
            resolved = str(Path(first).expanduser())
        checks.append(("reviewer submit command", bool(resolved), resolved or f"not found: {first or '(empty)'}"))
    else:
        checks.append(
            ("reviewer mode", True, "manual (operator submits; set reviewer_submit_command to automate)")
        )
    if worker_ref and shutil.which("codex-sync"):
        observed = CodexAdapter(project_root, worker_ref).observe()
        checks.append(("codex worker session", bool(observed.get("ok")), observed.get("path") or observed.get("error", "")))
    return checks
