#!/usr/bin/env python3
"""AO project launch page — local, single-operator, one-click onboarding.

启动页：填好项目信息，一键完成 bootstrap → MASTER_PLAN 注入 → config.yaml 注册 →
sidecar 装载 → `ao start` → （可选）第一切片引导 poke。

Run from the repo checkout:
    python3 scripts/launch_ui.py            # binds 127.0.0.1, prints the URL

Design boundaries (deliberate):
  - localhost only; single operator; per-process CSRF token on the form.
  - every step is fail-closed: the first failure ABORTS the launch and the page shows
    exactly what already ran and the rollback anchor (config backup path, rendered dir).
  - subprocess uses argv arrays only (never shell=True); form fields are charset-validated.
  - config.yaml is operator-owned: AUTO-CREATE ONLY. We only write it after an explicit
    in-form consent checkbox, and only when the file is absent or effectively empty;
    ANY existing content aborts to manual-paste mode (a textual merge could silently
    corrupt every project's registration on the host).
  - MASTER_PLAN.md is the human-gate file: the form is its one-time authoring surface
    (the operator's own words at creation); after launch the page never touches it again.
  - `ao start` is a foreground daemon that never exits on success — it is launched
    DETACHED and judged by the orchestrator session appearing (same contract as the
    liveness sidecar's detached revive).
  - non-goals: no daemon management, no dashboard, no remote bind, no deletions.
"""
from __future__ import annotations

import datetime
import html
import http.server
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BOOTSTRAP = REPO / "scripts" / "bootstrap_project.py"

FRAGMENT_BEGIN = "--- paste this block into ~/.agent-orchestrator/config.yaml"
FRAGMENT_END = "--- next:"

ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
REPO_PART_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
# Models / commands may carry spaces, =, /, : (e.g. "PYTHONPATH=/x python3 -m pkg.cli") but
# never control chars or quotes that could confuse the rendered TOML/YAML downstream.
SAFE_LINE_RE = re.compile(r"^[^\x00-\x1f\"'`\\]{1,300}$")
# Canonical filenames must be bare (no path separators) — they live in the project root.
FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,100}$")

GOAL_PLACEHOLDER = "<describe the product goal>"
SLICE_PLACEHOLDER = "<the first slice the orchestrator is externally bootstrapped to dispatch>"


class StepError(Exception):
    """Fail-closed launch abort: message is shown verbatim on the result page."""


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested in tests/test_launch_ui_merge.py)
# ---------------------------------------------------------------------------

def extract_fragment(bootstrap_stdout: str) -> str:
    """Pull the config.yaml fragment out of bootstrap_project.py's stdout."""
    begin = bootstrap_stdout.find(FRAGMENT_BEGIN)
    end = bootstrap_stdout.find(FRAGMENT_END)
    if begin == -1 or end == -1 or end <= begin:
        raise StepError("bootstrap output did not contain the expected config fragment markers")
    block = bootstrap_stdout[begin:end].split("---", 2)[-1]
    fragment = block.strip("\n")
    if "projects:" not in fragment or "notifiers:" not in fragment:
        raise StepError("extracted config fragment is missing projects:/notifiers: keys")
    return fragment + "\n"


def is_project_registered(existing: str, project_id: str) -> bool:
    """A registration already satisfied by hand counts as done — recognize, never write.

    Requires a real (un-commented) projects: key AND an exact projectId line, so prose
    mentions or commented-out blocks never pass."""
    if not re.search(r"^\s*projects\s*:", existing, re.M):
        return False
    pid = re.escape(project_id)
    return bool(re.search(rf'^\s*projectId\s*:\s*"?{pid}"?\s*$', existing, re.M))


def merge_config(existing: str, fragment: str, project_id: str) -> tuple:
    """AUTO-CREATE ONLY, never auto-modify: the global AO config is operator-owned.

    If the host config is absent or effectively empty (comments/blank lines only), the
    fragment becomes the file ('fresh-create'). ANY existing content — even without a
    projects: key — aborts to manual paste (fail-closed; see docs/MULTI_PROJECT.md for
    registering project N+1 by hand)."""
    meaningful = "\n".join(
        l for l in existing.splitlines() if l.strip() and not l.lstrip().startswith("#"))
    if not meaningful.strip():
        return fragment, "fresh-create"
    raise StepError("host config.yaml 已有内容——启动页绝不自动改既有主机配置")


def inject_master_plan(text: str, goal: str, first_slice: str) -> str:
    """Replace the two scaffold placeholders with the operator's own words."""
    if GOAL_PLACEHOLDER not in text or SLICE_PLACEHOLDER not in text:
        raise StepError("rendered MASTER_PLAN does not contain the expected scaffold placeholders")
    return text.replace(GOAL_PLACEHOLDER, goal.strip()).replace(SLICE_PLACEHOLDER, first_slice.strip())


_TODO_NEXT_STUB = re.compile(r"^(\s*-\s*next_locked_action\s*:\s*)define-first-slice\s*$", re.M)


def inject_todo_current_state(text: str, first_slice: str) -> str:
    """Write the first slice into TODO Current Execution State. The engine renders
    dispatch prompts from canonical TODO next_locked_action — NOT from the Master Plan
    roadmap — so leaving the scaffold stub would dispatch the literal 'define-first-slice'
    (cross-review HIGH)."""
    if not _TODO_NEXT_STUB.search(text):
        raise StepError("rendered TODO does not contain the expected next_locked_action stub")
    one_line = " ".join(first_slice.split())
    return _TODO_NEXT_STUB.sub(lambda m: m.group(1) + one_line, text, count=1)


def canonical_next_locked_action(todo_text: str):
    """(text, line) of next_locked_action inside the head Current Execution State block,
    or None. The field is canonical ONLY inside that block — a global scan would promote
    stale changelog QUOTES of the field (review HIGH)."""
    bm = _CANON_BLOCK.search(todo_text)
    if not bm:
        return None
    cm = _CANON_NEXT.search(bm.group(1))
    if not (cm and cm.group(1).strip()):
        return None
    offset = bm.start(1) + cm.start()
    return cm.group(1).strip(), todo_text[:offset].count("\n") + 1


def _norm_ws(s: str) -> str:
    return " ".join(s.split())


# ---------------------------------------------------------------------------
# Launch steps
# ---------------------------------------------------------------------------

def _run(argv: list, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)


def _validate_common(form: dict) -> dict:
    f = {k: (v or "").strip() for k, v in form.items()}
    if not ID_RE.match(f.get("project_id", "")):
        raise StepError("project id 必须是小写字母开头的 [a-z0-9-]，2-41 字符")
    for key in ("repo_owner", "repo_name"):
        if not REPO_PART_RE.match(f.get(key, "")):
            raise StepError(f"{key} 含非法字符")
    for role in ("worker_model", "orchestrator_model"):
        if not f.get(role):  # resolve from the select+custom pair (direct field wins)
            sel = f.get(role + "_sel", "")
            f[role] = f.get(role + "_custom", "").strip() if sel == "__custom__" else sel
    for key in ("agent", "worker_model", "orchestrator_model", "state_writer_cmd"):
        if not SAFE_LINE_RE.match(f.get(key, "")):
            raise StepError(f"{key} 为空或含非法字符（引号/控制符不允许）")
    f["orchestrator_session"] = f.get("orchestrator_session") or f["project_id"] + "-orchestrator"
    if not ID_RE.match(f["orchestrator_session"]):
        raise StepError("orchestrator session 名含非法字符")
    for key, vocab in (("worker_effort", EFFORT_LEVELS), ("orch_effort", EFFORT_LEVELS),
                       ("codex_effort", CODEX_EFFORT_LEVELS)):
        if f.get(key, "") not in ("",) + vocab:
            raise StepError(f"非法 {key} 值：{f[key]!r}")
    return f


def validate_new(form: dict) -> dict:
    """New-project mode: target must be a fresh dir; goal/first-slice are the human words."""
    f = _validate_common(form)
    target = Path(f.get("target_dir", "")).expanduser()
    if not target.is_absolute():
        raise StepError("target dir 必须是绝对路径")
    if target.exists() and any(target.iterdir()):
        raise StepError(f"target dir 已存在且非空：{target}（从零新建只写全新/空目录；已有项目请用「接管已有项目」模式）")
    if not f.get("product_goal") or not f.get("first_slice"):
        raise StepError("产品目标与第一个切片都必须填写（它们会写进 MASTER_PLAN——人类内容人类写）")
    f["target_dir"] = str(target)
    return f


