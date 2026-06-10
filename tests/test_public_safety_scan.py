"""Regression tests for the public leak gate ``scripts/public_safety_scan.py``.

Every forbidden token used as a FIXTURE is assembled at RUNTIME (e.g. ``"Claw" + "Code"``), never
written as a contiguous literal, because this committed test file is itself scanned by the gate in
CI — a literal forbidden token here would poison the gate. The fixtures are written into pytest's
``tmp_path`` (never committed), so only the gate-under-test ever sees the assembled tokens.

WHY each test exists (intent, not just behavior):
  * the gate is a SECURITY boundary for a public repo; a false-pass leaks private data and a
    false-fail blocks publishing, so the regressions pin the two adversarially-found holes
    (file-arg false-pass, identifier-bounded domain term) plus the fail-closed contract.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "public_safety_scan.py"

# Forbidden tokens, assembled at runtime so this source carries no contiguous forbidden literal.
PRODUCT_SLUG = "Claw" + "Code"          # private product repo slug
DOMAIN = "S" + "FW"                     # product business-domain term
DOMAIN_N = "NS" + "FW"

# Brand-vocabulary tokens (V1/V2 removed these from the engine; the gate now re-catches them so a
# future edit cannot silently reintroduce the brand). Assembled at runtime — no contiguous literal.
BRAND_ACTUATOR = "gp" + "t_pro"          # removed snake_case brand actuator token
BRAND_ACTUATOR_CASE = "GP" + "T-Pro"     # exact case/hyphen variant the V2 cascade missed
BRAND_CHAT_MODEL = "chat" + "gpt"        # removed chat-model brand
BRAND_VERSIONED = "gp" + "t-5.5 Pro"     # versioned brand label (caught; bare model below is legit)
BRAND_PROTO = "browser_" + "cd" + "p"    # browser-protocol acronym in snake_case
BRAND_ADAPTER = "desk" + "top_bridge"    # narrowed local-adapter family member
BRAND_SPELLED_PROTO = "Dev" + "Tools"         # spelled-out browser protocol
LEGIT_MODEL = "gp" + "t-5.5"             # legitimate model string — must NOT be flagged


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
    )


def test_file_root_with_leak_is_caught(tmp_path: Path) -> None:
    """A FILE named as the scan root must be scanned. Regression: ``rglob('*')`` returns [] on a
    file, so a named file silently passed even when it carried a leak."""
    leak = tmp_path / "note.txt"
    leak.write_text(f"see the {PRODUCT_SLUG} repo\n", encoding="utf-8")
    result = _run(str(leak))
    assert result.returncode == 1, result.stdout
    assert "scan failed" in result.stdout


def test_domain_term_in_snake_case_is_caught(tmp_path: Path) -> None:
    """The domain term must be caught even inside snake_case identifiers. Regression: Python ``\\b``
    treats ``_`` as a word char, so a ``\\bTERM\\b`` pattern would miss ``term_classifier`` /
    ``is_term_label`` — the gate uses identifier-aware (non-alnum) boundaries instead."""
    module = tmp_path / "mod.py"
    module.write_text(
        f"{DOMAIN.lower()}_classifier = 1\nis_{DOMAIN_N.lower()}_label = 2\n",
        encoding="utf-8",
    )
    result = _run(str(module))
    assert result.returncode == 1, result.stdout
    assert "scan failed" in result.stdout


def test_missing_root_fails_closed(tmp_path: Path) -> None:
    """A typo'd / non-existent root must fail closed, never silently pass."""
    result = _run(str(tmp_path / "does-not-exist"))
    assert result.returncode == 1, result.stdout
    assert "does not exist" in result.stdout


def test_skip_dir_name_as_explicit_root_is_scanned(tmp_path: Path) -> None:
    """An explicitly-named root whose own basename is in SKIP_DIRS (e.g. ``build/``) is
    authoritative and fully scanned. Regression: skip-filtering on the full path silently dropped
    every file when the caller pointed the gate AT a skip-named dir."""
    skip_named = tmp_path / "build"
    skip_named.mkdir()
    (skip_named / "leak.txt").write_text(f"{PRODUCT_SLUG}\n", encoding="utf-8")
    result = _run(str(skip_named))
    assert result.returncode == 1, result.stdout
    assert "scan failed" in result.stdout


