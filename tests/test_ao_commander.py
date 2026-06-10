"""Faithful-port + brand-neutral + security-hardening tests for the ao_commander package.

Everything is built under tmp_path at runtime. Forbidden brand/secret literals are assembled from
adjacent fragments at runtime so this test source never self-poisons the public safety scan.
"""
from __future__ import annotations

from pathlib import Path
import os
import re
import zipfile

import pytest

from ao_commander.adapters import CommandReviewer, ManualReviewer, make_reviewer
from ao_commander.config import ConfigError, init_project, load_config
from ao_commander.package_review import (
    PackageError,
    create_review_package,
    collect_files,
    secret_scan,
    sha256_file,
    _hard_denied,
)
from ao_commander.protocol import Mode, classify
from ao_commander.state import LockHeld, single_flight, write_state
from ao_commander import cli


def write_project(
    root: Path,
    *,
    worker: str = "worker-001",
    reviewer: str = "review-queue-1",
    package_auth: bool = False,
    phase_auto: bool = False,
    submit_command: str = "",
    include_paths=None,
    exclude=None,
    extra_names=None,
    write_canonical: bool = True,
) -> Path:
    (root / ".commander").mkdir(parents=True, exist_ok=True)
    lines = [
        f'project_name = "{root.name}"',
        'project_root = "."',
        'master_plan_path = "MASTER_PLAN.md"',
        'todo_path = "TODO.md"',
        f'codex_worker_thread_id = "{worker}"',
        f'reviewer_target = "{reviewer}"',
        f'package_preparation_authorized = {"true" if package_auth else "false"}',
        f'phase_close_auto_accept = {"true" if phase_auto else "false"}',
        f'reviewer_submit_command = "{submit_command}"',
        "",
        "[review]",
    ]
    if extra_names is not None:
        lines.append("extra_include_names = [" + ", ".join(f'"{n}"' for n in extra_names) + "]")
    inc = include_paths or []
    lines.append("include_paths = [" + ", ".join(f'"{p}"' for p in inc) + "]")
    exc = exclude if exclude is not None else [".git", ".env", ".omx/state"]
    lines.append("exclude_patterns = [" + ", ".join(f'"{p}"' for p in exc) + "]")
    (root / ".commander" / "commander.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if write_canonical:
        (root / "MASTER_PLAN.md").write_text("# Master Plan\n\nStatus: draft\n", encoding="utf-8")
        (root / "TODO.md").write_text("# TODO\n\n## Locked Sequence\n\n- [ ] first slice\n", encoding="utf-8")
    return root


# --------------------------------------------------------------------------- protocol / Modes


def test_mode_enum_has_all_six_values():
    assert {m.value for m in Mode} == {
        "missing_setup",
        "small_slice",
        "owner_wait",
        "completion_candidate",
        "external_review_wait",
        "blocked",
    }


def test_missing_setup_requires_worker_and_reviewer(tmp_path):
    root = write_project(tmp_path / "p", worker="", reviewer="")
    brief = classify(load_config(root))
    assert brief.mode is Mode.MISSING_SETUP
    # both required targets are named in the missing-setup fact (faithfulness: setup needs both).
    assert "codex_worker_thread_id" in brief.current_fact
    assert "reviewer_target" in brief.current_fact


def test_small_slice_default(tmp_path):
    root = write_project(tmp_path / "p")
    assert classify(load_config(root)).mode is Mode.SMALL_SLICE


def test_owner_wait_on_gate_marker(tmp_path):
    root = write_project(tmp_path / "p")
    (root / "TODO.md").write_text("# TODO\n\nThis is a completion candidate for the phase.\n", encoding="utf-8")
    assert classify(load_config(root)).mode is Mode.OWNER_WAIT


def test_completion_candidate_when_package_authorized(tmp_path):
    root = write_project(tmp_path / "p", package_auth=True)
    assert classify(load_config(root)).mode is Mode.COMPLETION_CANDIDATE


def test_external_review_wait_from_state_and_advisory_boundary(tmp_path):
    root = write_project(tmp_path / "p")
    cfg = load_config(root)
    write_state(cfg.state_path, {"status": "external_review_pending"})
    brief = classify(cfg)
    assert brief.mode is Mode.EXTERNAL_REVIEW_WAIT
    # advisory-only boundary must survive the de-brand: no auto-accept, no auto-close.
    assert "must not be auto-accepted" in brief.risk
    assert "automatically" in brief.forbidden_shortcut


def test_external_review_wait_from_todo_tail(tmp_path):
    root = write_project(tmp_path / "p")
    (root / "TODO.md").write_text("# TODO\n\nStatus: external_review_pending\n", encoding="utf-8")
    assert classify(load_config(root)).mode is Mode.EXTERNAL_REVIEW_WAIT


# --------------------------------------------------------------------------- config gates


def test_phase_close_auto_accept_forbidden(tmp_path):
    root = write_project(tmp_path / "p", phase_auto=True)
    with pytest.raises(ConfigError):
        load_config(root)


def test_missing_required_field_reports_it(tmp_path):
    root = tmp_path / "p"
    (root / ".commander").mkdir(parents=True)
    (root / ".commander" / "commander.toml").write_text(
        'project_name = "p"\nproject_root = "."\nmaster_plan_path = "MASTER_PLAN.md"\n'
        'todo_path = "TODO.md"\ncodex_worker_thread_id = "w"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc:
        load_config(root)
    assert "reviewer_target" in str(exc.value)


def test_init_project_writes_neutral_templates(tmp_path):
    root = tmp_path / "fresh"
    root.mkdir()
    written = init_project(root)
    cfg_text = (root / ".commander" / "commander.toml").read_text(encoding="utf-8")
    assert 'reviewer_target = ""' in cfg_text
    assert "reviewer_submit_command" in cfg_text
    assert (root / "MASTER_PLAN.md").exists() and (root / "TODO.md").exists()
    assert any(p.name == "commander.toml" for p in written)


# --------------------------------------------------------------------------- package builder


def test_package_requires_owner_authorization(tmp_path):
    root = write_project(tmp_path / "p")
    cfg = load_config(root)
    with pytest.raises(PermissionError):
        create_review_package(cfg, dry_run=False, owner_authorized=False)


def test_dry_run_writes_no_artifacts(tmp_path):
    root = write_project(tmp_path / "p")
    cfg = load_config(root)
    result = create_review_package(cfg, dry_run=True, owner_authorized=False)
    assert result.dry_run and result.zip_path is None and result.sha256 is None
    assert not result.package_dir.exists()
    assert not cfg.package_dir.exists()
    assert result.files  # canonical files listed


def test_dry_run_reports_missing_optional(tmp_path):
    root = write_project(tmp_path / "p")  # AGENTS.md/README.md not created
    cfg = load_config(root)
    result = create_review_package(cfg, dry_run=True)
    joined = " ".join(result.missing_optional)
    assert "AGENTS.md" in joined and "README.md" in joined


def test_package_build_integrity_and_advisory_prompt(tmp_path):
    root = write_project(tmp_path / "p")
    cfg = load_config(root)
    result = create_review_package(cfg, dry_run=False, owner_authorized=True)
    assert result.zip_path and result.zip_path.exists()
    # sha256 receipt matches the actual zip bytes.
    assert result.sha256 == sha256_file(result.zip_path)
    assert (result.package_dir / "MANIFEST.md").exists()
    assert (result.package_dir / "SHA256SUMS.txt").exists()
    assert result.prompt_path.name == "EXTERNAL_REVIEW_PROMPT.md"
    prompt_text = result.prompt_path.read_text(encoding="utf-8")
    assert "Provide recommendations only." in prompt_text
    assert "Do not mark the phase closed." in prompt_text
    # zip is internally consistent.
    with zipfile.ZipFile(result.zip_path) as archive:
        assert archive.testzip() is None
        names = archive.namelist()
    assert any(n.endswith("MANIFEST.md") for n in names)


def test_zip_recheck_rejects_denied_member(tmp_path, monkeypatch):
    # codex re-confirm R3 nit: prove the defense-in-depth zip recheck itself fails closed, not just
    # the shared predicate. Force a hard-denied file past collect_files so the recheck is the ONLY
    # thing standing between it and the shipped archive.
    import ao_commander.package_review as pr

    root = write_project(tmp_path / "p")
    denied_dir = root / "sub" / ".git"
    denied_dir.mkdir(parents=True)
    denied_file = denied_dir / "config"
    denied_file.write_text("denied-but-not-secret\n", encoding="utf-8")
    cfg = load_config(root)
    monkeypatch.setattr(pr, "collect_files", lambda c: (denied_file.resolve(),))
    with pytest.raises(PackageError) as exc:
        pr.create_review_package(cfg, dry_run=False, owner_authorized=True)
    assert "Denied path in package" in str(exc.value)


def test_collect_files_empty_fails(tmp_path):
    root = write_project(tmp_path / "p", write_canonical=False, extra_names=[])
    cfg = load_config(root)
    with pytest.raises(PackageError):
        collect_files(cfg)


def test_collect_files_hard_denies_regardless_of_config(tmp_path):
    root = write_project(tmp_path / "p", include_paths=["payload"], exclude=[])
    payload = root / "payload"
    payload.mkdir()
    (payload / "ok.md").write_text("clean\n", encoding="utf-8")
    (payload / ".env").write_text("API_KEY=\"shouldnotship\"\n", encoding="utf-8")
    git_dir = payload / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("secret\n", encoding="utf-8")
    state_dir = root / ".omx" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text("{}\n", encoding="utf-8")
    cfg = load_config(root)
    rels = {p.relative_to(root).as_posix() for p in collect_files(cfg)}
    assert "payload/ok.md" in rels
    assert not any(".env" in r or ".git" in r or ".omx/state" in r for r in rels)


def test_hard_denied_is_case_insensitive_and_nesting_aware():
    # codex confirm V5 blocker regression: case-variant and NESTED runtime-state/VCS paths must be
    # hard-denied regardless of config (case-insensitive parts, anywhere in the path).
    denied = [
        ".git/config",
        "a/b/.git/c",
        ".GIT/config",
        "sub/.Git/x",
        ".env",
        "x/.ENV",
        "x/.env.local",
        "x/.env.PROD",
        "sub/.commander/state.json",
        "deep/a/.commander/locks/x",
        "sub/.omx/state/state.json",
        "a/.OMX/state/x.json",
        "NODE_MODULES/x",
        "node_modules/x",
        "x/target/y",
        "x/__pycache__/y",
    ]
    for rel in denied:
        assert _hard_denied(Path(rel)) is True, rel
    allowed = [
        "MASTER_PLAN.md",
        "src/app.py",
        "docs/readme.md",
        "commander_notes.md",
        "environment.md",
    ]
    for rel in allowed:
        assert _hard_denied(Path(rel)) is False, rel


def test_collect_files_denies_nested_runtime_state(tmp_path):
    root = write_project(tmp_path / "p", include_paths=["payload"], exclude=[])
    payload = root / "payload"
    payload.mkdir()
    (payload / "keep.md").write_text("ok\n", encoding="utf-8")
    for sub, fname in [(".commander", "state.json"), (".omx/state", "state.json"), (".GIT", "config")]:
        d = payload / sub
        d.mkdir(parents=True)
        (d / fname).write_text("x\n", encoding="utf-8")
    cfg = load_config(root)
    rels = {p.relative_to(root).as_posix() for p in collect_files(cfg)}
    assert "payload/keep.md" in rels
    assert all(
        ".commander" not in r.lower() and ".omx" not in r.lower() and ".git" not in r.lower()
        for r in rels
    ), rels


def test_collect_files_rejects_escaping_include_path(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.md").write_text("external\n", encoding="utf-8")
    root = write_project(tmp_path / "p", include_paths=[str(outside.resolve())])
    cfg = load_config(root)
    with pytest.raises(PackageError) as exc:
        collect_files(cfg)
    assert "escape" in str(exc.value)


def test_collect_files_rejects_symlink_escape(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.md"
    secret.write_text("external\n", encoding="utf-8")
    root = write_project(tmp_path / "p", include_paths=["linked"])
    link = root / "linked"
    try:
        os.symlink(secret, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")
    cfg = load_config(root)
    with pytest.raises(PackageError):
        collect_files(cfg)


# --------------------------------------------------------------------------- secret scan


def test_secret_scan_positive_blocks_package(tmp_path):
    root = write_project(tmp_path / "p", include_paths=["leaky.txt"])
    # assembled at runtime so this source never carries a matchable secret literal.
    token = "sk-" + "A" * 32
    (root / "leaky.txt").write_text(f"key = {token}\n", encoding="utf-8")
    cfg = load_config(root)
    assert secret_scan(collect_files(cfg))
    with pytest.raises(PackageError):
        create_review_package(cfg, dry_run=False, owner_authorized=True)


def test_secret_scan_detects_token_variants(tmp_path):
    root = write_project(tmp_path / "p", include_paths=["t.txt"])
    gh = "gh" + "p_" + "B" * 36
    (root / "t.txt").write_text(f"token={gh}\n", encoding="utf-8")
    cfg = load_config(root)
    assert secret_scan(collect_files(cfg))


def test_secret_scan_negative(tmp_path):
    root = write_project(tmp_path / "p")
    cfg = load_config(root)
    assert secret_scan(collect_files(cfg)) == []


def test_secret_scan_fails_on_undecodable(tmp_path):
    root = write_project(tmp_path / "p", include_paths=["data.bin"])
    (root / "data.bin").write_bytes(b"\xff\xfe\x00\x01garbage\x80\x81")
    cfg = load_config(root)
    findings = secret_scan(collect_files(cfg))
    assert any("undecodable" in f or "unreadable" in f for f in findings)


# --------------------------------------------------------------------------- lock / reviewers


def test_single_flight_lock_contention(tmp_path):
    lock_dir = tmp_path / "locks"
    with single_flight(lock_dir):
        with pytest.raises(LockHeld):
            with single_flight(lock_dir):
                pass


def test_make_reviewer_defaults_manual(tmp_path):
    reviewer = make_reviewer("queue-1", "")
    assert isinstance(reviewer, ManualReviewer)
    pkg = tmp_path / "pkg.zip"
    pkg.write_text("x", encoding="utf-8")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("p", encoding="utf-8")
    out = reviewer.submit_package(pkg, prompt, dry_run=False)
    assert out["mode"] == "manual" and "instructions" in out


def test_command_reviewer_runs_configured_command(tmp_path):
    script = tmp_path / "submit.sh"
    script.write_text("#!/bin/sh\necho submitted \"$1\"\n", encoding="utf-8")
    script.chmod(0o755)
    reviewer = make_reviewer("queue-1", str(script))
    assert isinstance(reviewer, CommandReviewer)
    pkg = tmp_path / "pkg.zip"
    pkg.write_text("x", encoding="utf-8")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("p", encoding="utf-8")
    out = reviewer.submit_package(pkg, prompt, dry_run=False)
    assert out["mode"] == "command" and "submitted" in out["stdout"]


def test_submit_review_sets_external_review_pending(tmp_path):
    root = write_project(tmp_path / "p")
    assert cli.main(["package-review", "--project", str(root), "--owner-authorized"]) == 0
    assert cli.main(["submit-review", "--project", str(root)]) == 0
    cfg = load_config(root)
    from ao_commander.state import read_state

    assert read_state(cfg.state_path)["status"] == "external_review_pending"


# --------------------------------------------------------------------------- CLI smoke + brand


def test_cli_prog_name_is_brand_neutral():
    assert cli.build_parser().prog == "ao-commander"


def test_cli_brief_and_doctor_return_int(tmp_path, capsys):
    root = write_project(tmp_path / "p")
    assert cli.main(["brief", "--project", str(root)]) == 0
    rc = cli.main(["doctor", "--project", str(root)])
    assert rc in (0, 1)  # doctor returns 1 if a host tool is missing; both are valid run outcomes


def test_src_tree_has_no_brand_tokens():
    src = Path(__file__).resolve().parents[1] / "src" / "ao_commander"
    py_files = list(src.rglob("*.py"))
    assert py_files
    forbidden = [("chat" + "gpt"), ("claw-" + "commander"), ("gpt" + "_pro"), ("Claw" + "Code")]
    bare_thread = re.compile(r"\bthread[_-]?id\b")
    for py in py_files:
        text = py.read_text(encoding="utf-8")
        low = text.lower()
        for token in forbidden:
            assert token.lower() not in low, f"{py.name} contains forbidden token {token!r}"
        # the config field codex_worker_thread_id is underscore-guarded and allowed; a BARE
        # word-bounded worker-reference token of the gated shape is not.
        assert bare_thread.search(text) is None, f"{py.name} contains a bare worker-reference token"
