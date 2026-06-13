"""launch_ui pure-helper contracts.

WHY: the launch page's only write into operator-owned territory is creating
~/.agent-orchestrator/config.yaml. The contract is AUTO-CREATE ONLY: a textual
merge into an existing config can silently corrupt every project's registration
on the host (duplicate top-level keys, mangled notifier lists), so ANY existing
content must abort to manual paste — fail-closed, never best-effort.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("launch_ui", REPO / "scripts" / "launch_ui.py")
launch_ui = importlib.util.module_from_spec(spec)
sys.modules["launch_ui"] = launch_ui
spec.loader.exec_module(launch_ui)


FRAGMENT = """projects:
  "new-proj":
    projectId: "new-proj"
    path: "/tmp/new-proj"
    repo:
      owner: "me"
      name: "new-repo"
      platform: github
    sessionPrefix: "new-proj"
notifiers:
  orchestrator-poke:
    plugin: orchestrator-poke
    sessionPrefix: "new-proj"
    orchestratorSession: "new-proj-orchestrator"
    activeRoot: "/tmp/new-proj"
    stateWriterCommand: "ao-state-writer"
    projectIds:
      - "new-proj"
notificationRouting:
  urgent:
    - orchestrator-poke
"""

BOOTSTRAP_STDOUT = (
    "Rendered 8 files into /tmp/new-proj:\n  MASTER_PLAN.md\n\n"
    "--- paste this block into ~/.agent-orchestrator/config.yaml "
    "(NOT written for you — the global AO config is operator-owned) ---\n\n"
    + FRAGMENT
    + "--- next: `ao start <project-id>`, then externally bootstrap the first slice. ---\n"
)


def test_extract_fragment_from_bootstrap_stdout():
    frag = launch_ui.extract_fragment(BOOTSTRAP_STDOUT)
    assert frag.startswith("projects:")
    assert 'orchestratorSession: "new-proj-orchestrator"' in frag
    assert "--- next" not in frag


def test_extract_fragment_missing_markers_fails_closed():
    with pytest.raises(launch_ui.StepError):
        launch_ui.extract_fragment("Rendered 8 files. no markers here")


def test_merge_into_absent_host_is_fresh_create():
    new, mode = launch_ui.merge_config("", FRAGMENT, "new-proj")
    assert mode == "fresh-create"
    assert new == FRAGMENT


def test_merge_into_comments_only_host_is_fresh_create():
    new, mode = launch_ui.merge_config("# placeholder file\n\n  # nothing real\n", FRAGMENT, "new-proj")
    assert mode == "fresh-create"
    assert new == FRAGMENT


def test_merge_refuses_any_existing_content():
    # Even content WITHOUT a projects: key — a blind append would duplicate
    # whatever top-level keys the fragment carries (notifiers:, notificationRouting:).
    with pytest.raises(launch_ui.StepError):
        launch_ui.merge_config("defaults:\n  agent: claude-code\n", FRAGMENT, "new-proj")


def test_merge_refuses_existing_multi_project_host():
    existing = 'projects:\n  "old-proj":\n    projectId: "old-proj"\n'
    with pytest.raises(launch_ui.StepError):
        launch_ui.merge_config(existing, FRAGMENT, "new-proj")


def test_master_plan_injection_replaces_both_placeholders():
    scaffold = ("## Product Goal\n\n<describe the product goal>\n\n"
                "## Roadmap / Next Implementation Slice\n\n"
                "- <the first slice the orchestrator is externally bootstrapped to dispatch>\n")
    out = launch_ui.inject_master_plan(scaffold, "做一个甜品店记账 app", "S1: 渲染主账本页骨架")
    assert "做一个甜品店记账 app" in out and "S1: 渲染主账本页骨架" in out
    assert "<describe" not in out and "<the first slice" not in out


def test_master_plan_injection_fails_closed_without_placeholders():
    with pytest.raises(launch_ui.StepError):
        launch_ui.inject_master_plan("# replaced by hand already\n", "g", "s")


# --- adopt-existing mode contracts ---------------------------------------------------
# WHY: adoption writes into a LIVING project. The two safety properties that must never
# regress: (1) copy-if-absent — an operator's existing canonical files (plan/TODO/contract)
# are never overwritten, byte-for-byte; (2) validate_adopt refuses to "adopt" a project
# whose canonical plan/TODO are missing (adoption must never fabricate plan files).

def _form(**over):
    base = dict(project_id="my-proj", repo_owner="me", repo_name="repo",
                agent="claude-code", worker_model="m1", orchestrator_model="m2",
                state_writer_cmd="ao-state-writer", orchestrator_session="")
    base.update(over)
    return base


def test_validate_adopt_requires_existing_plan_and_todo(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "PLAN.md").write_text("# plan", encoding="utf-8")
    # TODO missing -> refuse
    with pytest.raises(launch_ui.StepError):
        launch_ui.validate_adopt(_form(project_dir=str(proj), plan_file="PLAN.md", todo_file="TODO.md"))
    (proj / "TODO.md").write_text("# todo", encoding="utf-8")
    f = launch_ui.validate_adopt(_form(project_dir=str(proj), plan_file="PLAN.md", todo_file="TODO.md"))
    assert f["project_dir"] == str(proj)
    assert f["session_log_file"] == "SESSION_LOG.md"
    assert f["orchestrator_session"] == "my-proj-orchestrator"


def test_validate_adopt_rejects_missing_dir_and_path_separators(tmp_path):
    with pytest.raises(launch_ui.StepError):
        launch_ui.validate_adopt(_form(project_dir=str(tmp_path / "nope")))
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "MASTER_PLAN.md").write_text("x", encoding="utf-8")
    (proj / "TODO.md").write_text("x", encoding="utf-8")
    with pytest.raises(launch_ui.StepError):
        launch_ui.validate_adopt(_form(project_dir=str(proj), plan_file="../MASTER_PLAN.md"))


def test_adopt_copy_into_never_overwrites_and_excludes_sidecar(tmp_path):
    staging = tmp_path / "staging"
    project = tmp_path / "project"
    (staging / ".commander").mkdir(parents=True)
    project.mkdir()
    # staging renders the full artifact set
    for rel, body in [("DIRECT_PROJECT_CONTRACT.toml", "rendered-contract"),
                      ("CUSTOM_PLAN.md", "rendered-plan-template"),
                      ("agent-orchestrator.yaml", "brain"),
                      ("orchestrator-liveness.sh", "#!/bin/bash\n"),
                      (".commander/commander.toml", "cmdr")]:
        (staging / rel).write_text(body, encoding="utf-8")
    # the living project already owns its plan and contract
    (project / "CUSTOM_PLAN.md").write_text("OPERATOR PLAN — DO NOT TOUCH", encoding="utf-8")
    (project / "DIRECT_PROJECT_CONTRACT.toml").write_text("OPERATOR CONTRACT", encoding="utf-8")

    copied, skipped = launch_ui.adopt_copy_into(staging, project)

    assert "CUSTOM_PLAN.md" in skipped and "DIRECT_PROJECT_CONTRACT.toml" in skipped
    assert (project / "CUSTOM_PLAN.md").read_text(encoding="utf-8") == "OPERATOR PLAN — DO NOT TOUCH"
    assert (project / "DIRECT_PROJECT_CONTRACT.toml").read_text(encoding="utf-8") == "OPERATOR CONTRACT"
    assert "agent-orchestrator.yaml" in copied
    # The sidecar script is INFRA, never a project artifact: launchd cannot execute under
    # ~/Documents (TCC, live exit 126 2026-06-12), so _step_sidecar_install owns its home
    # install and the project copy set must exclude it entirely.
    assert "orchestrator-liveness.sh" not in copied
    assert "orchestrator-liveness.sh" not in skipped
    assert not (project / "orchestrator-liveness.sh").exists()
    assert str(Path(".commander") / "commander.toml") in copied


def test_adopt_staging_render_end_to_end(tmp_path):
    """Real bootstrap render into staging with custom canonical names + real copy-in.

    WHY: pins the --target-dir/--active-root decoupling the adopt flow depends on — the
    rendered plist/sidecar/fragment must reference the PROJECT path, never the staging path."""
    project = tmp_path / "living-proj"
    project.mkdir()
    (project / "MY_PLAN.md").write_text("# my real plan", encoding="utf-8")
    (project / "MY_TODO.md").write_text("# my real todo", encoding="utf-8")
    f = launch_ui.validate_adopt(_form(project_dir=str(project),
                                       plan_file="MY_PLAN.md", todo_file="MY_TODO.md"))
    staging = tmp_path / "staging"
    fragment = launch_ui._run_bootstrap(f, str(staging), extra=(
        "--active-root", f["project_dir"],
        "--plan-file", f["plan_file"], "--todo-file", f["todo_file"],
        "--session-log-file", f["session_log_file"]))
    assert f'path: "{project}"' in fragment and str(staging) not in fragment
    plist = (staging / "ao-orchestrator-liveness.my-proj.plist").read_text(encoding="utf-8")
    assert str(project) in plist and str(staging) not in plist
    # The plist must execute the sidecar from ~/.agent-orchestrator — NOT the project
    # (TCC denies launchd execution under ~/Documents) and NOT the staging dir — and its
    # PATH must cover homebrew/npm-global installs (launchd never inherits the shell PATH).
    assert ".agent-orchestrator/my-proj-orchestrator-liveness.sh" in plist
    assert str(project / "orchestrator-liveness.sh") not in plist
    # PATH is composed from where the critical commands actually resolve (plus the
    # npm-global/.local fallbacks) — pin the dynamic rule, not one machine's prefix.
    assert ".npm-global/bin" in plist
    import shutil as _sh
    tmux_hit = _sh.which("tmux")
    if tmux_hit:
        assert str(Path(tmux_hit).parent) in plist

    copied, skipped = launch_ui.adopt_copy_into(staging, project)
    assert "MY_PLAN.md" in skipped and "MY_TODO.md" in skipped
    assert (project / "MY_PLAN.md").read_text(encoding="utf-8") == "# my real plan"
    assert "agent-orchestrator.yaml" in copied


# --- auto-detect + effort contracts ---------------------------------------------------
# WHY: the detect button fills fields the operator will LAUNCH with — a wrong silent pick
# (e.g. grabbing one of several TODO-ish files) would aim the orchestrator at the wrong
# canonical files. Ambiguity must yield an empty field + note, never a guess. The effort
# merge writes into an operator-owned settings.json — it must touch exactly one key and
# refuse files it cannot parse.

def test_detect_canonical_files_exact_and_prefixed():
    plan, todo, slog, notes = launch_ui.detect_canonical_files(
        ["ACME_MASTER_PLAN.md", "ACME_TODO.md",
         "ACME_SESSION_LOG.md", "PLAN.md", "README.md"])
    assert plan == "ACME_MASTER_PLAN.md"
    assert todo == "ACME_TODO.md"
    assert slog == "ACME_SESSION_LOG.md"
    assert notes == []


def test_detect_canonical_files_ambiguity_yields_note_not_guess():
    plan, todo, slog, notes = launch_ui.detect_canonical_files(
        ["MASTER_PLAN.md", "OLD_TODO.md", "NEW_TODO.md"])
    assert plan == "MASTER_PLAN.md"
    assert todo == ""  # two TODO candidates -> never silently choose
    assert any("TODO" in n for n in notes)


def test_slice_candidates_current_section_beats_dated_stale_tail():
    """Reproduces the observed real-ledger shape: the CURRENT phase section sits mid-file
    under an undated 'current'-flavored heading, while dated historical changelog sections
    trail at the bottom with stale next-slice lines. Semantic bucket must beat date/position
    — 'last occurrence wins' was the bug this replaces."""
    text = ("# Ledger\n"
            "## Phase 2 — current status\n"
            "Status: not_started\n"
            "Current next slice: M0 kickoff landing\n"
            "...\n"
            "### 2026-05-05-old-package-round\n"
            "- work record prose\n"
            "- Current next slice now: P7-RP review-package construction\n"
            "### 2026-05-05-even-older\n"
            "- Current next slice now: P7 closure routing\n")
    cands = launch_ui.detect_slice_candidates(text)
    assert cands[0]["text"] == "M0 kickoff landing"
    assert len(cands) == 3  # the stale ones remain visible as clickable候选, just ranked lower
    assert {c["text"] for c in cands[1:]} == {"P7-RP review-package construction",
                                              "P7 closure routing"}


def test_slice_candidates_placeholder_and_done_sink_checkbox_fallback():
    text = ("## current\n- Current next slice: <next>\n"
            "## current work\nCurrent next slice: real thing\n")
    cands = launch_ui.detect_slice_candidates(text)
    assert cands[0]["text"] == "real thing"  # template placeholder sinks
    assert launch_ui.detect_slice_candidates("- [x] done\n- [ ] next thing to do\n")[0]["text"] == "next thing to do"
    assert launch_ui.detect_slice_candidates("no markers at all") == []


def test_suggest_project_id_table():
    assert launch_ui.suggest_project_id("AcmeShop-Mall") == "acmeshop-mall"
    assert launch_ui.suggest_project_id("My_Cool Project!!") == "my-cool-project"
    assert launch_ui.suggest_project_id("123") == ""      # cannot start with a letter -> omit
    assert launch_ui.suggest_project_id("42-shop") == "shop"
    assert launch_ui.suggest_project_id("") == ""


def test_profile_store_roundtrip_and_corrupt_fail_open(tmp_path, monkeypatch):
    monkeypatch.setattr(launch_ui, "PROFILE_STORE", tmp_path / "profiles.json")
    f = dict(project_id="shop", agent="claude-code", worker_model="claude-sonnet-4-6",
             orchestrator_model="claude-fable-5", worker_effort="xhigh", orch_effort="medium",
             plan_file="P.md", todo_file="T.md", seed_poke="yes",
             state_writer_cmd="secret-ish-machinery", write_codex_global="yes")
    launch_ui.save_profile("/abs/proj", dict(f, mode="adopt"))
    store = launch_ui.parse_profiles((tmp_path / "profiles.json").read_text())
    prof = store["/abs/proj"]
    assert prof["worker_effort"] == "xhigh" and prof["mode"] == "adopt"
    # per-run consents and machinery are deliberately NOT replayed
    assert "state_writer_cmd" not in prof and "write_codex_global" not in prof and "write_config" not in prof
    # corrupt store fails open and a save repairs it
    (tmp_path / "profiles.json").write_text("{broken", encoding="utf-8")
    assert launch_ui.parse_profiles("{broken") == {}
    launch_ui.save_profile("/abs/other", dict(f, mode="new"))
    assert "/abs/other" in launch_ui.parse_profiles((tmp_path / "profiles.json").read_text())


def test_parse_github_remote_forms():
    assert launch_ui.parse_github_remote("git@github.com:me/my-repo.git") == ("me", "my-repo")
    assert launch_ui.parse_github_remote("https://github.com/org/repo") == ("org", "repo")
    assert launch_ui.parse_github_remote("https://gitlab.com/org/repo.git") == ("", "")


def test_merge_effort_surgical_and_fail_closed():
    out = launch_ui.merge_effort("", "xhigh")
    import json as _json
    assert _json.loads(out) == {"effortLevel": "xhigh"}
    out = launch_ui.merge_effort('{"model": "opus", "effortLevel": "low"}', "high")
    assert _json.loads(out) == {"model": "opus", "effortLevel": "high"}  # other keys preserved
    with pytest.raises(launch_ui.StepError):
        launch_ui.merge_effort("{not json", "high")
    with pytest.raises(launch_ui.StepError):
        launch_ui.merge_effort("[1,2]", "high")


def test_validate_common_rejects_forged_effort(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "MASTER_PLAN.md").write_text("x", encoding="utf-8")
    (proj / "TODO.md").write_text("x", encoding="utf-8")
    for field in ("worker_effort", "orch_effort", "codex_effort"):
        with pytest.raises(launch_ui.StepError):
            launch_ui.validate_adopt(_form(project_dir=str(proj), plan_file="MASTER_PLAN.md",
                                           todo_file="TODO.md", **{field: "ultra-mega"}))
    # codex vocab includes minimal; claude vocab does not
    launch_ui.validate_adopt(_form(project_dir=str(proj), plan_file="MASTER_PLAN.md",
                                   todo_file="TODO.md", codex_effort="minimal"))
    with pytest.raises(launch_ui.StepError):
        launch_ui.validate_adopt(_form(project_dir=str(proj), plan_file="MASTER_PLAN.md",
                                       todo_file="TODO.md", worker_effort="minimal"))


# --- per-role effort + codex toml contracts -------------------------------------------
# WHY: these write into operator-owned config surfaces. merge_codex_effort touches the
# GLOBAL ~/.codex/config.toml — a wrong edit breaks every codex session on the machine, so
# anything but a clean top-level single-key situation must abort to manual. The settings
# routing (worker -> settings.json, orchestrator -> settings.local.json) is the entire
# per-role mechanism; read_local_effort guards the "orchestrator left empty" semantics.

def test_merge_codex_effort_insert_and_replace():
    out = launch_ui.merge_codex_effort("", "xhigh")
    assert out == 'model_reasoning_effort = "xhigh"\n'
    src = 'model = "gpt-5.4"\nmodel_reasoning_effort = "low"\n[profiles.fast]\nmodel = "o4"\n'
    out = launch_ui.merge_codex_effort(src, "high")
    assert 'model_reasoning_effort = "high"' in out
    assert out.count("model_reasoning_effort") == 1
    assert '[profiles.fast]' in out and 'model = "gpt-5.4"' in out  # rest untouched


def test_merge_codex_effort_fail_closed_on_section_or_dupes():
    with pytest.raises(launch_ui.StepError):
        launch_ui.merge_codex_effort('[profiles.x]\nmodel_reasoning_effort = "low"\n', "high")
    with pytest.raises(launch_ui.StepError):
        launch_ui.merge_codex_effort(
            'model_reasoning_effort = "low"\nmodel_reasoning_effort = "high"\n', "high")


def test_read_local_effort_states():
    assert launch_ui.read_local_effort("") == ("absent", None)
    assert launch_ui.read_local_effort('{"model": "opus"}') == ("none", None)
    assert launch_ui.read_local_effort('{"effortLevel": "high"}') == ("set", "high")
    assert launch_ui.read_local_effort("{broken") == ("unparseable", None)
    assert launch_ui.read_local_effort("[1]") == ("unparseable", None)


def test_step_effort_routes_per_role_and_ignores_gitless_gracefully(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    logs = []
    f = dict(agent="claude-code", worker_effort="xhigh", orch_effort="medium")
    launch_ui._step_effort(f, root, lambda *a: logs.append(a))
    import json as _json
    assert _json.loads((root / ".claude" / "settings.json").read_text())["effortLevel"] == "xhigh"
    assert _json.loads((root / ".claude" / "settings.local.json").read_text())["effortLevel"] == "medium"
    # not a git repo -> the ignore guard WARNs instead of failing the launch
    assert any(a[1] == "WARN" and "git" in a[2] for a in logs)


def test_step_effort_codex_without_consent_never_touches_global(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # safety net: even a bug must not reach the real HOME
    logs = []
    f = dict(agent="codex", codex_effort="xhigh")  # write_codex_global NOT set
    launch_ui._step_effort(f, tmp_path / "proj", lambda *a: logs.append(a))
    assert not (tmp_path / ".codex").exists()
    assert any(a[1] == "SKIP" and "config.toml" in a[2] for a in logs)


# --- local-binary model extraction + select resolution --------------------------------
# WHY: the extraction regex runs over megabytes of compiled string-table junk where model
# ids appear CONCATENATED with neighbors ("gpt-5.5openai"). A boundary bug would surface
# phantom models in the operator's dropdown. And the select+custom resolution feeds the
# model string that ultimately reaches `--model` — direct fields must stay authoritative
# so tests/scripts keep working.

def test_extract_models_from_binary_boundaries():
    blob = (b"\x00junk gpt-5.5openai.gpt-5.5gpt-5.4codex-auto-review\x01"
            b"gpt-5.5\x00gpt-5.3-codex\x00gpt-5.1-codex-max\x00o3\x00o4x\x00"
            b"claude-fable-5.md claude-fable-5\x00claude-haiku-4-5-20251001-v1\x00"
            b"claude-haiku-4-5\x00")
    codex = launch_ui.extract_models_from_binary(blob, "codex")
    assert "gpt-5.5" in codex and "gpt-5.3-codex" in codex and "gpt-5.1-codex-max" in codex
    assert "o3" in codex
    assert "o4" not in codex            # "o4x" is junk, boundary must reject it
    assert "gpt-5.5openai" not in str(codex)
    cl = launch_ui.extract_models_from_binary(blob, "claude-code")
    assert "claude-fable-5" in cl and "claude-haiku-4-5" in cl
    assert all(not m.endswith((".", "-")) for m in cl)
    assert "claude-haiku-4-5-20251001-v1" not in cl  # -v1 junk rejected at that position


def test_model_select_resolution_direct_field_wins(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "MASTER_PLAN.md").write_text("x", encoding="utf-8")
    (proj / "TODO.md").write_text("x", encoding="utf-8")
    base = dict(project_dir=str(proj), plan_file="MASTER_PLAN.md", todo_file="TODO.md")
    # select chosen
    f = launch_ui.validate_adopt(_form(worker_model="", orchestrator_model="",
                                       worker_model_sel="claude-fable-5",
                                       orchestrator_model_sel="claude-opus-4-8", **base))
    assert f["worker_model"] == "claude-fable-5"
    # custom chosen
    f = launch_ui.validate_adopt(_form(worker_model="", orchestrator_model="",
                                       worker_model_sel="__custom__",
                                       worker_model_custom="my-fine-tune-1",
                                       orchestrator_model_sel="claude-fable-5", **base))
    assert f["worker_model"] == "my-fine-tune-1"
    # direct field stays authoritative (tests/scripts path)
    f = launch_ui.validate_adopt(_form(worker_model_sel="claude-fable-5", **base))
    assert f["worker_model"] == "m1"
    # custom selected but left empty -> fail-closed
    with pytest.raises(launch_ui.StepError):
        launch_ui.validate_adopt(_form(worker_model="", worker_model_sel="__custom__",
                                       worker_model_custom="", **base))


def test_slice_candidates_declaration_form_is_strongest():
    """Real ledgers write the current slice DECLARATIVELY: a section heading plus
    'Status: not_started — current next slice (...)' with no marker:value capture. The
    candidate must be the HEADING, and the active status makes it outrank every dated
    changelog value-match — this exact shape was the live mis-ranking the fix addresses."""
    text = ("### M9: The Real Current Section\n"
            "Status: `not_started` — current next slice (first slice of the locked order).\n"
            "...\n"
            "### 2026-06-11-some-changelog-entry\n"
            "current next slice; stale prose fragment restating gates\n"
            "### 2026-05-05-older-entry\n"
            "- Current next slice now: stale value form\n")
    cands = launch_ui.detect_slice_candidates(text)
    assert cands[0]["text"] == "M9: The Real Current Section"
    assert cands[0]["line"] == 2
    # declaration WITHOUT an active status does not enter the top lane
    text2 = ("### Done Section\n"
             "Status: `completed` — was the current next slice once.\n"
             "## current work\nCurrent next slice: real value form\n")
    cands2 = launch_ui.detect_slice_candidates(text2)
    assert cands2[0]["text"] == "real value form"


def test_slice_candidates_canonical_field_beats_everything():
    """A machine-readable head field is canonical truth — no heuristic may outrank it,
    and only the FIRST occurrence counts (later ones can be changelog quotes)."""
    text = ("# Ledger\n"
            "## Current Execution State\n"
            "- current_phase: M0 completed\n"
            "- next_locked_action: dispatch M2 (Widget Registry) — first Phase 2 slice\n"
            "...\n"
            "### M2: Widget Registry\n"
            "Status: `not_started` — current next slice (first slice of locked order).\n"
            "### 2026-06-11-changelog\n"
            "- next_locked_action: stale quoted copy inside a changelog entry\n"
            "- Current next slice now: stale value form\n")
    cands = launch_ui.detect_slice_candidates(text)
    assert cands[0]["text"] == "dispatch M2 (Widget Registry) — first Phase 2 slice"
    assert cands[0]["heading"] == "next_locked_action (canonical)"
    assert cands[0]["line"] == 4
    assert cands[0]["text"] != "stale quoted copy inside a changelog entry"
    # without the canonical field, heuristics still work
    assert launch_ui.detect_slice_candidates(
        "## current\nCurrent next slice: fallback value\n")[0]["text"] == "fallback value"


def test_slice_candidates_canonical_scan_is_scoped_to_head_block():
    """A changelog QUOTE of next_locked_action outside the Current Execution State block
    must NOT enter the canonical lane (review HIGH: a global scan promoted stale quotes
    in ledgers that lack the head block entirely)."""
    text = ("# Ledger\n"
            "## current work\n"
            "Current next slice: live fallback\n"
            "### 2026-06-11-changelog\n"
            "- next_locked_action: stale quoted copy\n")
    cands = launch_ui.detect_slice_candidates(text)
    assert cands[0]["text"] == "live fallback"
    assert all(c["heading"] != "next_locked_action (canonical)" for c in cands)


def test_is_project_registered_predicate():
    """An already-done manual registration must let the launch flow CONTINUE past the
    config step (otherwise launchd/ao-start are unreachable forever on any host with a
    non-empty config) — while prose mentions and commented-out blocks never pass."""
    registered = ('projects:\n  my-proj:\n    projectId: my-proj\n'
                  'notifiers:\n  orchestrator-poke:\n    projectIds:\n      - my-proj\n')
    assert launch_ui.is_project_registered(registered, "my-proj")
    assert launch_ui.is_project_registered(registered.replace(": my-proj", ': "my-proj"'), "my-proj")
    assert not launch_ui.is_project_registered(registered, "other-proj")
    assert not launch_ui.is_project_registered("# projects:\n#  projectId: my-proj\n", "my-proj")
    assert not launch_ui.is_project_registered("prose mentioning projectId: my-proj only\n", "my-proj")
    assert not launch_ui.is_project_registered("", "my-proj")
    # comments-only host still fresh-creates via merge_config (unchanged contract)
    new, mode = launch_ui.merge_config("# nothing\n", 'projects:\n  "x":\n    projectId: "x"\nnotifiers:\n  n: 1\n', "x")
    assert mode == "fresh-create"


# --- machine-seeded bootstrap support (cross-review findings, 2026-06-12) ---

def test_inject_todo_current_state_replaces_stub():
    text = ("# TODO\n\n## Current Execution State\n\n"
            "- current_phase: bootstrap\n"
            "- next_locked_action: define-first-slice\n"
            "- review_gate_state: none\n")
    out = launch_ui.inject_todo_current_state(text, "build the\n  login page")
    # The engine renders dispatch prompts from this field — the stub must be gone and
    # the operator's slice collapsed to one canonical line.
    assert "define-first-slice" not in out
    assert "- next_locked_action: build the login page" in out


def test_inject_todo_current_state_fails_closed_without_stub():
    with pytest.raises(launch_ui.StepError):
        launch_ui.inject_todo_current_state("# TODO\nno stub here\n", "slice")


def test_canonical_next_locked_action_block_scoped():
    text = ("# TODO\n\n## Current Execution State\n\n"
            "- next_locked_action: dispatch M1 (real)\n\n"
            "## Changelog\n\n"
            "- old quote: next_locked_action: dispatch P9 (stale)\n")
    hit = launch_ui.canonical_next_locked_action(text)
    assert hit is not None and hit[0] == "dispatch M1 (real)"
    # No canonical block → None, never a changelog quote.
    assert launch_ui.canonical_next_locked_action("# TODO\n- next_locked_action: x\n") is None