def validate_adopt(form: dict) -> dict:
    """Adopt mode: the project dir and its canonical plan/TODO must already exist."""
    f = _validate_common(form)
    project = Path(f.get("project_dir", "")).expanduser()
    if not project.is_absolute() or not project.is_dir():
        raise StepError("项目目录必须是已存在目录的绝对路径")
    if project.resolve() == Path.home().resolve():
        raise StepError("项目目录不能是家目录")
    f["plan_file"] = f.get("plan_file") or "MASTER_PLAN.md"
    f["todo_file"] = f.get("todo_file") or "TODO.md"
    f["session_log_file"] = f.get("session_log_file") or "SESSION_LOG.md"
    for key in ("plan_file", "todo_file", "session_log_file"):
        if not FILE_RE.match(f[key]):
            raise StepError(f"{key} 必须是不含路径分隔符的安全文件名：{f[key]!r}")
    for key in ("plan_file", "todo_file"):
        if not (project / f[key]).is_file():
            raise StepError(f"接管要求项目目录里已有 {f[key]}（找不到 {project / f[key]}——接管绝不替你创建计划文件）")
    f["project_dir"] = str(project)
    raw = f.get("first_slice_adopt", "")
    f["first_slice_adopt"] = re.sub(r"[\x00-\x08\x0b-\x1f]", " ", raw).strip()[:500]
    return f


def adopt_copy_into(staging: Path, project: Path) -> tuple:
    """Copy rendered AO artifacts into the project, COPY-IF-ABSENT, never overwrite.

    The operator's existing canonical files (plan/TODO/SESSION_LOG/contract) are skipped by
    construction: they already exist under the same names. Note the engine reads exactly
    <root>/DIRECT_PROJECT_CONTRACT.toml — if the project has its own, it is kept as-is and a
    vocab mismatch will fail closed at runtime preflight; if absent, the rendered AO contract
    is copied in. Returns (copied, skipped) as sorted relative-path strings."""
    copied, skipped = [], []
    for src in sorted(p for p in staging.rglob("*") if p.is_file()):
        rel = src.relative_to(staging)
        if rel.name == "orchestrator-liveness.sh":
            # Infra, not a project artifact: the plist executes it from ~/.agent-orchestrator
            # (macOS TCC denies launchd execution under ~/Documents — live exit 126,
            # 2026-06-12). _step_sidecar_install owns its install; never copy it into the repo.
            continue
        dst = project / rel
        if dst.exists():
            skipped.append(str(rel))
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(str(rel))
    return copied, skipped


EFFORT_LEVELS = ("low", "medium", "high", "xhigh")
CODEX_EFFORT_LEVELS = ("minimal", "low", "medium", "high", "xhigh")

# Built-in fallback lists; the 🔄 button replaces them with a live API fetch when a key is
# available. Free text is always allowed — these only feed the datalists.
CURATED_MODELS = {
    "claude-code": ["claude-fable-5", "claude-opus-4-8", "claude-sonnet-4-6",
                    "claude-haiku-4-5", "fable", "opus", "sonnet", "haiku"],
    "codex": ["gpt-5.5", "gpt-5.4", "gpt-5.3-codex", "gpt-5.2-codex",
              "gpt-5.1-codex-max", "gpt-5"],
}


def merge_codex_effort(toml_text: str, level: str) -> str:
    """Surgically set top-level model_reasoning_effort in ~/.codex/config.toml text.

    Single-line textual edit with fail-closed guards: the key inside a [section]/profile
    or appearing more than once is NOT ours to touch — raise and let the operator do it."""
    lines = toml_text.splitlines(keepends=True)
    top_end = len(lines)
    for i, line in enumerate(lines):
        if re.match(r"\s*\[", line):
            top_end = i
            break
    pat = re.compile(r"^\s*model_reasoning_effort\s*=")
    hits_top = [i for i in range(top_end) if pat.match(lines[i])]
    hits_below = [i for i in range(top_end, len(lines)) if pat.match(lines[i])]
    if hits_below:
        raise StepError("model_reasoning_effort 出现在某个 [section]/profile 里——请手工处理")
    if len(hits_top) > 1:
        raise StepError("顶层有多处 model_reasoning_effort——请手工处理")
    newline = f'model_reasoning_effort = "{level}"\n'
    if hits_top:
        lines[hits_top[0]] = newline
    else:
        lines.insert(0, newline)
    return "".join(lines)


def read_local_effort(settings_local_text: str) -> tuple:
    """Classify an existing settings.local.json: ('absent'|'none'|'set'|'unparseable', level)."""
    if not settings_local_text.strip():
        return "absent", None
    try:
        data = json.loads(settings_local_text)
    except json.JSONDecodeError:
        return "unparseable", None
    if not isinstance(data, dict):
        return "unparseable", None
    level = data.get("effortLevel")
    return ("set", level) if level else ("none", None)


def detect_canonical_files(md_names: list) -> tuple:
    """Pick plan/TODO/SESSION_LOG from the project's root *.md names.

    Exact conventional name wins; else a UNIQUE substring candidate; multiple
    candidates are never silently chosen — the field stays empty and a note
    lists them (operator picks by hand)."""
    notes = []

    def pick(token: str, taken: set) -> str:
        exact = [n for n in md_names if n.upper() == f"{token}.MD" and n not in taken]
        if exact:
            return exact[0]
        cands = sorted(n for n in md_names if token in n.upper() and n not in taken)
        if len(cands) == 1:
            return cands[0]
        if cands:
            notes.append(f"{token} 有多个候选（{', '.join(cands)}），请手选")
        return ""

    plan = pick("MASTER_PLAN", set())
    todo = pick("TODO", {plan})
    slog = pick("SESSION_LOG", {plan, todo})
    return plan, todo, slog, notes


_SLICE_MARK = re.compile(
    r"(?:current next slice(?: now)?|next slice|当前下一切片|下一切片)\s*[:：;；—-]\s*(.+)", re.I)
_HEADING_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_CANON_NEXT = re.compile(r"^\s*[-*]?\s*next_locked_action\s*[:：]\s*(.+)$", re.I | re.M)
_CANON_BLOCK = re.compile(r"^#{1,6}\s*Current Execution State\s*$(.*?)(?=^#|\Z)",
                          re.I | re.M | re.S)
_HIST_CTX = re.compile(r"changelog|history|archive|历史|存档", re.I)
_CUR_CTX = re.compile(r"current|active|in[_ -]?progress|当前|进行中", re.I)
_DONE_CTX = re.compile(r"completed|已完成|closed|done", re.I)


def detect_slice_candidates(todo_text: str) -> list:
    """Ranked next-slice candidates from a TODO ledger.

    Real ledgers are NOT append-ordered: the current phase section can sit mid-file while
    dated historical changelog sections trail at the bottom — so 'last occurrence wins' is
    wrong. Ranking (cross-review decision): semantic buckets first, date only WITHIN a
    bucket — current/active context beats dated changelog history; placeholders and
    completed-context candidates sink; later line wins only inside the same bucket.
    The UI shows the whole top-5 list — the top prefill is convenience, not authority."""
    canon = []
    canon_hit = canonical_next_locked_action(todo_text)
    if canon_hit:
        canon.append({"text": canon_hit[0][:300],
                      "line": canon_hit[1],
                      "heading": "next_locked_action (canonical)"})
    lines = todo_text.splitlines()
    cur_heading, cur_date = "", 0
    ranked = []
    for i, line in enumerate(lines):
        if re.match(r"#{1,6}\s", line):
            cur_heading = line.lstrip("#").strip()
            m = _HEADING_DATE.search(cur_heading)
            cur_date = int("".join(m.groups())) if m else 0
            continue
        m = _SLICE_MARK.search(line)
        decl = re.search(r"current next slice|当前下一切片", line, re.I)
        if m and not m.group(1).strip().startswith("("):
            # value form: "current next slice: <text>"
            text = m.group(1).strip()
            priority = 1 if _CUR_CTX.search(cur_heading) else 2
        elif decl and cur_heading:
            # declaration form: the line asserts the ENCLOSING SECTION is the current
            # next slice ("Status: not_started — current next slice (...)"); the
            # candidate text is the heading itself. An active status on the same line
            # is the strongest currency signal in the whole ledger.
            text = cur_heading
            active = re.search(r"not[_ -]?started|in[_ -]?progress|当前|进行中", line, re.I)
            priority = 0 if active else (1 if _CUR_CTX.search(cur_heading) else 2)
        else:
            continue
        if not text:
            continue
        # Context is judged on the HEADING and the candidate text ONLY. Neighbor-line
        # windows were tried and removed: dense ledger prose almost always contains
        # done/current words somewhere nearby, which inverted the ranking on a real file.
        key = (
            1 if _HIST_CTX.search(cur_heading) else 0,
            1 if re.search(r"<[^>]+>", text) else 0,
            1 if _DONE_CTX.search(cur_heading + " " + text) else 0,
            priority,
            -cur_date,
            -i,
        )
        ranked.append((key, {"text": text[:300], "line": i + 1, "heading": cur_heading[:80]}))
    ranked.sort(key=lambda c: c[0])
    seen, out = {c["text"] for c in canon}, list(canon)
    for _, cand in ranked:
        if cand["text"] in seen:
            continue
        seen.add(cand["text"])
        out.append(cand)
        if len(out) == 5:
            break
    if not out:  # plain checkbox ledgers: first unchecked item as the single candidate
        m = re.search(r"^\s*[-*] \[ \] (.+)$", todo_text, re.M)
        if m:
            out.append({"text": m.group(1).strip()[:300],
                        "line": todo_text[:m.start()].count("\n") + 1, "heading": ""})
    return out


