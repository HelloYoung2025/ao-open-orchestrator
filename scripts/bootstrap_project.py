#!/usr/bin/env python3
"""Bootstrap a new Agent Orchestrator project from the examples/ templates.

Renders every @TOKEN@ template in examples/ into a NEW target directory and PRINTS the
~/.agent-orchestrator/config.yaml fragment for the operator to paste. It deliberately does NOT:
  - write the global ~/.agent-orchestrator/config.yaml (the operator owns that host config),
  - write into an existing non-empty dir, a symlink, the operator's HOME, ~/.agent-orchestrator,
    this engine repo, or any directory containing a `.omx` state tree,
  - create canonical state (.omx/state) — the engine owns that on first run,
  - overwrite any existing file.

Run from the repo checkout (templates are read from ../examples relative to this script):
    python3 scripts/bootstrap_project.py --project-id my-proj --orchestrator-session my-proj-orchestrator \
        --repo-owner me --repo-name my-repo --agent claude-code \
        --worker-model my-worker-model --orchestrator-model my-orchestrator-model \
        --target-dir /path/to/new/project
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO_ROOT / "examples"
TOKEN_RE = re.compile(r"@[A-Z_]+@")
# Identifier values land in unquoted YAML scalars (repo/agent/model) and filenames; restrict them
# to a charset that is simultaneously safe for TOML/YAML(quoted+unquoted)/XML/filenames. Path and
# command inputs are NOT restricted (they may contain '/', spaces, '='); they only ever land in
# quoted scalars or free-text literal blocks, handled by per-format escaping / raw.
IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
IDENT_FIELDS = (
    "project_id", "orchestrator_session", "session_prefix",
    "repo_owner", "repo_name", "agent", "worker_model", "orchestrator_model",
)
# Path/command inputs are not charset-restricted (they may hold '/', spaces, '='), but a literal
# newline or control char would still break the rendered TOML/YAML even after quote escaping.
CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
PATH_CMD_FIELDS = ("active_root", "state_writer_cmd", "home", "path", "log_file",
                   "sidecar_path", "python_bin")


class BootstrapError(Exception):
    """Operator-facing failure (bad input or unsafe target). Fails loud, writes nothing."""


# --- per-format value escaping (raw token replacement would break quoted scalars / XML) ---
def _toml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _yaml_escape(s: str) -> str:
    # Templates wrap tokens in double-quoted scalars, so escape backslash and double-quote.
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _raw(s: str) -> str:
    return s


ESCAPERS = {"toml": _toml_escape, "yaml": _yaml_escape, "xml": _xml_escape, "raw": _raw}


def is_safe_filename(name: str) -> bool:
    """Bare filename only: no separators, no '..', not absolute, not hidden. Mirrors the engine's
    canonical-filename rule so a rendered contract never points at an unsafe governance path."""
    if not name or name != name.strip():
        return False
    if "/" in name or "\\" in name or ".." in name:
        return False
    if name.startswith(".") or Path(name).is_absolute():
        return False
    return True


def is_safe_label(label: str) -> bool:
    """launchd label / plist filename stem: no path separators or '..'."""
    return bool(label) and "/" not in label and "\\" not in label and ".." not in label


def render(text: str, token_map: dict, fmt: str) -> str:
    esc = ESCAPERS[fmt]
    for tok, val in token_map.items():
        text = text.replace(tok, esc(val))
    return text


def render_plan(args: argparse.Namespace):
    """(template filename, output path relative to target, value format)."""
    return [
        ("DIRECT_PROJECT_CONTRACT.example.toml", "DIRECT_PROJECT_CONTRACT.toml", "toml"),
        ("TODO.example.md", args.todo_file, "raw"),
        ("MASTER_PLAN.example.md", args.plan_file, "raw"),
        ("SESSION_LOG.example.md", args.session_log_file, "raw"),
        # The brain's tokens are all in `|` literal blocks (free text) or unquoted scalars
        # (repo/agent/model), never double-quoted scalars — so raw, NOT yaml escaping (which would
        # inject backslashes into literal prose). Unquoted-scalar safety is guaranteed by IDENT_RE.
        ("agent-orchestrator.example.yaml", "agent-orchestrator.yaml", "raw"),
        ("commander.example.toml", str(Path(".commander") / "commander.toml"), "toml"),
        ("com.example.ao-orchestrator-liveness.plist.tmpl", f"{args.label}.plist", "xml"),
        ("orchestrator-liveness.sh", "orchestrator-liveness.sh", "raw"),
    ]


def build_token_map(args: argparse.Namespace) -> dict:
    return {
        "@PROJECT_ID@": args.project_id,
        "@ORCHESTRATOR_SESSION@": args.orchestrator_session,
        "@SESSION_PREFIX@": args.session_prefix,
        "@REPO@": f"{args.repo_owner}/{args.repo_name}",
        "@REPO_OWNER@": args.repo_owner,
        "@REPO_NAME@": args.repo_name,
        "@AGENT@": args.agent,
        "@WORKER_MODEL@": args.worker_model,
        "@ORCHESTRATOR_MODEL@": args.orchestrator_model,
        "@ACTIVE_ROOT@": args.active_root,
        "@PLAN_FILE@": args.plan_file,
        "@TODO_FILE@": args.todo_file,
        "@SESSION_LOG_FILE@": args.session_log_file,
        "@STATE_WRITER_CMD@": args.state_writer_cmd,
        "@PYTHON_BIN@": args.python_bin,
        "@HOME@": args.home,
        "@PATH@": args.path,
        "@LABEL@": args.label,
        "@SIDECAR_PATH@": args.sidecar_path,
        "@LOG_FILE@": args.log_file,
    }


def check_target(target: Path) -> Path:
    """Resolve and validate the target dir. Raises BootstrapError; writes nothing."""
    if target.is_symlink():
        raise BootstrapError(f"refusing symlink target: {target}")
    resolved = target.resolve()
    home = Path.home().resolve()
    forbidden_exact = {home, (home / ".agent-orchestrator").resolve(), REPO_ROOT.resolve()}
    if resolved in forbidden_exact:
        raise BootstrapError(f"refusing to render into a protected location: {resolved}")
    # Never render into this engine repo (== or under it).
    if resolved == REPO_ROOT.resolve() or REPO_ROOT.resolve() in resolved.parents:
        raise BootstrapError(f"refusing to render inside the engine repo: {resolved}")
    # Never render into a canonical-state tree (also structurally rules out an active AO clone,
    # which is always non-empty and carries .omx/state — without naming any private path here).
    if ".omx" in resolved.parts:
        raise BootstrapError(f"refusing to render inside a .omx state tree: {resolved}")
    if resolved.exists():
        if not resolved.is_dir():
            raise BootstrapError(f"target exists and is not a directory: {resolved}")
        if any(resolved.iterdir()):
            raise BootstrapError(f"refusing to render into a non-empty directory: {resolved}")
    return resolved


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Bootstrap a new Agent Orchestrator project from templates.")
    # Identity (required).
    p.add_argument("--project-id", required=True)
    p.add_argument("--orchestrator-session", required=True)
    p.add_argument("--repo-owner", required=True)
    p.add_argument("--repo-name", required=True)
    p.add_argument("--agent", required=True, help="agent runtime, e.g. claude-code")
    p.add_argument("--worker-model", required=True)
    p.add_argument("--orchestrator-model", required=True)
    p.add_argument("--target-dir", required=True)
    # Defaulted / derived (override as needed).
    p.add_argument("--session-prefix", default=None,
                   help="default: orchestrator-session with a trailing '-orchestrator' stripped")
    p.add_argument("--active-root", default=None, help="default: the resolved target dir")
    p.add_argument("--plan-file", default="MASTER_PLAN.md")
    p.add_argument("--todo-file", default="TODO.md")
    p.add_argument("--session-log-file", default="SESSION_LOG.md")
    p.add_argument("--state-writer-cmd", default="ao-state-writer")
    p.add_argument("--python-bin", default="python3")
    p.add_argument("--home", default=None, help="default: the current user's home")
    p.add_argument("--path", default=None,
                   help="PATH the sidecar runs with; MUST include node, `ao`, and the engine command. "
                        "Default: composed from where tmux/ao/node/the engine/python actually resolve "
                        "right now, plus <home>/.npm-global/bin, <home>/.local/bin and the system dirs "
                        "(launchd does NOT inherit the user shell PATH)")
    p.add_argument("--label", default=None, help="launchd label / plist stem; default: ao-orchestrator-liveness.<project-id>")
    p.add_argument("--log-file", default=None)
    p.add_argument("--sidecar-path", default=None,
                   help="install path the plist EXECUTES (the script itself is still rendered into "
                        "<target>; copy it here before loading the plist). Default: "
                        "<home>/.agent-orchestrator/<project-id>-orchestrator-liveness.sh — NOT inside "
                        "the project: macOS TCC blocks launchd from executing under ~/Documents")
    return p.parse_args(argv)


def finalize_args(args: argparse.Namespace, resolved_target: Path) -> argparse.Namespace:
    if args.session_prefix is None:
        s = args.orchestrator_session
        args.session_prefix = s[:-len("-orchestrator")] if s.endswith("-orchestrator") else s
    if args.active_root is None:
        args.active_root = str(resolved_target)
    if args.home is None:
        args.home = str(Path.home())
    if args.label is None:
        args.label = f"ao-orchestrator-liveness.{args.project_id}"
    if args.log_file is None:
        args.log_file = str(Path(args.home) / ".agent-orchestrator" / f"{args.project_id}-orchestrator-liveness.log")
    if args.path is None:
        # launchd agents run with a minimal PATH; the user's shell PATH is never inherited.
        # Missing tmux made a live sidecar misjudge "orchestrator absent" (duplicate-revival
        # risk), and a missing engine command blinded its canonical sweep (2026-06-12).
        # Compose the PATH from where the critical commands ACTUALLY resolve right now —
        # this covers any Homebrew prefix, npm-global, and pipx without baking one
        # machine's install layout into rendered artifacts.
        dirs: list[str] = []
        # "claude"/"codex" are the agent binaries the revive launch script execs — a PATH
        # that resolves ao but not the agent revives a DEAD orchestrator pane (live failure
        # 2026-06-12: `claude: command not found` after a guarded restart).
        for cmd in ("tmux", "ao", "node", "claude", "codex",
                    args.state_writer_cmd.split()[0], args.python_bin):
            hit = shutil.which(cmd)
            if hit:
                d = str(Path(hit).parent)
                if d not in dirs:
                    dirs.append(d)
        for d in (str(Path(args.home) / ".npm-global" / "bin"),
                  str(Path(args.home) / ".local" / "bin"),
                  "/usr/local/bin", "/usr/bin", "/bin"):
            if d not in dirs:
                dirs.append(d)
        args.path = ":".join(dirs)
    if args.sidecar_path is None:
        # The plist must execute the sidecar from OUTSIDE the project: macOS TCC denies
        # launchd execution under ~/Documents (live exit 126, 2026-06-12).
        args.sidecar_path = str(
            Path(args.home) / ".agent-orchestrator" / f"{args.project_id}-orchestrator-liveness.sh"
        )
    return args


def run(args: argparse.Namespace) -> list[Path]:
    resolved = check_target(Path(args.target_dir).expanduser())
    args = finalize_args(args, resolved)
    for field in IDENT_FIELDS:
        value = getattr(args, field)
        if not IDENT_RE.match(value):
            raise BootstrapError(
                f"unsafe --{field.replace('_', '-')}: {value!r} "
                f"(allowed: letters/digits/._- , starting alphanumeric)")
    for label, value in (("plan-file", args.plan_file), ("todo-file", args.todo_file),
                         ("session-log-file", args.session_log_file)):
        if not is_safe_filename(value):
            raise BootstrapError(f"unsafe canonical {label}: {value!r} (bare filename only)")
    if not is_safe_label(args.label):
        raise BootstrapError(f"unsafe --label: {args.label!r}")
    for field in PATH_CMD_FIELDS:
        value = getattr(args, field)
        if value is not None and CTRL_RE.search(value):
            raise BootstrapError(
                f"control character in --{field.replace('_', '-')} is not allowed "
                f"(a newline/control char would break the rendered TOML/YAML)")
    token_map = build_token_map(args)

    # Pre-render everything (and assert no unresolved tokens) BEFORE writing anything.
    staged: list[tuple[Path, str]] = []
    for tmpl_name, out_rel, fmt in render_plan(args):
        tmpl = TEMPLATES_DIR / tmpl_name
        if not tmpl.is_file():
            raise BootstrapError(f"missing template: {tmpl}")
        rendered = render(tmpl.read_text(encoding="utf-8"), token_map, fmt)
        leftover = TOKEN_RE.search(rendered)
        if leftover:
            raise BootstrapError(f"unresolved placeholder {leftover.group()} in {tmpl_name}")
        staged.append((resolved / out_rel, rendered))

    # Write (no-overwrite). The .omx/state tree is intentionally NOT created.
    # Best-effort transactional: on any failure mid-write, roll back the files we created
    # so an I/O error never leaves a half-rendered project behind.
    resolved.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    try:
        for out_path, content in staged:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            if out_path.exists():
                raise BootstrapError(f"refusing to overwrite existing file: {out_path}")
            out_path.write_text(content, encoding="utf-8")
            written.append(out_path)
    except BaseException:
        for p in reversed(written):
            try:
                p.unlink()
            except OSError:
                pass
        raise
    return written


def config_fragment(args: argparse.Namespace) -> str:
    return render((TEMPLATES_DIR / "agent-orchestrator.config.example.yaml").read_text(encoding="utf-8"),
                  build_token_map(args), "yaml")


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        written = run(args)
    except BootstrapError as exc:
        print(f"bootstrap: {exc}", file=sys.stderr)
        return 2
    print(f"Rendered {len(written)} files into {Path(args.target_dir).expanduser().resolve()}:")
    for p in written:
        print(f"  {p}")
    print("\n--- paste this block into ~/.agent-orchestrator/config.yaml "
          "(NOT written for you — the global AO config is operator-owned) ---\n")
    print(config_fragment(args))
    print("--- next: `ao start <project-id>`, then externally bootstrap the first slice. ---")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
