from __future__ import annotations

from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".pytest_cache", "__pycache__", ".venv", "dist", "build"}
FORBIDDEN = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        "/" + "Users/",
        "young" + "hu",
        r"\." + "agent-orchestrator",
        "ccai" + "bao",
        "claw-code-" + "aibase",
        "CLAW_" + "WORKBENCH",
        r"\b" + "A" + "18" + r"\b",
        r"\b" + "A" + "19" + r"\b",
        "/" + "tmp/" + "a" + "18",
    ]
]


def iter_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        files.append(path)
    return files


def main() -> int:
    findings: list[str] = []
    for path in iter_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rel = path.relative_to(ROOT)
        for index, line in enumerate(text.splitlines(), start=1):
            for pattern in FORBIDDEN:
                if pattern.search(line):
                    findings.append(f"{rel}:{index}: {pattern.pattern}")
    if findings:
        print("public safety scan failed")
        for finding in findings:
            print(finding)
        return 1
    print("public safety scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