def test_skip_dir_below_root_is_still_skipped(tmp_path: Path) -> None:
    """Default behavior preserved: a SKIP_DIRS dir BELOW the scanned root is skipped, so local-only
    runtime/build state does not produce false failures."""
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "leak.txt").write_text(f"{PRODUCT_SLUG}\n", encoding="utf-8")
    (tmp_path / "clean.txt").write_text("a generic orchestrator file\n", encoding="utf-8")
    result = _run(str(tmp_path))
    assert result.returncode == 0, result.stdout
    assert "scan passed" in result.stdout


def test_clean_dir_passes(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("nothing private here\n", encoding="utf-8")
    result = _run(str(tmp_path))
    assert result.returncode == 0, result.stdout


def test_gate_script_does_not_self_match() -> None:
    """The gate's own source must pass when scanned as a file: split-built patterns and the
    domain-free comments leave no contiguous forbidden literal in it."""
    result = _run(str(SCRIPT))
    assert result.returncode == 0, result.stdout


def test_brand_actuator_snake_token_is_caught(tmp_path: Path) -> None:
    """The removed proprietary actuator vocabulary must be re-caught so a future edit cannot
    silently reintroduce the brand. WHY: V1/V2 desanitized the engine; this gate is the automated
    backstop that keeps it brand-neutral."""
    f = tmp_path / "mod.py"
    f.write_text(f"{BRAND_ACTUATOR}_handler = 1\n", encoding="utf-8")
    result = _run(str(f))
    assert result.returncode == 1, result.stdout
    assert "scan failed" in result.stdout


def test_brand_actuator_case_variant_is_caught(tmp_path: Path) -> None:
    """The exact case/hyphen variant the V2 cascade missed. WHY: case-sensitive grep let it slip;
    the gate's IGNORECASE is the backstop that pins this specific regression."""
    f = tmp_path / "doc.md"
    f.write_text(f"see the {BRAND_ACTUATOR_CASE} flow\n", encoding="utf-8")
    result = _run(str(f))
    assert result.returncode == 1, result.stdout


def test_brand_chat_model_is_caught(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text(f"uses {BRAND_CHAT_MODEL} for review\n", encoding="utf-8")
    result = _run(str(f))
    assert result.returncode == 1, result.stdout


def test_brand_versioned_label_is_caught(tmp_path: Path) -> None:
    """A versioned brand label (model name + Pro) must be caught even though the bare model string
    is legitimate (see test_legit_model_string_is_allowed)."""
    f = tmp_path / "b.txt"
    f.write_text(f"escalate to {BRAND_VERSIONED}\n", encoding="utf-8")
    result = _run(str(f))
    assert result.returncode == 1, result.stdout


def test_brand_protocol_acronym_is_caught(tmp_path: Path) -> None:
    f = tmp_path / "c.py"
    f.write_text(f"{BRAND_PROTO}_required = True\n", encoding="utf-8")
    result = _run(str(f))
    assert result.returncode == 1, result.stdout


def test_brand_adapter_family_is_caught(tmp_path: Path) -> None:
    """The narrowed local-adapter family (suffix-qualified) must be caught."""
    f = tmp_path / "d.py"
    f.write_text(f"{BRAND_ADAPTER} = object()\n", encoding="utf-8")
    result = _run(str(f))
    assert result.returncode == 1, result.stdout


def test_brand_spelled_protocol_is_caught(tmp_path: Path) -> None:
    f = tmp_path / "e.md"
    f.write_text(f"drives the {BRAND_SPELLED_PROTO} endpoint\n", encoding="utf-8")
    result = _run(str(f))
    assert result.returncode == 1, result.stdout


def test_legit_model_string_is_allowed(tmp_path: Path) -> None:
    """CRITICAL no-false-positive contract: the legitimate bare model string must NOT be flagged,
    even though the brand pattern shares its 3-letter prefix. Pins that the brand lock never blocks
    publishing over the real model identifier."""
    f = tmp_path / "model.py"
    f.write_text(f'CODEX_CC_MODEL = "{LEGIT_MODEL}"\n', encoding="utf-8")
    result = _run(str(f))
    assert result.returncode == 0, result.stdout
    assert "scan passed" in result.stdout
