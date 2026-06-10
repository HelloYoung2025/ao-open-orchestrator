"""Generic escalated-review actuator (no product-specific reviewer baked in).

This is the reference actuator the orchestrator dispatch (`cli.py`) invokes for an
``escalated_review`` obligation. It is a thin, brand-neutral adapter:

  stdin  : a review-job JSON (package_path, prompt_path, package_sha256, proposal_id, nonce)
  action : verify the package sha, then shell out to a CONTRACT/ENV-configured external
           reviewer command, which must write the raw review to <raw_out>
  stdout : a JSON receipt (verdict / blocker_code / nonce / artifact_path / transcript_sha256)

The actual reviewer is pluggable via ``AO_ESCALATED_REVIEW_ACTUATOR_SCRIPT`` (a path to an
executable invoked as ``<script> run <package> <prompt> <raw_out> <timeout>``). There is NO
default reviewer — if the env is unset the actuator fails loudly. Do NOT point the contract's
``escalated_review_actuator_command`` back at this module's own env script (that would recurse).
"""
from __future__ import annotations

from pathlib import Path
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time


def main() -> int:
    try:
        job = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        return _emit({"ok": False, "error": "invalid_review_job_json", "detail": str(exc)}, 2)
    if not isinstance(job, dict):
        return _emit({"ok": False, "error": "invalid_review_job_json"}, 2)

    root = Path.cwd()
    package_path = _resolve_job_path(root, job.get("package_path"))
    prompt_path = _resolve_job_path(root, job.get("prompt_path"))
    if package_path is None:
        return _emit({"ok": False, "error": "package_missing"}, 3)
    if prompt_path is None:
        return _emit({"ok": False, "error": "prompt_missing"}, 3)

    expected_sha = job.get("package_sha256")
    actual_sha = hashlib.sha256(package_path.read_bytes()).hexdigest()
    if expected_sha != actual_sha:
        return _emit(
            {
                "ok": False,
                "error": "package_sha_mismatch",
                "expected_package_sha256": expected_sha,
                "actual_package_sha256": actual_sha,
            },
            3,
        )

    proposal_id = str(job.get("proposal_id") or "escalated-review")
    job_nonce = job.get("external_review_submission_nonce")
    nonce = (
        job_nonce
        if isinstance(job_nonce, str) and job_nonce
        else os.environ.get("AO_ESCALATED_REVIEW_SUBMISSION_NONCE")
        or f"{_safe_component(proposal_id)}-{int(time.time())}"
    )
    out_dir = root / "reports" / "escalated-review-raw"
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_out = out_dir / f"{_safe_component(proposal_id)}-{_safe_component(nonce)}.txt"

    reviewer_script = os.environ.get("AO_ESCALATED_REVIEW_ACTUATOR_SCRIPT")
    if not reviewer_script:
        return _emit(
            {
                "ok": False,
                "error": "escalated_review_actuator_script_unset",
                "detail": "set AO_ESCALATED_REVIEW_ACTUATOR_SCRIPT to your reviewer command",
            },
            3,
        )
    timeout = os.environ.get("AO_ESCALATED_REVIEW_TIMEOUT_SECONDS", "7200")
    try:
        reviewer_tokens = shlex.split(reviewer_script)
    except ValueError as exc:
        return _emit({"ok": False, "error": "invalid_actuator_script", "detail": str(exc)}, 3)
    if not reviewer_tokens:
        return _emit({"ok": False, "error": "invalid_actuator_script"}, 3)
    script_path = Path(reviewer_tokens[0]).expanduser()
    if not script_path.exists():
        return _emit(
            {"ok": False, "error": "escalated_review_actuator_missing", "script": str(script_path)},
            3,
        )

    reviewer_env = os.environ.copy()
    reviewer_env.setdefault("AO_ESCALATED_REVIEW_PROPOSAL_ID", proposal_id)
    reviewer_env.setdefault("AO_ESCALATED_REVIEW_SUBMISSION_NONCE", nonce)

    completed = subprocess.run(
        [str(script_path), *reviewer_tokens[1:], "run", str(package_path), str(prompt_path), str(raw_out), timeout],
        capture_output=True,
        text=True,
        check=False,
        env=reviewer_env,
    )
    if completed.returncode != 0:
        return _emit(
            {
                "ok": False,
                "error": "escalated_review_actuator_failed",
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            },
            3,
        )
    if not raw_out.exists():
        return _emit({"ok": False, "error": "review_artifact_missing", "artifact_path": str(raw_out)}, 3)

    raw_text = raw_out.read_text(encoding="utf-8", errors="replace")
    verdict = _classify_verdict(raw_text)
    if verdict is None:
        return _emit(
            {"ok": False, "error": "unrecognized_review_verdict", "artifact_path": str(raw_out)},
            3,
        )
    # For blocker verdicts, derive a stable blocker_code from the response content so the
    # bounded-repair counter distinguishes "same blocker unfixed" (same code -> escalate) from
    # "old blocker fixed, new blocker found" (new code -> continue). Without this, every blocker
    # round collapses into the "unspecified" bucket and the circuit breaker mis-fires.
    blocker_code = _extract_blocker_code(raw_text) if verdict == "blocker" else None
    return _emit(
        {
            "ok": True,
            "package_sha256": actual_sha,
            "verdict": verdict,
            "blocker_code": blocker_code,
            "external_review_submission_nonce": nonce,
            "artifact_path": str(raw_out),
            "model": os.environ.get("AO_ESCALATED_REVIEW_MODEL_LABEL", "escalated review actuator"),
            "model_slug": os.environ.get("AO_ESCALATED_REVIEW_MODEL_SLUG", ""),
            "transcript_sha256": hashlib.sha256(raw_text.encode("utf-8", errors="replace")).hexdigest(),
            "summary": f"Escalated review artifact captured; verdict={verdict}",
        },
        0,
    )


