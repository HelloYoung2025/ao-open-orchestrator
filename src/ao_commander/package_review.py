from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import re
import shutil
import time
import uuid
import zipfile

from .config import CommanderConfig


class PackageError(RuntimeError):
    pass


# Path segments denied REGARDLESS of config (config exclude_patterns can only ADD, never remove),
# matched CASE-INSENSITIVELY and ANYWHERE in the path. Covers VCS, worker/agent caches, virtualenvs,
# build/cache trees, and the commander/AO runtime-state trees (.commander, .omx) so a misconfiguration
# can never ship secrets, prior packages, or runtime state — even when such a tree is nested under an
# include path or named with a different case (e.g. ``payload/.commander/...`` or ``.GIT/config`` on a
# case-insensitive filesystem). All entries are lowercase; comparison lowercases each path part.
HARD_DENIED_PARTS = {
    ".git",
    ".codex",
    ".venv",
    "venv",
    "node_modules",
    "target",
    "__pycache__",
    ".pytest_cache",
    "build",
    "dist",
    ".omx",
    ".commander",
}

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?i)(api[_-]?key|secret|token|password|passwd)\s*[:=]\s*['\"][^'\"]{8,}['\"]"),
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA |PGP )?PRIVATE KEY-----"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)aws_secret_access_key\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{40}"),
]


@dataclass(frozen=True)
class PackageResult:
    package_dir: Path
    zip_path: Path | None
    files: tuple[Path, ...]
    sha256: str | None
    prompt_path: Path | None
    dry_run: bool
    missing_optional: tuple[str, ...] = ()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_rel(root: Path, path: Path) -> Path:
    return path.resolve().relative_to(root.resolve())


def _hard_denied(rel: Path) -> bool:
    # Case-insensitive, part-based deny applied anywhere in the path (not just a root-relative
    # prefix), so nested or case-variant runtime-state/VCS trees cannot bypass the floor.
    if any(part.lower() in HARD_DENIED_PARTS for part in rel.parts):
        return True
    name = rel.name.lower()
    if name == ".env" or name.startswith(".env."):
        return True
    return False


def _denied(rel: Path, cfg: CommanderConfig) -> bool:
    if _hard_denied(rel):
        return True
    as_posix = rel.as_posix()
    return any(pattern and pattern in as_posix for pattern in cfg.exclude_patterns)


def _candidate_inputs(cfg: CommanderConfig) -> list[Path]:
    canonical = [cfg.master_plan_path, cfg.todo_path]
    extras = [cfg.project_root / name for name in cfg.extra_include_names]
    return canonical + extras


def collect_files(cfg: CommanderConfig) -> tuple[Path, ...]:
    root = cfg.project_root.resolve()
    selected: dict[Path, Path] = {}
    escapes: list[str] = []

    def consider(path: Path) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            return
        try:
            rel = resolved.relative_to(root)
        except ValueError:
            escapes.append(str(path))
            return
        if _denied(rel, cfg):
            return
        if resolved.is_file():
            selected[rel] = resolved

    for path in _candidate_inputs(cfg):
        if path.exists() and path.is_file():
            consider(path)

    for item in cfg.include_paths:
        # Reject absolute/escaping include roots up front (fail loud; do not silently skip).
        try:
            item_resolved = item.resolve()
            item_resolved.relative_to(root)
        except (ValueError, OSError):
            escapes.append(str(item))
            continue
        if item_resolved.is_file():
            consider(item_resolved)
        elif item_resolved.is_dir():
            for child in item_resolved.rglob("*"):
                if child.is_file():
                    consider(child)

    if escapes:
        raise PackageError("include paths escape project root: " + ", ".join(sorted(set(escapes))))
    files = tuple(selected[key] for key in sorted(selected))
    if not files:
        raise PackageError("review package would be empty; check canonical/include configuration")
    return files


