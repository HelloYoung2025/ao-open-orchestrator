from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .adapters import CodexAdapter, doctor, make_reviewer
from .config import ConfigError, init_project, load_config
from .package_review import PackageError, create_review_package
from .protocol import classify
from .state import ActionRecord, LockHeld, append_ledger, read_state, single_flight, write_state


def _project_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", required=True, help="Target project root")


def cmd_init(args: argparse.Namespace) -> int:
    written = init_project(args.project)
    if written:
        print("Created:")
        for path in written:
            print(f"- {path}")
        print("Status: paused until owner confirms generated templates and commander.toml")
    else:
        print("No files created; commander setup already exists")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    try:
        cfg = load_config(args.project)
        checks = doctor(cfg.project_root, cfg.codex_worker_thread_id, cfg.reviewer_submit_command)
        checks.insert(0, ("master plan", cfg.master_plan_path.exists(), str(cfg.master_plan_path)))
        checks.insert(1, ("todo", cfg.todo_path.exists(), str(cfg.todo_path)))
        checks.insert(
            2,
            ("codex_worker_thread_id configured", bool(cfg.codex_worker_thread_id), cfg.codex_worker_thread_id or "empty"),
        )
        checks.insert(
            3,
            ("reviewer_target configured", bool(cfg.reviewer_target), cfg.reviewer_target or "empty"),
        )
    except ConfigError as exc:
        print(f"Config: FAIL - {exc}")
        checks = doctor(Path(args.project).expanduser().resolve())
    failed = False
    for name, ok, detail in checks:
        failed = failed or not ok
        print(f"{name}: {'PASS' if ok else 'FAIL'} - {detail}")
    return 1 if failed else 0


def cmd_brief(args: argparse.Namespace) -> int:
    cfg = load_config(args.project)
    brief = classify(cfg)
    print(brief.render())
    append_ledger(cfg.ledger_path, ActionRecord("brief", "ok", {"mode": brief.mode.value}))
    return 0


def cmd_heartbeat(args: argparse.Namespace) -> int:
    cfg = load_config(args.project)
    try:
        with single_flight(cfg.lock_dir):
            codex = CodexAdapter(cfg.project_root, cfg.codex_worker_thread_id).observe()
            brief = classify(cfg)
            print(brief.render())
            print(f"Codex worker observed: {'yes' if codex.get('ok') else 'no'}")
            if args.dispatch and brief.mode.value == "small_slice":
                print("Visible Terminal command:")
                print(CodexAdapter(cfg.project_root, cfg.codex_worker_thread_id).visible_terminal_command())
                print("Inner Plan Mode prompt:")
                print(cfg.default_next_slice_prompt)
            append_ledger(
                cfg.ledger_path,
                ActionRecord(
                    "heartbeat",
                    "ok",
                    {"mode": brief.mode.value, "codex_observed": bool(codex.get("ok")), "dispatch": bool(args.dispatch)},
                ),
            )
    except LockHeld as exc:
        print(
            f"Mode: deferred\nCurrent fact: {exc}\nRisk: overlapping commander run\n"
            "Next allowed action: wait\nForbidden shortcut: do not start another run"
        )
        return 0
    return 0


def cmd_package_review(args: argparse.Namespace) -> int:
    cfg = load_config(args.project)
    result = create_review_package(cfg, dry_run=args.dry_run, owner_authorized=args.owner_authorized)
    if result.dry_run:
        print(f"Dry-run package directory: {result.package_dir}")
        print("Files:")
        for path in result.files:
            print(f"- {path}")
        if result.missing_optional:
            print("Skipped (missing optional inputs):")
            for path in result.missing_optional:
                print(f"- {path}")
        append_ledger(
            cfg.ledger_path,
            ActionRecord(
                "package-review",
                "dry-run",
                {"files": len(result.files), "missing_optional": len(result.missing_optional)},
            ),
        )
        return 0
    state = read_state(cfg.state_path)
    state.update(
        {
            "status": "package_ready",
            "package_path": str(result.zip_path),
            "package_sha256": result.sha256,
            "prompt_path": str(result.prompt_path),
        }
    )
    write_state(cfg.state_path, state)
    append_ledger(
        cfg.ledger_path,
        ActionRecord(
            "package-review",
            "ok",
            {"zip_path": str(result.zip_path), "sha256": result.sha256, "files": len(result.files)},
        ),
    )
    print(f"Package: {result.zip_path}")
    print(f"SHA-256: {result.sha256}")
    print(f"Prompt: {result.prompt_path}")
    return 0


def cmd_submit_review(args: argparse.Namespace) -> int:
    cfg = load_config(args.project)
    state = read_state(cfg.state_path)
    package_path = Path(args.package or state.get("package_path", ""))
    prompt_path = Path(args.prompt or state.get("prompt_path", ""))
    if not package_path.exists():
        raise FileNotFoundError(f"Missing package: {package_path}")
    if not prompt_path.exists():
        raise FileNotFoundError(f"Missing prompt: {prompt_path}")
    if not cfg.external_review_auto_submit and not args.dry_run:
        raise PermissionError("external_review_auto_submit=false; refusing to submit")
    reviewer = make_reviewer(cfg.reviewer_target, cfg.reviewer_submit_command)
    result = reviewer.submit_package(package_path, prompt_path, dry_run=args.dry_run)
    if not args.dry_run:
        state.update({"status": "external_review_pending", "submitted_package_path": str(package_path)})
        write_state(cfg.state_path, state)
    append_ledger(cfg.ledger_path, ActionRecord("submit-review", "dry-run" if args.dry_run else "ok", result))
    print(result)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ao-commander")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    _project_arg(init)
    init.set_defaults(func=cmd_init)

    doctor_cmd = sub.add_parser("doctor")
    _project_arg(doctor_cmd)
    doctor_cmd.set_defaults(func=cmd_doctor)

    brief = sub.add_parser("brief")
    _project_arg(brief)
    brief.set_defaults(func=cmd_brief)

    heartbeat = sub.add_parser("heartbeat")
    _project_arg(heartbeat)
    heartbeat.add_argument("--dispatch", action="store_true", help="Print visible Terminal dispatch command when safe")
    heartbeat.set_defaults(func=cmd_heartbeat)

    package = sub.add_parser("package-review")
    _project_arg(package)
    package.add_argument("--dry-run", action="store_true")
    package.add_argument("--owner-authorized", action="store_true")
    package.set_defaults(func=cmd_package_review)

    submit = sub.add_parser("submit-review")
    _project_arg(submit)
    submit.add_argument("--package")
    submit.add_argument("--prompt")
    submit.add_argument("--dry-run", action="store_true")
    submit.set_defaults(func=cmd_submit_review)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ConfigError, PackageError, FileNotFoundError, PermissionError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