def _resolve_job_path(root: Path, value: object) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    full = path if path.is_absolute() else root / path
    try:
        full = full.resolve()
        full.relative_to(root.resolve())
    except ValueError:
        return None
    if not full.exists() or not full.is_file():
        return None
    return full


# Recognize the pass_with_advisory alias (the writer normalizes it -> advisory at apply time).
# Ordered BEFORE `pass` in the regex so the longer alias wins; `pass\b` never matches inside
# "pass_with_advisory" because the trailing `_` is a word char.
_SUPPORTED_VERDICTS = ("blocker", "pass_with_advisory", "pass_with_nits", "advisory", "pass")
_VERDICT_RE = re.compile(
    r'(?:["“”]?\bverdict\b["“”]?|结论)\s*[:：]\s*["“”]?'
    r"(blocker|pass_with_advisory|pass_with_nits|advisory|pass)\b",
    re.IGNORECASE,
)


def _classify_verdict(text: str) -> str | None:
    verdict: str | None = None
    for match in _VERDICT_RE.finditer(text):
        candidate = match.group(1).lower()
        if candidate in _SUPPORTED_VERDICTS:
            verdict = candidate
    return verdict


# Explicit blocker_code field (curly/straight quotes), if the reviewer provided one.
_BLOCKER_CODE_RE = re.compile(
    r'["“”]?blocker_code["“”]?\s*[:：]\s*["“”]([A-Za-z0-9_.\-]+)["“”]',
    re.IGNORECASE,
)
# First blocker finding's title, used to derive a stable slug when no explicit code is given.
_BLOCKER_TITLE_RE = re.compile(
    r'["“”]?title["“”]?\s*[:：]\s*["“”]([^"“”]{4,})["“”]',
    re.IGNORECASE,
)


def _extract_blocker_code(text: str) -> str:
    """Derive a stable blocker_code from a blocker review response.

    Preference order:
      1. An explicit non-null ``blocker_code`` field in the response.
      2. A slug derived from the first finding ``title`` (stable for the same issue,
         different for a different issue — exactly what bounded-repair counting needs).
      3. A short hash of the whole response as a last resort.
    """
    for match in _BLOCKER_CODE_RE.finditer(text):
        candidate = match.group(1).strip().lower()
        if candidate and candidate not in {"null", "none", ""}:
            return _slug(candidate)
    title_match = _BLOCKER_TITLE_RE.search(text)
    if title_match:
        return "blk-" + _slug(title_match.group(1))[:48]
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"blk-{digest}"


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "unspecified"


def _safe_component(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")
    return sanitized or "review"


def _emit(payload: dict[str, object], exit_code: int) -> int:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