def suggest_project_id(dir_basename: str) -> str:
    """Canonical project-id from a directory name; empty when it cannot be made valid
    (omit rather than truncate-and-guess — cross-review rule)."""
    s = re.sub(r"[^a-z0-9-]+", "-", dir_basename.lower())
    s = re.sub(r"-{2,}", "-", s).strip("-")
    s = re.sub(r"^[^a-z]+", "", s)
    return s if ID_RE.match(s) else ""


PROFILE_STORE = Path.home() / ".agent-orchestrator" / "launch-ui-profiles.json"
# Deliberately excludes write_config (a per-run consent), write_codex_global (a per-run
# consent into a GLOBAL file) and state_writer_cmd (machinery, not a choice to replay).
PROFILE_FIELDS = ("mode", "project_id", "orchestrator_session", "repo_owner", "repo_name",
                  "agent", "worker_model", "orchestrator_model",
                  "worker_effort", "orch_effort", "codex_effort",
                  "plan_file", "todo_file", "session_log_file", "seed_poke")


def parse_profiles(store_text: str) -> dict:
    """Fail-open: a corrupt store never blocks anything — it just stops resuming."""
    try:
        data = json.loads(store_text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save_profile(root: str, f: dict) -> None:
    profiles = parse_profiles(
        PROFILE_STORE.read_text(encoding="utf-8") if PROFILE_STORE.exists() else "")
    profiles[root] = {k: f.get(k, "") for k in PROFILE_FIELDS}
    PROFILE_STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PROFILE_STORE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(profiles, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(PROFILE_STORE)


def parse_github_remote(url: str) -> tuple:
    m = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", url.strip())
    return (m.group(1), m.group(2)) if m else ("", "")


def merge_effort(settings_text: str, level: str) -> str:
    """Surgically set ONLY effortLevel in a .claude/settings.json text.

    Unparseable or non-object input raises StepError — the caller downgrades to a
    WARN with manual instructions; we never overwrite a file we cannot read."""
    if not settings_text.strip():
        data = {}
    else:
        try:
            data = json.loads(settings_text)
        except json.JSONDecodeError as exc:
            raise StepError(f"已有 settings.json 不是合法 JSON（{exc}）")
        if not isinstance(data, dict):
            raise StepError("已有 settings.json 顶层不是对象")
    data["effortLevel"] = level
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _ensure_local_ignored(root: Path, log) -> None:
    """settings.local.json is a LOCAL override — make sure git will not commit it."""
    rel = ".claude/settings.local.json"
    chk = _run(["git", "-C", str(root), "check-ignore", "-q", rel], timeout=15)
    if chk.returncode == 0:
        return
    if chk.returncode == 1:
        gi = root / ".gitignore"
        existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
        if rel not in existing:
            sep = "" if (not existing or existing.endswith("\n")) else "\n"
            gi.write_text(existing + sep + rel + "\n", encoding="utf-8")
            log("effort 设置", "OK", f"已在 {gi} 追加忽略规则 {rel}（本地覆盖文件不应入库）")
    else:
        log("effort 设置", "WARN",
            f"无法确认 {rel} 的 git ignore 状态（项目可能不是 git 仓库）——请自行确保它不被提交")


def _merge_effort_file(path: Path, level: str, role_desc: str, log) -> None:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        new_text = merge_effort(existing, level)
    except StepError as exc:
        log("effort 设置", "WARN", f"{role_desc} 未写入（{exc}）——请手工设置 {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_text, encoding="utf-8")
    log("effort 设置", "OK", f"{role_desc} → {path}")


def _step_effort(f: dict, root: Path, log) -> None:
    agent = f["agent"]
    if agent == "claude-code":
        w, o = f.get("worker_effort", ""), f.get("orch_effort", "")
        if not w and not o:
            log("effort 设置", "SKIP", "未选择 effort")
            return
        sp = root / ".claude" / "settings.json"
        sp_local = root / ".claude" / "settings.local.json"
        if w:
            _merge_effort_file(sp, w,
                f"worker effortLevel={w}（settings.json 需 commit 进仓库才传导到 worktree 里的 worker）", log)
        if o:
            _ensure_local_ignored(root, log)
            _merge_effort_file(sp_local, o,
                f"orchestrator effortLevel={o}（settings.local.json 只在项目根生效=只管 orchestrator）", log)
        elif w:
            state, lv = read_local_effort(
                sp_local.read_text(encoding="utf-8") if sp_local.exists() else "")
            if state == "set":
                log("effort 设置", "OK",
                    f"orchestrator 保持既有本地覆盖 effortLevel={lv}（settings.local.json 优先于 settings.json）")
            elif state == "unparseable":
                log("effort 设置", "WARN",
                    f"{sp_local} 不可解析——orchestrator 实际 effort 不确定，请手工检查")
            else:
                log("effort 设置", "OK",
                    f"orchestrator 未单独设置——worker 的 {w} 同样作用于 orchestrator"
                    f"（要分开就给 orchestrator 也选一档）")
        return
    if agent == "codex":
        level = f.get("codex_effort", "")
        if not level:
            log("effort 设置", "SKIP", "未选择 codex effort")
            return
        manual = f'在 ~/.codex/config.toml 顶层设置 model_reasoning_effort = "{level}"'
        if f.get("write_codex_global") != "yes":
            log("effort 设置", "SKIP",
                f"未勾选写入全局 codex 配置——{manual}。"
                f"（AO 引擎对 codex 没有项目级/角色级 effort 通道，全局 config.toml 是唯一真实位置）")
            return
        cfg = Path.home() / ".codex" / "config.toml"
        existing = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
        try:
            new_text = merge_codex_effort(existing, level)
        except StepError as exc:
            log("effort 设置", "WARN", f"未写入（{exc}）——{manual}")
            return
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(new_text, encoding="utf-8")
        log("effort 设置", "OK",
            f"{cfg} → model_reasoning_effort={level}（全局：影响本机所有 codex 会话；"
            f"引擎无 per-role 通道，无法按 worker/orchestrator 区分）")
        return
    log("effort 设置", "SKIP", f"agent={agent} 无 effort 接线")


# Model-id shapes embedded in the local CLI binaries. The trailing negative lookahead
# rejects concatenated rust/js string-table junk like "gpt-5.5openai" at that position
# (standalone occurrences still match).
MODEL_BIN_PATTERNS = {
    "codex": re.compile(rb"(?:gpt-5(?:\.\d+)?(?:-codex)?(?:-max|-mini|-nano|-pro|-spark)?"
                        rb"|o[34])(?![A-Za-z0-9.-])"),
    "claude-code": re.compile(rb"claude-(?:fable|opus|sonnet|haiku)-\d+(?:[.-]\d+)*"
                              rb"(?![A-Za-z0-9.-])"),
}


def extract_models_from_binary(data: bytes, runtime: str) -> list:
    """Pull the model-id vocabulary out of a CLI binary's bytes (read-only, one-shot —
    chunked scanning would reintroduce seam false-matches at the lookahead boundary)."""
    pat = MODEL_BIN_PATTERNS[runtime]
    return sorted({m.group(0).decode("ascii") for m in pat.finditer(data)}, reverse=True)


def cli_models(runtime: str) -> tuple:
    """Model list from the LOCAL CLI the engine actually launches — fresher than any
    curated list, needs no API key."""
    cmd = "claude" if runtime == "claude-code" else "codex"
    path = shutil.which(cmd)
    if not path:
        raise StepError(f"PATH 上找不到 {cmd}")
    data = Path(path).resolve().read_bytes()
    models = extract_models_from_binary(data, runtime)
    if not models:
        raise StepError(f"{cmd} 二进制中未提取到模型名")
    ver = ""
    proc = _run([cmd, "--version"], timeout=15)
    if proc.returncode == 0 and proc.stdout.strip():
        ver = proc.stdout.strip().splitlines()[0]
    return models, f"本机 {ver or cmd} 内嵌清单"


def fetch_live_models(runtime: str) -> tuple:
    """Operator-triggered live model list. Returns (models, source); raises StepError.

    The ONLY outbound network call this page can make: read-only model listing against the
    vendor API, using a key the operator already exported. No key -> StepError (caller
    falls back to CURATED_MODELS with a note)."""
    import urllib.request
    if runtime == "claude-code":
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise StepError("未设置 ANTHROPIC_API_KEY（导出后重开启动页再点更新）")
        req = urllib.request.Request("https://api.anthropic.com/v1/models?limit=100",
                                     headers={"x-api-key": key,
                                              "anthropic-version": "2023-06-01"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
        ids = sorted((m.get("id", "") for m in data.get("data", []) if m.get("id")), reverse=True)
        if not ids:
            raise StepError("Anthropic API 返回空模型列表")
        return ids, "Anthropic API 实时列表"
    if runtime == "codex":
        key = os.environ.get("OPENAI_API_KEY", "")
        if not key:
            raise StepError("未设置 OPENAI_API_KEY（导出后重开启动页再点更新）")
        req = urllib.request.Request("https://api.openai.com/v1/models",
                                     headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
        ids = sorted((m.get("id", "") for m in data.get("data", [])
                      if re.match(r"^(gpt-|o\d|codex)", m.get("id", ""))), reverse=True)
        if not ids:
            raise StepError("OpenAI API 返回的列表里没有 gpt/o*/codex 系模型")
        return ids, "OpenAI API 实时列表"
    raise StepError(f"未知 runtime：{runtime!r}")


def _run_bootstrap(f: dict, target_dir: str, extra: tuple = ()) -> str:
    argv = [sys.executable, str(BOOTSTRAP),
            "--project-id", f["project_id"],
            "--orchestrator-session", f["orchestrator_session"],
            "--repo-owner", f["repo_owner"], "--repo-name", f["repo_name"],
            "--agent", f["agent"],
            "--worker-model", f["worker_model"],
            "--orchestrator-model", f["orchestrator_model"],
            "--state-writer-cmd", f["state_writer_cmd"],
            "--target-dir", target_dir, *extra]
    proc = _run(argv)
    if proc.returncode != 0:
        raise StepError(f"bootstrap 失败：{proc.stderr.strip() or proc.stdout.strip()}")
    return extract_fragment(proc.stdout)


def _step_config(f: dict, fragment: str, log) -> None:
    config_path = Path.home() / ".agent-orchestrator" / "config.yaml"
    if f.get("write_config") != "yes":
        log("config 注册", "SKIP", "未勾选自动写入——把下面片段手工粘进 ~/.agent-orchestrator/config.yaml 后再 `ao start`：\n" + fragment)
        raise StepError("已按选择停在手工注册步骤（上面渲染产物全部有效）")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    if is_project_registered(existing, f["project_id"]):
        log("config 注册", "OK", "检测到该项目已登记于 config.yaml——跳过写入，继续后续步骤")
        return
    try:
        new_text, mode = merge_config(existing, fragment, f["project_id"])
    except StepError as exc:
        raise StepError(f"config.yaml 自动写入拒绝（{exc}）。请手工粘贴此片段后再 `ao start`：\n{fragment}")
    backup = None
    if existing:  # comments-only file about to be overwritten — keep its bytes
        backup = config_path.with_name(f"config.yaml.bak-{datetime.datetime.now():%Y%m%dT%H%M%S}")
        shutil.copy2(config_path, backup)
    config_path.write_text(new_text, encoding="utf-8")
    readback = config_path.read_text(encoding="utf-8")
    if f'"{f["project_id"]}"' not in readback:
        raise StepError(f"config.yaml 写入读回校验失败（备份在 {backup}）")
    log("config 注册", "OK", f"模式={mode}" + (f"；备份 {backup}" if backup else ""))


def _step_launchd(plist_src: Path, label: str, log) -> None:
    """Load the EXACT expected plist — never glob (an adopted dir may hold unrelated plists)."""
    if not plist_src.is_file():
        raise StepError(f"找不到预期的 plist：{plist_src}")
    plist_dst = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    if plist_dst.exists():
        raise StepError(f"launchd 任务已存在：{plist_dst}（不覆盖既有自动化）")
    probe = _run(["launchctl", "print", f"gui/{os.getuid()}/{label}"])
    if probe.returncode == 0:
        raise StepError(f"launchd 已存在同名服务 {label}（不覆盖既有自动化）")
    shutil.copy2(plist_src, plist_dst)
    proc = _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_dst)])
    if proc.returncode != 0:
        raise StepError(f"launchctl bootstrap 失败：{proc.stderr.strip()}（plist 已就位 {plist_dst}，可手工重试）")
    log("sidecar 装载", "OK", str(plist_dst))


def _step_sidecar_install(script_src: Path, project_id: str, log) -> Path:
    """Install the rendered sidecar script to ~/.agent-orchestrator (copy-if-absent).

    The plist executes it from there: macOS TCC denies launchd execution under
    ~/Documents (live exit 126, 2026-06-12), so the script must live outside the
    project. Must run BEFORE _step_launchd loads the plist."""
    if not script_src.is_file():
        raise StepError(f"找不到渲染出的 sidecar 脚本：{script_src}")
    dst = Path.home() / ".agent-orchestrator" / f"{project_id}-orchestrator-liveness.sh"
    if dst.exists():
        log("sidecar 脚本", "OK", f"已存在，保留不覆盖：{dst}")
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(script_src, dst)
    dst.chmod(0o755)
    log("sidecar 脚本", "OK", f"安装到 {dst}（项目根之外——launchd 无法执行 ~/Documents 下的脚本）")
    return dst


def _step_ao_start(f: dict, log_dir: Path, log) -> str:
    ao = shutil.which("ao")
    if not ao:
        raise StepError("PATH 上找不到 `ao`（AO runtime 未安装？）")
    daemon_log = log_dir / "ao-start.launch-ui.log"
    with open(daemon_log, "ab") as fh:
        daemon = subprocess.Popen([ao, "start", f["project_id"]],
                                  stdin=subprocess.DEVNULL, stdout=fh, stderr=fh,
                                  start_new_session=True)
    appeared = False
    for _ in range(18):  # bounded wait: 18 x 5s = 90s
        if subprocess.run(["tmux", "has-session", "-t", f["orchestrator_session"]],
                          capture_output=True).returncode == 0:
            appeared = True
            break
        if daemon.poll() is not None:
            raise StepError(f"ao start 提前退出（exit {daemon.returncode}），日志：{daemon_log}")
        time.sleep(5)
    if appeared:
        log("ao start", "OK", f"orchestrator 会话 {f['orchestrator_session']} 已出现（daemon detached, pid {daemon.pid}）")
    else:
        log("ao start", "WARN", f"daemon 活着（pid {daemon.pid}）但 90s 内未见 orchestrator 会话——"
                                 f"sidecar 下一拍会接手拉起；日志：{daemon_log}")
    return ao


def _step_poke(f: dict, ao: str, msg: str, log) -> None:
    if f.get("seed_poke") == "yes":
        proc = _run([ao, "send", f["orchestrator_session"], msg], timeout=60)
        status = "OK" if proc.returncode == 0 else "WARN"
        log("引导 poke", status, proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else proc.stderr.strip())
    else:
        log("引导 poke", "SKIP", f"届时手工：ao send {f['orchestrator_session']} \"<引导语>\"")


def launch_new(f: dict, log) -> None:
    """From-zero flow: render into the fresh target, inject the human words, wire, start."""
    target = Path(f["target_dir"])
    label = f"ao-orchestrator-liveness.{f['project_id']}"
    save_profile(str(target), dict(f, mode="new"))

    fragment = _run_bootstrap(f, f["target_dir"])
    log("bootstrap 渲染", "OK", f"{f['target_dir']}")

    plan = target / "MASTER_PLAN.md"
    plan.write_text(inject_master_plan(plan.read_text(encoding="utf-8"),
                                       f["product_goal"], f["first_slice"]), encoding="utf-8")
    log("MASTER_PLAN 注入", "OK", "产品目标 + 第一切片已写入（此文件此后只归人类改）")

    todo = target / "TODO.md"
    todo.write_text(inject_todo_current_state(todo.read_text(encoding="utf-8"),
                                              f["first_slice"]), encoding="utf-8")
    log("TODO 注入", "OK", "next_locked_action=第一切片（引擎从 canonical TODO 渲染派发 prompt，"
                           "不读 Master Plan roadmap）")

    _step_effort(f, target, log)
    _step_config(f, fragment, log)
    _step_sidecar_install(target / "orchestrator-liveness.sh", f["project_id"], log)
    _step_launchd(target / f"{label}.plist", label, log)
    ao = _step_ao_start(f, target, log)
    _step_poke(f, ao, (
        f"FIRST-SLICE BOOTSTRAP poke: 这是项目 {f['project_id']} 的首次外部引导。"
        f"引导走状态机种子流程，不要 ad-hoc spawn：(1) 写种子提案 JSON——requested_state=historical_closed、"
        f"target_kind=small_chapter、target_id=phase-0-bootstrap、base_state_revision=0、"
        f"actor_role=orchestrator、evidence_refs 引用 MASTER_PLAN.md 目标节与 TODO.md Current Execution State——"
        f"用 ao-state-writer apply --root {f['target_dir']} --proposal <该JSON> 提交。"
        f"bootstrap 阶段确已完成，这是历史事实导入；引擎只在机器空闲且目标不存在时接受，带评审字段即拒。"
        f"(2) 然后跑你的标准 reconcile（list-ready → 逐 pid dispatch，用引擎渲染的 prompt spawn worker）。"
        f"canonical TODO 的 next_locked_action 已写入第一切片。"), log)


def launch_adopt(f: dict, log) -> None:
    """Adopt flow: render to a temp staging dir with --active-root pointing at the REAL
    project, then copy-if-absent the AO artifacts in. The operator's plan/TODO are never
    written — the seed poke just tells the orchestrator to reconcile and resume them."""
    project = Path(f["project_dir"])
    label = f"ao-orchestrator-liveness.{f['project_id']}"
    save_profile(str(project), dict(f, mode="adopt"))

    staging = Path(tempfile.mkdtemp(prefix="ao-adopt-staging-"))
    try:
        fragment = _run_bootstrap(f, str(staging), extra=(
            "--active-root", str(project),
            "--plan-file", f["plan_file"],
            "--todo-file", f["todo_file"],
            "--session-log-file", f["session_log_file"]))
        log("bootstrap 渲染（暂存）", "OK", "渲染到临时目录，运行时路径全部指向真实项目目录")
        copied, skipped = adopt_copy_into(staging, project)
        _step_sidecar_install(staging / "orchestrator-liveness.sh", f["project_id"], log)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    log("AO 工件补齐", "OK",
        f"补入：{', '.join(copied) if copied else '（无）'}；保留既有：{', '.join(skipped) if skipped else '（无）'}。"
        f"接管绝不覆盖已有文件。注意：引擎只认 DIRECT_PROJECT_CONTRACT.toml 这个文件名——"
        f"若保留的是你自己的契约且词表与引擎不兼容，运行时 preflight 会 fail-closed 提示。")

    confirmed = f.get("first_slice_adopt", "")
    if confirmed:
        # The engine renders dispatch prompts from canonical TODO next_locked_action; a
        # poke sentence must never override it. Verify agreement or fail closed
        # (cross-review HIGH).
        canon_hit = canonical_next_locked_action(
            (project / f["todo_file"]).read_text(encoding="utf-8"))
        if canon_hit is None:
            raise StepError(
                f"{f['todo_file']} 的 Current Execution State 块里没有 next_locked_action 字段，"
                f"无法核对确认切片——引擎按该字段渲染派发 prompt。请先把下一切片写入该字段再启动。")
        if _norm_ws(canon_hit[0]) != _norm_ws(confirmed):
            raise StepError(
                f"确认的切片与 canonical TODO 不一致（{f['todo_file']}:{canon_hit[1]} "
                f"next_locked_action）。以 TODO 为准改表单，或先改 TODO 再启动。")

    _step_effort(f, project, log)
    _step_config(f, fragment, log)
    _step_launchd(project / f"{label}.plist", label, log)
    ao = _step_ao_start(f, project, log)
    slice_part = (f"下一切片以 canonical TODO 的 next_locked_action 为准（已与表单确认一致）：{confirmed}。"
                  if confirmed else "下一切片以 canonical TODO 的 next_locked_action 为准。")
    _step_poke(f, ao, (
        f"ADOPT-RESUME poke: 项目 {f['project_id']} 已接管到 AO。"
        f"请读取 {f['plan_file']} 与 {f['todo_file']} 做 reconcile。"
        f"若 canonical state 为空（接管首跑属预期），先做状态机种子，不要 ad-hoc spawn："
        f"从 canonical TODO 取最近一个真实已完成的切片，写种子提案 JSON（requested_state=historical_closed、"
        f"target_kind 按切片规格、base_state_revision=0、actor_role=orchestrator、"
        f"evidence_refs 引用 TODO/PR/SESSION_LOG 锚点），"
        f"用 ao-state-writer apply --root {f['project_dir']} --proposal <该JSON> 提交——"
        f"历史事实导入，引擎在机器忙、目标已存在或带评审字段时一律拒绝。"
        f"随后跑你的标准 reconcile（list-ready → 逐 pid dispatch，用引擎渲染的 prompt spawn worker）。"
        + slice_part), log)


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

TOKEN = secrets.token_hex(16)

FORM_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>AO 项目启动页</title><style>
:root{
  --bg:#f4f5fa; --card:#ffffff; --field:#f7f8fc; --text:#191b24; --muted:#697080;
  --line:#e4e6ef; --accent:#5b6cff; --accent2:#8b5cf6; --on-accent:#fff;
  --ok:#15803d; --warn:#b45309; --fail:#b91c1c;
  --ring:rgba(91,108,255,.22); --shadow:0 1px 2px rgba(16,18,35,.05),0 8px 24px rgba(16,18,35,.06);
}
@media(prefers-color-scheme:dark){:root{
  --bg:#0e0f15; --card:#171922; --field:#11131c; --text:#e9ebf5; --muted:#98a0b3;
  --line:#272a38; --ring:rgba(124,138,255,.28); --shadow:0 1px 2px rgba(0,0,0,.4),0 10px 30px rgba(0,0,0,.35);
  --ok:#4ade80; --warn:#fbbf24; --fail:#f87171;
}}
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC",Helvetica,sans-serif;
  background:var(--bg);color:var(--text);max-width:840px;margin:0 auto;
  padding:3rem 1.4rem 5rem;line-height:1.55;-webkit-font-smoothing:antialiased}
h1{font-size:2rem;font-weight:800;letter-spacing:-.02em;margin:0 0 .35rem;
  background:linear-gradient(92deg,var(--accent),var(--accent2));
  -webkit-background-clip:text;background-clip:text;color:transparent}
.sub{color:var(--muted);margin:0 0 2rem;font-size:.93rem;max-width:46rem}
.sub code{font-family:ui-monospace,Menlo,monospace;font-size:.85em;background:var(--field);
  border:1px solid var(--line);border-radius:5px;padding:.05rem .35rem}
fieldset{background:var(--card);border:1px solid var(--line);border-radius:14px;
  padding:1.15rem 1.4rem 1.4rem;margin:0 0 1.15rem;box-shadow:var(--shadow)}
legend{font-weight:700;font-size:.86rem;letter-spacing:.04em;color:var(--accent);
  padding:0 .55rem;text-transform:uppercase}
label{display:block;margin-top:1rem;font-weight:600;font-size:.88rem}
.hint{color:var(--muted);font-size:.76rem;font-weight:400}
input,textarea,select{width:100%;padding:.58rem .75rem;font-size:.95rem;margin-top:.32rem;
  border:1px solid var(--line);border-radius:9px;background:var(--field);color:var(--text);
  transition:border-color .15s,box-shadow .15s;appearance:none;-webkit-appearance:none}
select{background-image:linear-gradient(45deg,transparent 50%,var(--muted) 50%),
  linear-gradient(135deg,var(--muted) 50%,transparent 50%);
  background-position:calc(100% - 1.1rem) 55%,calc(100% - .78rem) 55%;
  background-size:.32rem .32rem;background-repeat:no-repeat;padding-right:2rem}
input:focus,textarea:focus,select:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--ring)}
input[type=checkbox],input[type=radio]{appearance:auto;-webkit-appearance:auto;width:1.05rem;height:1.05rem;
  margin:0 .5rem 0 0;accent-color:var(--accent);vertical-align:-2px}
