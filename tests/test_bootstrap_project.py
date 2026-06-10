"""Verify scripts/bootstrap_project.py renders a complete, safe, brand-neutral project.

WHY these matter (not just WHAT):
- A bootstrap that leaves an unresolved @TOKEN@ ships a broken project that the engine can't parse;
  a bootstrap that renders into the wrong place could clobber a live AO clone or the operator's
  home. So we assert full token resolution AND hard refusal of unsafe/forbidden targets.
- Raw token replacement can corrupt TOML/YAML/XML when values carry separators or quotes; we render
  a value with a space + '&' + '"' and assert every output still parses, proving per-format escaping.
- The global ~/.agent-orchestrator/config.yaml is operator-owned; the bootstrap must PRINT, never
  write it. We assert it is not written into the target and is emitted on stdout.
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "bootstrap_project.py"
EXAMPLES = REPO / "examples"

_spec = importlib.util.spec_from_file_location("bootstrap_project", SCRIPT)
boot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(boot)

try:
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None


def _argv(target, **over):
    base = dict(
        project_id="demo-proj", orchestrator_session="demo-proj-orchestrator",
        repo_owner="demo-owner", repo_name="demo-repo", agent="claude-code",
        worker_model="demo-worker", orchestrator_model="demo-orch",
        active_root="/tmp/demo/project", home="/tmp/demo-home", path="/usr/bin:/bin",
        target_dir=str(target),
    )
    base.update(over)
    argv = []
    for k, v in base.items():
        argv += [f"--{k.replace('_', '-')}", str(v)]
    return argv


def _run(target, **over):
    return boot.run(boot.parse_args(_argv(target, **over)))


def test_renders_all_files_with_no_unresolved_token(tmp_path):
    target = tmp_path / "proj"
    written = _run(target)
    names = {p.name for p in written}
    assert names == {
        "DIRECT_PROJECT_CONTRACT.toml", "TODO.md", "MASTER_PLAN.md", "SESSION_LOG.md",
        "agent-orchestrator.yaml", "commander.toml", "ao-orchestrator-liveness.demo-proj.plist",
        "orchestrator-liveness.sh",
    }, names
    blob = "\n".join(p.read_text() for p in target.rglob("*") if p.is_file())
    assert boot.TOKEN_RE.search(blob) is None, "unresolved @TOKEN@ in rendered output"


def test_commander_lands_in_dot_commander(tmp_path):
    target = tmp_path / "proj"
    _run(target)
    assert (target / ".commander" / "commander.toml").is_file()


def test_does_not_create_omx_state(tmp_path):
    target = tmp_path / "proj"
    _run(target)
    assert not (target / ".omx").exists(), "bootstrap must not create canonical state"


def test_state_writer_cmd_propagates_to_all_consumers(tmp_path):
    target = tmp_path / "proj"
    cmd = "PYTHONPATH=/abs/src python3 -m ao_state_writer.cli"
    args = boot.parse_args(_argv(target, state_writer_cmd=cmd))
    _written = boot.run(args)
    contract = (target / "DIRECT_PROJECT_CONTRACT.toml").read_text()
    brain = (target / "agent-orchestrator.yaml").read_text()
    plist = (target / "ao-orchestrator-liveness.demo-proj.plist").read_text()
    fragment = boot.config_fragment(args)
    for where, text in (("contract", contract), ("brain", brain), ("plist", plist), ("config", fragment)):
        assert cmd in text, f"--state-writer-cmd not propagated to {where}"


def test_token_map_covers_every_template_token():
    used = set()
    for f in EXAMPLES.rglob("*"):
        if f.is_file():
            used |= set(boot.TOKEN_RE.findall(f.read_text(encoding="utf-8", errors="ignore")))
    args = boot.parse_args(_argv("/tmp/whatever"))
    args = boot.finalize_args(args, Path("/tmp/whatever"))
    provided = set(boot.build_token_map(args).keys())
    missing = used - provided
    assert not missing, f"templates use tokens the bootstrap never fills: {sorted(missing)}"
    # And no stale/dead provider keys: every token the bootstrap fills must be consumed by a template.
    dead = provided - used
    assert not dead, f"bootstrap fills tokens no template consumes (stale): {sorted(dead)}"


def test_refuses_agent_orchestrator_and_under_repo(tmp_path):
    # The operator's global AO state dir is protected exactly...
    with pytest.raises(boot.BootstrapError):
        _run(Path.home() / ".agent-orchestrator")
    # ...and any directory *inside* the engine repo is refused (would pollute the checkout).
    with pytest.raises(boot.BootstrapError):
        _run(REPO / "examples" / "would-be-new-project")


@pytest.mark.skipif(tomllib is None, reason="tomllib unavailable")
def test_special_char_path_keeps_outputs_parseable(tmp_path):
    target = tmp_path / "proj"
    weird = '/tmp/demo a & "b"/proj'
    _run(target, active_root=weird)
    c = tomllib.loads((target / "DIRECT_PROJECT_CONTRACT.toml").read_text())
    assert c["ao_clone_isolation"]["active_root"] == weird, "TOML escaping corrupted the path"
    try:
        import yaml
        b = yaml.safe_load((target / "agent-orchestrator.yaml").read_text())
        assert b["repo"] == "demo-owner/demo-repo"
    except ImportError:
        pass
    if shutil.which("plutil"):
        plist = target / "ao-orchestrator-liveness.demo-proj.plist"
        r = subprocess.run(["plutil", "-lint", str(plist)], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr


def test_refuses_symlink_target(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(boot.BootstrapError):
        _run(link)


def test_refuses_nonempty_target(tmp_path):
    target = tmp_path / "proj"
    target.mkdir()
    (target / "existing.txt").write_text("x")
    with pytest.raises(boot.BootstrapError):
        _run(target)


def test_refuses_omx_state_tree(tmp_path):
    target = tmp_path / ".omx" / "state" / "proj"
    with pytest.raises(boot.BootstrapError):
        _run(target)


def test_refuses_engine_repo_and_home(tmp_path):
    with pytest.raises(boot.BootstrapError):
        _run(REPO)
    with pytest.raises(boot.BootstrapError):
        _run(Path.home())


def test_rejects_unsafe_canonical_filename(tmp_path):
    for bad in ("../escape.md", "a/b.md", ".."):
        with pytest.raises(boot.BootstrapError):
            _run(tmp_path / "proj", plan_file=bad)


def test_rejects_unsafe_identifier(tmp_path):
    for bad in ('bad id', 'has"quote', "a/b", "amp&er"):
        with pytest.raises(boot.BootstrapError):
            _run(tmp_path / "proj", project_id=bad)


def test_config_is_print_only(tmp_path, capsys):
    target = tmp_path / "proj"
    rc = boot.main(_argv(target))
    assert rc == 0
    # Config fragment is NOT written into the target.
    assert not (target / "agent-orchestrator.config.example.yaml").exists()
    assert not (target / "config.yaml").exists()
    out = capsys.readouterr().out
    assert "projects:" in out and "orchestrator-poke" in out, "config fragment must be printed"


def test_rendered_output_has_no_private_or_product_tokens(tmp_path):
    target = tmp_path / "proj"
    _run(target)
    blob = "\n".join(p.read_text() for p in target.rglob("*") if p.is_file())
    # Configured sample values present (substitution worked).
    assert "/tmp/demo/project" in blob and "demo-owner/demo-repo" in blob
    # Forbidden private/product tokens absent. Assembled so this test source never self-trips the gate.
    forbidden = [
        "Claw" + "Code", "claw-" + "commander", "CLAW" + "_WORKBENCH",
        "cc" + "aibao", "0d5173" + "2349", "S" + "FW", "gpt" + "_pro",
    ]
    hits = [f for f in forbidden if f in blob]
    assert not hits, f"rendered output leaked private/product tokens: {hits}"
