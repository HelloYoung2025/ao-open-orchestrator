from __future__ import annotations

from pathlib import Path
from typing import Any
import ast
import json
import re

try:
    import tomllib
except ImportError:  # pragma: no cover - Python < 3.11 fallback.
    tomllib = None


STATE_SCHEMA_VERSION = 1
CONTRACT_VERSION = 1
CALLER_TYPE_ENV = "AO_CALLER_TYPE"
SESSION_ID_ENV = "AO_SESSION_ID"


class UnsupportedStateSchemaVersion(ValueError):
    def __init__(self, version: object):
        self.version = version
        super().__init__(f"unsupported_state_schema_version:{version!r}")


def empty_state() -> dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "state_revision": 0,
        "targets": {},
        "proposal_results": {},
    }


def normalize_state(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a v1 state payload or raise on unknown/future schemas.

    Legacy v1 state omitted ``schema_version`` but had the canonical
    ``state_revision``/``targets``/``proposal_results`` shape. Accept that shape
    for reads, and let the next write backfill schema_version=1.
    """
    if not isinstance(payload, dict):
        raise UnsupportedStateSchemaVersion(type(payload).__name__)

    version = payload.get("schema_version")
    if version is None:
        if _has_legacy_v1_shape(payload):
            normalized = dict(payload)
            normalized["schema_version"] = STATE_SCHEMA_VERSION
            return normalized
        raise UnsupportedStateSchemaVersion(None)

    if version != STATE_SCHEMA_VERSION:
        raise UnsupportedStateSchemaVersion(version)

    if not _has_v1_shape(payload):
        raise UnsupportedStateSchemaVersion(version)
    return dict(payload)


def validate_contract_compat(
    root: Path,
    *,
    auto_spawn_actions: tuple[str, ...] | None = None,
    gated_actions: tuple[str, ...] | None = None,
    non_executable_actions: tuple[str, ...] | None = None,
) -> str | None:
    contract_path = root / "DIRECT_PROJECT_CONTRACT.toml"
    if not contract_path.exists():
        return None

    text = contract_path.read_text(encoding="utf-8")

    if _extract_top_level_int(text, "version") != CONTRACT_VERSION:
        return "unsupported_contract_version"

    policy = _extract_toml_section(text, "continuation_policy")
    if policy is None:
        return None

    if auto_spawn_actions is not None:
        for key in ("auto_spawn_actions", "policy_authorized_actions"):
            values = _extract_string_array(policy, key)
            if values is not None and values != list(auto_spawn_actions):
                return "contract_action_vocabulary_mismatch"

    if gated_actions is not None:
        for key in ("gated_actions", "orchestrator_authorization_required_actions"):
            values = _extract_string_array(policy, key)
            if values is not None and values != list(gated_actions):
                return "contract_action_vocabulary_mismatch"

    if non_executable_actions is not None:
        for key in ("non_executable_actions", "owner_proxy_convergence_actions"):
            values = _extract_string_array(policy, key)
            if values is not None and values != list(non_executable_actions):
                return "contract_action_vocabulary_mismatch"

    return None


def read_contract_active_root(root: Path) -> Path | None:
    value = read_contract_string(root, "ao_clone_isolation", "active_root")
    if not value:
        return None
    path = Path(value).expanduser().resolve()
    return path if path.exists() else None


def read_contract_project_id(root: Path) -> str | None:
    value = read_contract_string(root, "owner_proxy", "project_id")
    return value if isinstance(value, str) and value.strip() else None


def read_contract_string(root: Path, section: str, key: str) -> str | None:
    parsed = _read_contract_toml(root)
    if parsed is not None:
        value: object = parsed
        for part in section.split("."):
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(part)
        if not isinstance(value, dict):
            return None
        scalar = value.get(key)
        return scalar if isinstance(scalar, str) and scalar.strip() else None

    section_text = read_contract_section(root, section)
    if section_text is None:
        return None
    value_match = re.search(
        rf'''(?m)^\s*{re.escape(key)}\s*=\s*(?:"((?:\\.|[^"\\])*)"|'([^']*)')\s*$''',
        section_text,
    )
    if value_match is None:
        return None
    if value_match.group(1) is not None:
        try:
            value = json.loads(f'"{value_match.group(1)}"')
        except json.JSONDecodeError:
            return None
    else:
        value = value_match.group(2)
    return value if isinstance(value, str) else None


def read_contract_section(root: Path, section: str) -> str | None:
    text = _read_contract_text(root)
    if text is None:
        return None
    return _extract_toml_section(text, section)


def _read_contract_text(root: Path) -> str | None:
    contract_path = root / "DIRECT_PROJECT_CONTRACT.toml"
    if not contract_path.exists():
        return None
    try:
        return contract_path.read_text(encoding="utf-8")
    except OSError:
        return None


def _read_contract_toml(root: Path) -> dict[str, Any] | None:
    if tomllib is None:
        return None
    text = _read_contract_text(root)
    if text is None:
        return None
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _has_legacy_v1_shape(payload: dict[str, Any]) -> bool:
    return (
        isinstance(payload.get("state_revision"), int)
        and isinstance(payload.get("targets"), dict)
        and isinstance(payload.get("proposal_results"), dict)
    )


def _has_v1_shape(payload: dict[str, Any]) -> bool:
    return _has_legacy_v1_shape(payload)


def _extract_top_level_int(text: str, key: str) -> int | None:
    prefix = f"{key} ="
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            return None
        if not stripped.startswith(prefix):
            continue
        raw = stripped[len(prefix) :].strip()
        try:
            return int(raw)
        except ValueError:
            return None
    return None


def _extract_toml_section(text: str, name: str) -> str | None:
    matches = list(re.finditer(rf"(?m)^\[{re.escape(name)}\]\s*$", text))
    if not matches:
        return None
    start = matches[-1].end()
    next_section = re.search(r"(?m)^\[", text[start:])
    end = len(text) if next_section is None else start + next_section.start()
    return text[start:end]


def _extract_string_array(section: str, key: str) -> list[str] | None:
    match = re.search(rf"(?ms)^\s*{re.escape(key)}\s*=\s*(\[[^\]]*\])", section)
    if match is None:
        return None
    try:
        value = ast.literal_eval(match.group(1))
    except (SyntaxError, ValueError):
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return []
    return value