def secret_scan(files: tuple[Path, ...]) -> list[str]:
    findings: list[str] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # Selected files are deliberately-included text candidates; one we cannot decode/read
            # could hide secret/binary material, so refuse to ship instead of skipping silently.
            findings.append(f"{path}: unreadable/undecodable, refusing to ship ({exc.__class__.__name__})")
            continue
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                findings.append(str(path))
                break
    return findings


def _write_review_files(cfg: CommanderConfig, package_dir: Path, files: tuple[Path, ...]) -> Path:
    package_dir.mkdir(parents=True, exist_ok=True)
    file_lines = []
    sha_lines = []
    for source in files:
        rel = _safe_rel(cfg.project_root, source)
        dest = package_dir / "source" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        file_lines.append(f"- source/{rel.as_posix()}")
        sha_lines.append(f"{sha256_file(dest)}  source/{rel.as_posix()}")

    manifest = package_dir / "MANIFEST.md"
    manifest.write_text(
        "# Review Package Manifest\n\n"
        f"Project: {cfg.project_name}\n\n"
        "## Files\n\n"
        + "\n".join(file_lines)
        + "\n",
        encoding="utf-8",
    )
    (package_dir / "SHA256SUMS.txt").write_text("\n".join(sha_lines) + "\n", encoding="utf-8")
    (package_dir / "VERIFICATION_SUMMARY.md").write_text(
        "# Verification Summary\n\n"
        "- Package file list collected from configured canonical and review include paths.\n"
        "- Zip integrity and hash verification must pass before submission.\n",
        encoding="utf-8",
    )
    (package_dir / "CHANGELOG_SUMMARY.md").write_text(
        "# Changelog Summary\n\n"
        "Review the included canonical files and configured source files for the current major phase.\n",
        encoding="utf-8",
    )
    prompt = package_dir / "EXTERNAL_REVIEW_PROMPT.md"
    prompt.write_text(
        "# External Review Prompt\n\n"
        f"Please review the attached package for `{cfg.project_name}`.\n\n"
        "Goal: determine whether the current major phase should proceed toward owner acceptance, "
        "or whether blocking gaps remain.\n\n"
        "Important boundaries:\n"
        "- Provide recommendations only.\n"
        "- Do not mark the phase closed.\n"
        "- Do not authorize starting the next major phase.\n"
        "- Report blocking gaps, non-blocking risks, evidence concerns, and recommended next action.\n",
        encoding="utf-8",
    )
    return prompt


def create_review_package(cfg: CommanderConfig, dry_run: bool = False, owner_authorized: bool = False) -> PackageResult:
    if not dry_run and not (owner_authorized or cfg.package_preparation_authorized):
        raise PermissionError("Package preparation requires owner authorization")
    files = collect_files(cfg)
    findings = secret_scan(files)
    if findings:
        raise PackageError("Potential secret material found: " + ", ".join(findings))

    missing_optional = tuple(
        str(path) for path in _candidate_inputs(cfg) if not (path.exists() and path.is_file())
    )

    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", cfg.project_name.strip()).strip("-") or "project"
    package_dir = cfg.package_dir / f"{slug}-review-{timestamp}-{uuid.uuid4().hex[:8]}"
    if dry_run:
        return PackageResult(package_dir, None, files, None, None, True, missing_optional)

    prompt_path = _write_review_files(cfg, package_dir, files)
    zip_path = package_dir.parent / f"{package_dir.name}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for child in sorted(package_dir.rglob("*")):
            if child.is_file():
                archive.write(child, child.relative_to(package_dir.parent))
    with zipfile.ZipFile(zip_path) as archive:
        bad = archive.testzip()
        if bad:
            raise PackageError(f"Zip integrity failed at {bad}")
        for name in archive.namelist():
            # Apply the SAME hard-deny predicate used at collection time as a defense-in-depth recheck.
            if _hard_denied(Path(name)):
                raise PackageError(f"Denied path in package: {name}")
    digest = sha256_file(zip_path)
    zip_path.with_suffix(zip_path.suffix + ".sha256").write_text(f"{digest}  {zip_path.name}\n", encoding="utf-8")
    return PackageResult(package_dir, zip_path, files, digest, prompt_path, False, missing_optional)