.modes input{display:none}
textarea{height:5.2rem;resize:vertical;font-family:inherit}
.row{display:flex;gap:1rem}.row>div{flex:1}
button{margin-top:1.5rem;padding:.85rem 2.6rem;font-size:1.02rem;font-weight:700;cursor:pointer;
  border:none;border-radius:11px;color:var(--on-accent);
  background:linear-gradient(92deg,var(--accent),var(--accent2));
  box-shadow:0 4px 14px var(--ring);transition:transform .12s,box-shadow .12s;width:100%}
button:hover{transform:translateY(-1px);box-shadow:0 6px 20px var(--ring)}
button:active{transform:translateY(0)}
button.ghost{width:100%;margin-top:.32rem;padding:.58rem .9rem;font-size:.88rem;font-weight:600;
  color:var(--accent);background:var(--field);border:1px solid var(--line);box-shadow:none}
button.ghost:hover{border-color:var(--accent);transform:none;box-shadow:0 0 0 3px var(--ring)}
fieldset.modes{padding-bottom:1.15rem}
.modes label{display:inline-flex;align-items:center;margin:.35rem .6rem 0 0;padding:.6rem 1.1rem;
  font-weight:600;font-size:.92rem;border:1.5px solid var(--line);border-radius:999px;
  cursor:pointer;transition:all .15s;background:var(--field)}
