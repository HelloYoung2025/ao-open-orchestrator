from __future__ import annotations

from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
# Skip VCS, caches, build outputs, dependency trees, and gitignored local runtime state (.omx).
# These never ship in the published artifact (they are gitignored), so scanning them only produces
# false positives from local-only state; the scan must cover what gets published, i.e.
# tracked/publishable files.
SKIP_DIRS = {".git", ".pytest_cache", "__pycache__", ".venv", "dist", "build", ".omx", "node_modules"}

# Data/binary artifacts the text scan would silently skip (read_text -> UnicodeDecodeError). They
# can carry session prefixes, project ids, and absolute paths INSIDE them, so a leak would pass the
# text gate unseen. Flag them explicitly instead of skipping.
DENY_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm", ".zip"}

FORBIDDEN = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        # --- absolute host paths ---
        "/" + "Users/",
        "/" + "home/",
        r"[A-Za-z]:\\\\",
        "/" + "opt/" + "homebrew",          # private interpreter prefix
        # --- owner / machine identity ---
        "young" + "hu",
        # NB: ".agent-orchestrator" is intentionally NOT forbidden — it is the PUBLIC @aoagents/ao
        # runtime convention the engine integrates with (e.g. cli._ao_projects_root / `ao session ls`),
        # not owner/machine identity, and hardcoding AO's default keeps the engine in lock-step with the
        # AO CLI root (a config-only override would risk drift). A genuine machine leak that embeds it
        # under an absolute home root is still caught by the absolute-home-root + owner-handle patterns
        # above.
        # --- private product identity ---
        # NB: the bare owner handle is intentionally NOT forbidden — the public repo is itself
        # owned by that account, so its own URLs (security advisories, etc.) are legitimate. The
        # leak risk is the PRIVATE product repo slug, which the pattern below catches.
        "Claw" + "Code",                     # private product repo slug
        "claw-" + "commander",               # private dispatch path component
        "ccai" + "bao",                      # private session prefix
        "claw-code-" + "aibase",             # private project id prefix
        "0d517" + "32349",                   # private project id hash
        # --- governance canonical filenames (shipping literally re-Claws the template) ---
        "CLAW_" + "WORKBENCH",
        # --- phase tags / opaque ids ---
        r"\b" + "A" + "18" + r"\b",
        r"\b" + "A" + "19" + r"\b",
        "/" + "tmp/" + "a" + "18",
        r"\bconversation[_-]?id\b",
        r"\bthread[_-]?id\b",
        r"\brunner[_-]?id\b",
        r"\bremote[_-]?id\b",
        # --- secrets ---
        r"\bsk-[A-Za-z0-9]{20,}\b",
        r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b",
        # --- product business-domain backstop (a literal tripwire only; the real semantic leak
        #     audit is a separate human+codex gate). Identifier-aware boundaries (NOT \b, which
        #     treats "_" as a word char and would miss the term inside snake_case identifiers).
        #     Split so this file never matches its own source. ---
        r"(?<![A-Za-z0-9])S" + r"FW(?![A-Za-z0-9])",
        r"(?<![A-Za-z0-9])NS" + r"FW(?![A-Za-z0-9])",
        # --- proprietary review-actuator brand vocabulary (removed from the brand-neutral public
        #     engine; locked here so a future edit can never silently reintroduce it — this gate is
        #     the automated backstop, the semantic audit is a separate human+codex gate). Split so
        #     this file never matches its own source; IGNORECASE covers every case/hyphen variant.
        #     NB: the bare 3-letter model prefix is intentionally NOT forbidden — the legitimate
        #     model string in writer.py contains it; only the brand "<prefix><sep>pro" form is. ---
        "gp" + r"t[ _-]?pro",                                  # <prefix>_pro / <prefix>-pro / "<prefix> pro"
        "chat" + "gpt",                                        # chat-model brand
        r"gp" + r"t[- _]?[0-9][0-9.]*[ _-]?pro",               # versioned brand label, e.g. "<model> Pro"
        r"(?<![A-Za-z0-9])cd" + r"p(?![A-Za-z0-9])",           # browser protocol acronym
        "desk" + r"top[ _-]?(?:actuator|adapter|bridge|review|transport)",  # narrowed local-adapter family
        r"(?<![A-Za-z0-9])Dev" + r"Tools(?![A-Za-z0-9])",      # spelled-out browser protocol
    ]
]


def iter_files(root: Path) -> list[Path]:
    # An explicitly-named FILE root must scan THAT file: rglob("*") returns [] on a file, which is
    # the silent file-arg false-pass this gate must never have. The caller named it on purpose, so
    # SKIP_DIRS filtering does not apply.
    if root.is_file():
        return [root]
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        # Skip cache/build/runtime dirs RELATIVE to the supplied root, so a default repo scan still
        # skips build/.omx/etc., but an explicitly-named root (e.g. build/ or .omx/) is authoritative
        # and fully scanned instead of silently skipped.
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        files.append(path)
    return files


def scan_root(root: Path, findings: list[str]) -> None:
    base = root.parent if root.is_file() else root
    for path in iter_files(root):
        rel = path.relative_to(base)
        # Data/binary artifacts are skipped by the text scan, so flag them explicitly rather than
        # letting a leak slip through a UnicodeDecodeError.
        if path.suffix.lower() in DENY_SUFFIXES:
            findings.append(f"{rel}: denied data/binary artifact ({path.suffix}); do not publish")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # Non-deny binary (e.g. an image). The text gate cannot inspect it; leave it to the
            # DENY_SUFFIXES list above to catch the data artifacts that actually carry leaks.
            continue
        for index, line in enumerate(text.splitlines(), start=1):
            for pattern in FORBIDDEN:
                if pattern.search(line):
                    findings.append(f"{rel}:{index}: {pattern.pattern}")


def main() -> int:
    # Default to the repo root; accept extra roots on argv so the SAME gate can scan sibling publish
    # artifacts (e.g. a packages/<npm-plugin> dir or a sidecar/ template tree) that live outside the
    # Python package and would otherwise never be scanned by the repo CI.
    #
    # SCOPE: this gate scans source/template TEXT (and flags data/binary artifacts via
    # DENY_SUFFIXES). Built wheels/sdists under dist/ derive from these scanned sources; this gate
    # does not unpack release archives — that remains the release workflow's responsibility.
    roots = [Path(arg).resolve() for arg in sys.argv[1:]] or [ROOT]
    findings: list[str] = []
    for root in roots:
        if not root.exists():
            findings.append(f"{root}: scan root does not exist (fail-closed)")
            continue
        if not (root.is_file() or root.is_dir()):
            findings.append(f"{root}: scan root is neither file nor directory (fail-closed)")
            continue
        scan_root(root, findings)
    if findings:
        print("public safety scan failed")
        for finding in findings:
            print(finding)
        return 1
    print("public safety scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
