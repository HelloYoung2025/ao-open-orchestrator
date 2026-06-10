from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

try:  # Python 3.11+
    import tomllib  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - exercised on Python < 3.11
    tomllib = None


CONFIG_DIR = ".commander"
CONFIG_FILE = "commander.toml"

# Generic include names collected in addition to the canonical plan/todo files. Generalized from
# the LIVE package's hardcoded set so the include surface is config-driven (defaults stay generic).
DEFAULT_EXTRA_INCLUDE_NAMES = ("AGENTS.md", "README.md")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class CommanderConfig:
    project_name: str
    project_root: Path
    master_plan_path: Path
    todo_path: Path
    # Worker target: a Codex CLI session/thread reference the worker adapter resumes. Required.
    codex_worker_thread_id: str
    # Reviewer target: a brand-neutral, transport-agnostic identifier for the external review
    # destination (queue name, ticket id, review thread label, ...). Generalized from the LIVE
    # package's brand-laden reviewer field to drop the product-brand token; the required-field
    # role is retained. Required.
    reviewer_target: str
    heartbeat_interval_seconds: int = 180
    no_new_output_limit: int = 20
    external_review_auto_submit: bool = True
    phase_close_auto_accept: bool = False
    package_preparation_authorized: bool = False
    default_next_slice_prompt: str = "Please start the current canonical TODO next slice."
    # Optional operator-supplied submission command. When set, the reviewer adapter shells out to
    # it (``<command> <package_path> <prompt_path>``); when empty, the default fail-loud manual
    # reviewer is used. No GUI/app automation is hardcoded in the public package.
    reviewer_submit_command: str = ""
    extra_include_names: tuple[str, ...] = DEFAULT_EXTRA_INCLUDE_NAMES
    include_paths: tuple[Path, ...] = field(default_factory=tuple)
    exclude_patterns: tuple[str, ...] = field(default_factory=tuple)

    @property
    def commander_dir(self) -> Path:
        return self.project_root / CONFIG_DIR

    @property
    def ledger_path(self) -> Path:
        return self.commander_dir / "ledger.jsonl"

    @property
    def state_path(self) -> Path:
        return self.commander_dir / "state.json"

    @property
    def package_dir(self) -> Path:
        return self.commander_dir / "review-packages"

    @property
    def lock_dir(self) -> Path:
        return self.commander_dir / "locks"


def _resolve(project_root: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return (project_root / path).resolve()


def config_path(project: str | Path) -> Path:
    return Path(project).expanduser().resolve() / CONFIG_DIR / CONFIG_FILE


def _parse_scalar(raw: str):
    value = raw.strip()
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    if value in {"true", "false"}:
        return value == "true"
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part.strip()) for part in inner.split(",")]
    try:
        return int(value)
    except ValueError:
        return value


def _minimal_toml_loads(text: str) -> dict:
    data: dict = {}
    current = data
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip()
            current = data.setdefault(section, {})
            continue
        if "=" not in stripped:
            continue
        key, raw = stripped.split("=", 1)
        current[key.strip()] = _parse_scalar(raw)
    return data


def _loads_toml(text: str) -> dict:
    if tomllib is not None:
        return tomllib.loads(text)
    return _minimal_toml_loads(text)


def load_config(project: str | Path) -> CommanderConfig:
    project_root = Path(project).expanduser().resolve()
    path = config_path(project_root)
    if not path.exists():
        raise ConfigError(f"Missing config: {path}")

    data = _loads_toml(path.read_text(encoding="utf-8"))
    required = [
        "project_name",
        "project_root",
        "master_plan_path",
        "todo_path",
        "codex_worker_thread_id",
        "reviewer_target",
    ]
    missing = [key for key in required if key not in data]
    if missing:
        raise ConfigError(f"Missing required config field(s): {', '.join(missing)}")

    configured_root = _resolve(project_root, data["project_root"])
    review = data.get("review", {})
    include_paths = tuple(_resolve(configured_root, item) for item in review.get("include_paths", []))
    exclude_patterns = tuple(review.get("exclude_patterns", []))
    extra_include_names = tuple(review.get("extra_include_names", DEFAULT_EXTRA_INCLUDE_NAMES))

    cfg = CommanderConfig(
        project_name=str(data["project_name"]),
        project_root=configured_root,
        master_plan_path=_resolve(configured_root, data["master_plan_path"]),
        todo_path=_resolve(configured_root, data["todo_path"]),
        codex_worker_thread_id=str(data["codex_worker_thread_id"]),
        reviewer_target=str(data["reviewer_target"]),
        heartbeat_interval_seconds=int(data.get("heartbeat_interval_seconds", 180)),
        no_new_output_limit=int(data.get("no_new_output_limit", 20)),
        external_review_auto_submit=bool(data.get("external_review_auto_submit", True)),
        phase_close_auto_accept=bool(data.get("phase_close_auto_accept", False)),
        package_preparation_authorized=bool(data.get("package_preparation_authorized", False)),
        default_next_slice_prompt=str(
            data.get("default_next_slice_prompt", "Please start the current canonical TODO next slice.")
        ),
        reviewer_submit_command=str(data.get("reviewer_submit_command", "")),
        extra_include_names=extra_include_names,
        include_paths=include_paths,
        exclude_patterns=exclude_patterns,
    )
    if cfg.phase_close_auto_accept:
        raise ConfigError("phase_close_auto_accept=true is forbidden by Commander Protocol v1")
    return cfg


def default_config_text(project_root: Path) -> str:
    project_name = project_root.name or "New Project"
    return f'''project_name = "{project_name}"
project_root = "."
master_plan_path = "MASTER_PLAN.md"
todo_path = "TODO.md"
codex_worker_thread_id = ""
reviewer_target = ""
heartbeat_interval_seconds = 180
no_new_output_limit = 20
external_review_auto_submit = true
phase_close_auto_accept = false
# Standalone Commander package-preparation guard. Leave false until the owner authorizes
# package preparation for the current major phase.
package_preparation_authorized = false
default_next_slice_prompt = "Please start the current canonical TODO next slice."
# Optional. When set, `submit-review` shells out to this command as
# `<command> <package_path> <prompt_path>`. When empty, the default manual reviewer is used
# (it validates the package and prints submission instructions; the operator submits).
reviewer_submit_command = ""

[review]
# Extra generic files collected into the package alongside the canonical plan/todo files.
extra_include_names = ["AGENTS.md", "README.md"]
include_paths = []
exclude_patterns = [".git", ".env", ".codex", ".omx/state", "target", "node_modules", "__pycache__"]
'''


def init_project(project: str | Path) -> list[Path]:
    root = Path(project).expanduser().resolve()
    commander_dir = root / CONFIG_DIR
    commander_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    cfg_path = commander_dir / CONFIG_FILE
    if not cfg_path.exists():
        cfg_path.write_text(default_config_text(root), encoding="utf-8")
        written.append(cfg_path)

    master = root / "MASTER_PLAN.md"
    if not master.exists():
        master.write_text(
            "# Master Plan\n\nStatus: draft\n\n## Goal\n\nDescribe the project goal before enabling Commander.\n",
            encoding="utf-8",
        )
        written.append(master)

    todo = root / "TODO.md"
    if not todo.exists():
        todo.write_text(
            "# TODO\n\nStatus: draft\n\n## Locked Sequence\n\n- [ ] Define the first slice.\n\n"
            "## Session Checkpoint Log\n\n",
            encoding="utf-8",
        )
        written.append(todo)

    return written