.modes label:has(input:checked){border-color:var(--accent);color:var(--accent);
  background:var(--ring);box-shadow:0 0 0 3px var(--ring)}
.modes input{accent-color:var(--accent)}
.modes .hint{margin-left:.3rem}
.cand{cursor:pointer;padding:.55rem .75rem;margin:.4rem 0;border:1px solid var(--line);
  border-radius:9px;font-size:.84rem;background:var(--field);transition:all .12s}
.cand:hover{border-color:var(--accent);box-shadow:0 0 0 3px var(--ring)}
.cand .loc{color:var(--muted);margin-right:.45rem;font-family:ui-monospace,Menlo,monospace;font-size:.76rem}
#detect-status,#models-status{margin-top:.4rem;font-size:.8rem;min-height:1.1rem}
::placeholder{color:var(--muted);opacity:.65}
</style></head><body>
<h1>AO 项目启动页</h1>
<p class="sub">一键完成 bootstrap 渲染 → config 注册 → sidecar 装载 → <code>ao start</code> → 引导 poke。
任何一步失败都会立刻停下，并显示回滚锚。</p>
<form method="post" action="/launch">
<input type="hidden" name="token" value="__TOKEN__">
<fieldset class="modes"><legend>模式</legend>
<label><input type="radio" name="mode" value="new" checked onchange="setMode()"> 从零新建项目</label>
<label><input type="radio" name="mode" value="adopt" onchange="setMode()"> 接管已有项目<span class="hint">（目录里已有自己的 MASTER_PLAN/TODO）</span></label>
</fieldset>
<fieldset><legend>项目</legend>
<div class="row"><div>
<label>项目 ID <span class="hint">小写字母/数字/连字符</span></label>
<input name="project_id" placeholder="my-proj" pattern="[a-z][a-z0-9-]+" required>
</div><div>
<label>orchestrator 会话名 <span class="hint">默认 &lt;项目ID&gt;-orchestrator</span></label>
<input name="orchestrator_session" placeholder="（默认）">
</div></div>
<div id="new-fields">
<label>目标目录 <span class="hint">绝对路径，必须是新目录</span></label>
<input name="target_dir" placeholder="/abs/path/projects/my-proj" oninput="deriveId(this.value)" required>
<label>产品目标 <span class="hint">写进 MASTER_PLAN「Product Goal」——这是唯一人类门文件，启动页只在创建时写这一次，此后只归人类改</span></label>
<textarea name="product_goal" required></textarea>
<label>第一个切片 <span class="hint">写进 Roadmap，orchestrator 引导后会先派它</span></label>
<textarea name="first_slice" required></textarea>
</div>
<div id="adopt-fields" style="display:none">
<label>项目目录 <span class="hint">绝对路径，必须已存在；你的 MASTER_PLAN/TODO/契约绝不会被改动</span></label>
<div class="row"><div style="flex:3">
<input name="project_dir" placeholder="/abs/path/projects/existing-proj" oninput="deriveId(this.value)">
</div><div style="flex:1">
<button type="button" class="ghost" onclick="detectProject()">🔍 自动识别</button>
</div></div>
<div id="detect-status" class="hint"></div>
<div class="row"><div>
<label>计划文件名 <span class="hint">必须已存在于项目目录</span></label>
<input name="plan_file" value="MASTER_PLAN.md" data-default="MASTER_PLAN.md">
</div><div>
<label>TODO 文件名 <span class="hint">必须已存在于项目目录</span></label>
<input name="todo_file" value="TODO.md" data-default="TODO.md">
</div></div>
<label>SESSION_LOG 文件名 <span class="hint">没有则补一份空模板</span></label>
<input name="session_log_file" value="SESSION_LOG.md" data-default="SESSION_LOG.md">
<label>下一个切片 <span class="hint">自动识别自 TODO（下方候选可点选），请确认或修改；会写进引导 poke，留空则让 orchestrator 自行 reconcile</span></label>
<textarea name="first_slice_adopt"></textarea>
<div id="slice-candidates"></div>
</div>
<div class="row"><div>
<label>仓库 owner</label><input name="repo_owner" placeholder="my-org" required>
</div><div>
<label>仓库 name</label><input name="repo_name" placeholder="my-repo" required>
</div></div>
</fieldset>
<fieldset><legend>模型与 effort</legend>
<div class="row"><div>
<label>agent runtime <span class="hint">先选这里——模型/effort 选项随之联动</span></label>
<select name="agent" onchange="suggestModels()"><option>claude-code</option><option>codex</option></select>
</div><div>
<label>&nbsp;</label>
<button type="button" class="ghost" onclick="refreshModels()">🔄 更新模型列表</button>
<div id="models-status" class="hint"></div>
</div></div>
<div class="row"><div>
<label>worker 模型 <span class="hint">全量下拉；选「自定义…」可手填</span></label>
<select name="worker_model_sel" id="worker_model_sel" onchange="modelSelChanged('worker_model')"></select>
<input name="worker_model_custom" id="worker_model_custom" placeholder="自定义模型 ID" style="display:none;margin-top:.3rem">
</div><div>
<label>orchestrator 模型 <span class="hint">全量下拉；选「自定义…」可手填</span></label>
<select name="orchestrator_model_sel" id="orchestrator_model_sel" onchange="modelSelChanged('orchestrator_model')"></select>
<input name="orchestrator_model_custom" id="orchestrator_model_custom" placeholder="自定义模型 ID" style="display:none;margin-top:.3rem">
</div></div>
<div id="claude-effort">
<div class="row"><div>
<label>worker effort <span class="hint">写进项目 .claude/settings.json（需 commit 才传导到 worktree 里的 worker）</span></label>
<select name="worker_effort">
<option value="">（不设置）</option>
<option value="low">low</option><option value="medium">medium</option>
<option value="high">high</option><option value="xhigh">xhigh</option>
</select>
</div><div>
<label>orchestrator effort <span class="hint">写进 .claude/settings.local.json（只在项目根生效=只管 orchestrator）</span></label>
<select name="orch_effort">
<option value="">（不设置）</option>
<option value="low">low</option><option value="medium">medium</option>
<option value="high">high</option><option value="xhigh">xhigh</option>
</select>
</div></div>
</div>
<div id="codex-effort" style="display:none">
<label>codex effort <span class="hint">codex 词表 model_reasoning_effort；引擎无 per-role 通道，唯一真实位置是全局 ~/.codex/config.toml</span></label>
<select name="codex_effort">
<option value="">（不设置）</option>
<option value="minimal">minimal</option><option value="low">low</option>
<option value="medium">medium</option><option value="high">high</option>
<option value="xhigh">xhigh</option>
</select>
<label><input type="checkbox" name="write_codex_global" value="yes">
写入全局 ~/.codex/config.toml（影响本机所有 codex 会话；不勾则结果页给出手工指引）</label>
</div>
</fieldset>
<fieldset><legend>高级（有默认值）</legend>
<label>state-writer 命令 <span class="hint">默认已安装的 console script；源码跑法填 PYTHONPATH=… python3 -m ao_state_writer.cli</span></label>
<input name="state_writer_cmd" value="ao-state-writer">
<label><input type="checkbox" name="write_config" value="yes" checked>
自动创建 ~/.agent-orchestrator/config.yaml（仅当文件不存在或为空；已有任何内容一律拒绝并转手工粘贴）</label>
<label><input type="checkbox" name="seed_poke" value="yes" checked>
启动后自动发引导 poke</label>
</fieldset>
<button type="submit">一键启动 🚀</button>
<script>
var MODEL_SUGGEST={
 'claude-code':['claude-fable-5','claude-opus-4-8','claude-sonnet-4-6','claude-haiku-4-5'],
 'codex':['gpt-5.5','gpt-5.4','gpt-5.3-codex','gpt-5.2-codex','gpt-5.1-codex-max','gpt-5']};
var DEFAULT_PICK={
 'claude-code':{worker_model:'claude-sonnet-4-6',orchestrator_model:'claude-fable-5'},
 'codex':{worker_model:'gpt-5.3-codex',orchestrator_model:'gpt-5.5'}};
function populateModelSelects(){
  var a=document.getElementsByName('agent')[0].value;
  var list=MODEL_SUGGEST[a]||[];
  ['worker_model','orchestrator_model'].forEach(function(role){
    var sel=document.getElementById(role+'_sel');
    var prev=sel.value;
    sel.innerHTML='';
    var items=list.slice();
    // never silently drop the current selection when the list refreshes
    if(prev && prev!=='__custom__' && items.indexOf(prev)<0){items.unshift(prev);}
    items.forEach(function(m){var o=document.createElement('option');o.value=m;o.textContent=m;sel.appendChild(o);});
    var c=document.createElement('option');c.value='__custom__';c.textContent='自定义…';sel.appendChild(c);
    var want=prev||((DEFAULT_PICK[a]||{})[role]);
    if(want && items.indexOf(want)>=0){sel.value=want;}
    else if(prev==='__custom__'){sel.value='__custom__';}
    else{sel.value=items[0]||'__custom__';}
    modelSelChanged(role);
  });
}
function modelSelChanged(role){
  var sel=document.getElementById(role+'_sel');
  document.getElementById(role+'_custom').style.display=(sel.value==='__custom__')?'':'none';
}
function suggestModels(){
  var a=document.getElementsByName('agent')[0].value;
  // runtime switch: reset to the runtime's defaults (the lists are disjoint vocabularies)
  ['worker_model','orchestrator_model'].forEach(function(role){
    var sel=document.getElementById(role+'_sel');
    if(sel.value && (MODEL_SUGGEST[a]||[]).indexOf(sel.value)<0 && sel.value!=='__custom__'){sel.value='';}
  });
  populateModelSelects();
  document.getElementById('claude-effort').style.display=(a==='claude-code')?'':'none';
  document.getElementById('codex-effort').style.display=(a==='codex')?'':'none';
}
function refreshModels(){
  var st=document.getElementById('models-status');
  var a=document.getElementsByName('agent')[0].value;
  st.textContent='拉取中…';
  var body='token='+encodeURIComponent(document.getElementsByName('token')[0].value)
          +'&runtime='+encodeURIComponent(a);
  fetch('/models',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:body})
    .then(function(r){return r.json();})
    .then(function(d){
      if(d.error){st.textContent='⛔ '+d.error;return;}
      MODEL_SUGGEST[a]=d.models;  // cache survives runtime switch round-trips
      populateModelSelects();
      st.textContent='✅ '+d.models.length+' 个模型（'+d.source+'）';
    })
    .catch(function(e){st.textContent='⛔ '+e;});
}
function fillIfEmpty(name,v){
  var el=document.getElementsByName(name)[0];
  if(v && el && !el.value){el.value=v;}
}
function fillDetected(name,profileV,detectedV){
  // detection may correct an UNEDITED scaffold default; operator-typed values still win.
  // A profile value equal to the default is itself an unedited default — detection wins.
  var el=document.getElementsByName(name)[0];
  if(!el){return;}
  var def=el.dataset.default;
  var v=(profileV && profileV!==def)?profileV:(detectedV||profileV);
  if(v && (!el.value || el.value===def)){el.value=v;}
}
function deriveId(path){
  var idEl=document.getElementsByName('project_id')[0];
  if(idEl.value){return;}
  var b=(path||'').replace(/\/+$/,'').split('/').pop()||'';
  var s=b.toLowerCase().replace(/[^a-z0-9-]+/g,'-').replace(/-+/g,'-')
        .replace(/^-+|-+$/g,'').replace(/^[^a-z]+/,'');
  if(/^[a-z][a-z0-9-]{1,40}$/.test(s)){idEl.value=s;}
}
function setModelSel(role,v){
  if(!v){return;}
  var sel=document.getElementById(role+'_sel');
  var has=false;
  for(var i=0;i<sel.options.length;i++){if(sel.options[i].value===v){has=true;break;}}
  if(!has){var o=document.createElement('option');o.value=v;o.textContent=v;
           sel.insertBefore(o,sel.firstChild);}
  sel.value=v; modelSelChanged(role);
}
function renderSliceCandidates(cands){
  var box=document.getElementById('slice-candidates'); box.innerHTML='';
  (cands||[]).forEach(function(c){
    var d=document.createElement('div'); d.className='cand';
    var loc=document.createElement('span'); loc.className='loc';
    loc.textContent='L'+c.line+(c.heading?(' · '+c.heading):'');
    d.appendChild(loc); d.appendChild(document.createTextNode(c.text));
    d.onclick=function(){document.getElementsByName('first_slice_adopt')[0].value=c.text;};
    box.appendChild(d);});
}
function detectProject(){
  var st=document.getElementById('detect-status');
  var dir=document.getElementsByName('project_dir')[0].value;
  if(!dir){st.textContent='先填项目目录';return;}
  st.textContent='识别中…';
  var body='token='+encodeURIComponent(document.getElementsByName('token')[0].value)
          +'&project_dir='+encodeURIComponent(dir);
  fetch('/detect',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:body})
    .then(function(r){return r.json();})
    .then(function(d){
      if(d.error){st.textContent='⛔ '+d.error;return;}
      var p=d.profile||{};
      if(p.agent){var ag=document.getElementsByName('agent')[0];
        if(ag.value!==p.agent){ag.value=p.agent;} suggestModels();}
      // profile first (resume), detection fills the remaining gaps — EMPTY fields only
      fillIfEmpty('project_id', p.project_id||d.suggested_project_id);
      fillIfEmpty('orchestrator_session', p.orchestrator_session);
      fillIfEmpty('repo_owner', p.repo_owner||d.repo_owner);
      fillIfEmpty('repo_name', p.repo_name||d.repo_name);
      fillDetected('plan_file', p.plan_file, d.plan_file);
      fillDetected('todo_file', p.todo_file, d.todo_file);
      fillDetected('session_log_file', p.session_log_file, d.session_log_file);
      ['worker_effort','orch_effort','codex_effort'].forEach(function(n){
        var el=document.getElementsByName(n)[0];
        if(p[n] && el && !el.value){el.value=p[n];}});
      if(p.worker_model){setModelSel('worker_model',p.worker_model);}
      if(p.orchestrator_model){setModelSel('orchestrator_model',p.orchestrator_model);}
      fillIfEmpty('first_slice_adopt', d.first_slice);
      renderSliceCandidates(d.slice_candidates);
      st.textContent='✅ 识别完成。'
                     +((d.slice_candidates||[]).length?'切片候选见下方列表（点选替换）。':'')
                     +(d.notes.length?(' '+d.notes.join('；')):'');
    })
    .catch(function(e){st.textContent='⛔ '+e;});
}
function setMode(){
  var adopt=document.querySelector('input[name=mode][value=adopt]').checked;
  document.getElementById('new-fields').style.display=adopt?'none':'';
  document.getElementById('adopt-fields').style.display=adopt?'':'none';
  ['target_dir','product_goal','first_slice'].forEach(function(n){document.getElementsByName(n)[0].required=!adopt;});
  ['project_dir','plan_file','todo_file'].forEach(function(n){document.getElementsByName(n)[0].required=adopt;});
}
setMode();
suggestModels();
</script>
</form></body></html>"""

RESULT_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>启动结果</title><style>
:root{--bg:#f4f5fa;--card:#fff;--text:#191b24;--muted:#697080;--line:#e4e6ef;
  --accent:#5b6cff;--accent2:#8b5cf6;--ok:#15803d;--warn:#b45309;--fail:#b91c1c;
  --shadow:0 1px 2px rgba(16,18,35,.05),0 8px 24px rgba(16,18,35,.06)}
@media(prefers-color-scheme:dark){:root{--bg:#0e0f15;--card:#171922;--text:#e9ebf5;
  --muted:#98a0b3;--line:#272a38;--ok:#4ade80;--warn:#fbbf24;--fail:#f87171;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 10px 30px rgba(0,0,0,.35)}}
body{font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC",Helvetica,sans-serif;
  background:var(--bg);color:var(--text);max-width:840px;margin:0 auto;padding:3rem 1.4rem 4rem;line-height:1.6}
h1{font-size:1.6rem;font-weight:800;letter-spacing:-.02em;margin:0 0 1.2rem;
  background:linear-gradient(92deg,var(--accent),var(--accent2));
  -webkit-background-clip:text;background-clip:text;color:transparent}
pre{background:var(--card);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);
  padding:1.3rem 1.5rem;white-space:pre-wrap;word-break:break-all;
  font-family:ui-monospace,Menlo,monospace;font-size:.84rem;line-height:1.9}
.ok{color:var(--ok);font-weight:700}.warn{color:var(--warn);font-weight:700}.fail{color:var(--fail);font-weight:700}
a{color:var(--accent);text-decoration:none;font-weight:600}a:hover{text-decoration:underline}
</style></head><body><h1>__TITLE__</h1><pre>__BODY__</pre><p><a href="/">← 返回启动页</a></p></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code: int, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _local_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            return False
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
            return False
        return True

    def _send_json(self, code: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _detect(self, form: dict) -> dict:
        """Read-only project scan for the adopt-mode auto-fill button."""
        project = Path((form.get("project_dir") or "").strip()).expanduser()
        if not project.is_absolute() or not project.is_dir():
            return {"error": "项目目录必须是已存在目录的绝对路径"}
        mds = sorted(q.name for q in project.iterdir() if q.is_file() and q.suffix.lower() == ".md")
        plan, todo, slog, notes = detect_canonical_files(mds)
        candidates = []
        if todo:
            try:
                candidates = detect_slice_candidates(
                    (project / todo).read_text(encoding="utf-8", errors="replace"))
            except OSError as exc:
                notes.append(f"TODO 读取失败：{exc}")
        first_slice = candidates[0]["text"] if candidates else ""
        profile = parse_profiles(
            PROFILE_STORE.read_text(encoding="utf-8") if PROFILE_STORE.exists() else ""
        ).get(str(project.resolve()), {})
        if profile:
            notes.insert(0, "已加载该项目上次的启动配置（只填空字段）")
        owner = name = ""
        proc = _run(["git", "-C", str(project), "remote", "get-url", "origin"], timeout=15)
        if proc.returncode == 0:
            owner, name = parse_github_remote(proc.stdout)
            if not owner:
                notes.append("origin remote 不是 GitHub 形态，仓库 owner/name 请手填")
        else:
            notes.append("未找到 git origin remote，仓库 owner/name 请手填")
        if not first_slice:
            notes.append("未识别出下一个切片，请手填或留空")
        return {"plan_file": plan, "todo_file": todo, "session_log_file": slog,
                "repo_owner": owner, "repo_name": name,
                "first_slice": first_slice, "slice_candidates": candidates,
                "suggested_project_id": suggest_project_id(project.resolve().name),
                "profile": profile, "notes": notes}

    def do_GET(self):  # noqa: N802
        if not self._local_ok():
            self._send(403, "local only")
            return
        if self.path != "/":
            self._send(404, "not found")
            return
        self._send(200, FORM_HTML.replace("__TOKEN__", TOKEN))

    def do_POST(self):  # noqa: N802
        if not self._local_ok():
            self._send(403, "local only")
            return
        if self.path not in ("/launch", "/detect", "/models"):
            self._send(404, "not found")
            return
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if ctype != "application/x-www-form-urlencoded":
            self._send(415, "unsupported content type")
            return
        length = int(self.headers.get("Content-Length", "0"))
        form = {k: v[0] for k, v in urllib.parse.parse_qs(
            self.rfile.read(length).decode("utf-8")).items()}
        if form.get("token") != TOKEN:
            if self.path in ("/detect", "/models"):
                self._send_json(403, {"error": "bad token — 刷新页面后重试"})
            else:
                self._send(403, "bad token — reload the form page")
            return
        if self.path == "/detect":
            try:
                self._send_json(200, self._detect(form))
            except Exception as exc:  # read-only scan: fail loud, never a silent 500
                self._send_json(200, {"error": f"识别失败：{exc!r}"})
            return
        if self.path == "/models":
            runtime = (form.get("runtime") or "").strip()
            if runtime not in CURATED_MODELS:
                self._send_json(200, {"error": f"未知 runtime：{runtime}"})
                return
            try:
                models, source = fetch_live_models(runtime)
            except Exception as api_exc:
                try:
                    models, source = cli_models(runtime)
                    source += f"（API 未用：{api_exc}）"
                except Exception as cli_exc:
                    models = CURATED_MODELS[runtime]
                    source = f"内置列表（API：{api_exc}；本机 CLI：{cli_exc}）"
            self._send_json(200, {"models": models, "source": source})
            return
        lines: list = []

        def log(step: str, status: str, detail: str) -> None:
            cls = {"OK": "ok", "WARN": "warn"}.get(status, "fail" if status == "FAIL" else "")
            lines.append(f'<span class="{cls}">[{status}]</span> {html.escape(step)} — {html.escape(detail)}')

        try:
            if form.get("mode") == "adopt":
                launch_adopt(validate_adopt(form), log)
            else:
                launch_new(validate_new(form), log)
            title = "✅ 启动完成"
            lines.append("")
            lines.append(html.escape(
                f"接下来：orchestrator 会按 MASTER_PLAN/TODO 自走；日常操作见 docs/USAGE.zh-CN.md。"))
        except StepError as exc:
            title = "⛔ 已按 fail-closed 停止"
            lines.append(f'<span class="fail">[FAIL]</span> {html.escape(str(exc))}')
        except Exception as exc:  # fail loudly, never a silent 500
            title = "⛔ 意外错误（fail-loud）"
            lines.append(f'<span class="fail">[FAIL]</span> {html.escape(repr(exc))}')
        self._send(200, RESULT_HTML.replace("__TITLE__", title).replace("__BODY__", "\n".join(lines)))

    def log_message(self, fmt, *args):  # quiet the default request spam
        pass


def main() -> int:
    port_env = os.environ.get("AO_LAUNCH_UI_PORT", "0")
    server = http.server.HTTPServer(("127.0.0.1", int(port_env)), Handler)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    print(f"AO 启动页: {url}  (Ctrl-C 退出)")
    print("已自动打开浏览器；若没有，手动访问上面地址。")
    opener = threading.Timer(0.5, lambda: webbrowser.open(url))
    opener.daemon = True
    opener.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
