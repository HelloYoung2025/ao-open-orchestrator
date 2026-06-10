"""state.json SHADOW-REPLAY EQUIVALENCE GATE — the BEHAVIORAL half of S-RECON parity.

WHY THIS EXISTS (the spec, not just a check)
--------------------------------------------
The committed AST symbol-set parity gate (``test_ast_symbol_parity.py``) proves the
def/class *names* of the PUBLIC ``ao_state_writer`` engine line up with the private LIVE
engine modulo an allowlisted divergence taxonomy. It deliberately does NOT prove that the
same-named code *behaves* the same. THIS gate closes that gap: it replays the CLI
subcommands on an IDENTICAL seed through BOTH engines (as separate subprocesses, each with
exactly one ``src`` on ``PYTHONPATH``) and asserts the decisions + resulting ``state.json``
are identical EXCEPT at individually-justified, per-test allowlisted divergence points.

The two engines share a byte-identical entrypoint: ``python -m ao_state_writer.cli``, prog
``ao-state-writer``, a required ``--root``, and the state layout
``<root>/.omx/state/ao-state-writer/{state.json,state-transitions.jsonl}``. The ONLY axis of
difference is which ``src`` directory is on ``PYTHONPATH``. That is what makes a same-seed
cross-engine replay clean.

NEVER-TOUCH-LIVE (load-bearing safety invariant)
------------------------------------------------
The running AO must never be commanded. This harness only ever operates on COPIES under
pytest ``tmp_path``: it copies FROM the live ``state.json`` read-only, regenerates each
copy's contract so ``active_root`` points at the copy (the contract pin then fail-closes any
guarded write whose ``--root`` is not the copy), refuses to run an engine against any root
under the LIVE tree, replaces the real ``ao`` binary with a shim, and — via a module-scoped
tripwire — re-asserts the live ``state.json``/ledger sha is unchanged even if a test errors.

SLICING
-------
This file is built incrementally; each slice is codex-cross-validated and committed
separately (see AO_PARITY_GAP_LEDGER.md §11.24):
  * 14b-0 (this commit): the harness machinery + PUBLIC-vs-PUBLIC self-tests proving the
    invariants (import-origin asserted, per-root tmp copy, per-engine contract regen, fake
    ``ao``, NORMALIZE idempotent + brand-collapsing, read-only sha-invariance, negative
    live-root guard) over one read-only case and one synthetic mutating true-parity case.
  * 14b-1..14b-5 (later): SEED-A live-copy read-only smoke parity; apply/continue/
    record-final-convergence/reconcile behavioural parity vs the real LIVE engine, gated on
    ``AO_PARITY_LIVE_ROOT`` (the private tree is never committed), with the convergence and
    dispatch_kind divergences checked as EXACT per-case deltas (never a broad strip).

The LIVE-comparison tests below SKIP loudly unless ``AO_PARITY_LIVE_ROOT`` points at a LIVE
``ao_state_writer`` package dir; 14b-0 needs no LIVE tree.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# Reuse the AST gate's rename primitives as the SINGLE SOURCE OF TRUTH for the private LIVE
# vocabulary so the two parity gates cannot drift, and so this PUBLIC source never contains
# the forbidden literal tokens (the leak gate forbids them). ``_RENAMED_FROM`` is the LIVE
# root token; ``_live`` expands '@' -> that token and '#' -> the dropped transport token.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ast_symbol_parity import _RENAMED_FROM, _live  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
PUBLIC_SRC = REPO_ROOT / "src"
STATE_RELPATH = Path(".omx") / "state" / "ao-state-writer" / "state.json"
LEDGER_RELPATH = Path(".omx") / "state" / "ao-state-writer" / "state-transitions.jsonl"

_LIVE_ROOT_ENV = "AO_PARITY_LIVE_ROOT"
FAKE_SPAWN_SESSION_ID = "fake-sess-001"

# AO identity vars stripped from every engine subprocess env so each replay controls its own
# caller identity (mirrors tests/acceptance/conftest.py).
_AO_IDENTITY_VARS = ("AO_CALLER_TYPE", "AO_SESSION_ID", "AO_SESSION", "AO_PROJECT_ID")


# ============================== engine source resolution ==========================================


def _live_pkg_root() -> Path | None:
    raw = os.environ.get(_LIVE_ROOT_ENV)
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def _live_src() -> Path | None:
    """The src dir to put on PYTHONPATH for the LIVE engine = the package dir's parent."""
    pkg = _live_pkg_root()
    return pkg.parent if pkg is not None else None


def _live_tree() -> Path | None:
    """The LIVE project tree root (the never-touch-live boundary), derived from the env var.

    AO_PARITY_LIVE_ROOT -> <live_tree>/<commander>/src/ao_state_writer, so the tree is
    parents[2] of the package dir. Derived at runtime: this source hardcodes no private path.
    """
    pkg = _live_pkg_root()
    if pkg is None:
        return None
    try:
        return pkg.parents[2]
    except IndexError:  # pragma: no cover - malformed env
        return None


def _engine_src(engine: str) -> Path:
    if engine == "public":
        return PUBLIC_SRC
    if engine == "live":
        src = _live_src()
        if src is None:
            pytest.skip(f"{_LIVE_ROOT_ENV} not set — LIVE engine unavailable")
        return src
    raise ValueError(f"unknown engine {engine!r}")


# ============================== never-touch-live guards ============================================


def _is_within(path: Path, ancestor: Path) -> bool:
    path = Path(path).resolve()
    ancestor = Path(ancestor).resolve()
    return path == ancestor or ancestor in path.parents


def _assert_safe_copy_root(root: Path) -> None:
    """Refuse to run any engine against a root at or under the LIVE tree (defense in depth)."""
    live_tree = _live_tree()
    if live_tree is not None and _is_within(root, live_tree):
        raise RuntimeError(
            f"refusing to run an engine against a root under the LIVE tree: {root} (live tree {live_tree})"
        )


def _sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module", autouse=True)
def _live_state_tripwire():
    """Assert the real live state.json + ledger sha are unchanged across this module.

    Runs even if a test errors mid-way (post-yield). A no-op when AO_PARITY_LIVE_ROOT is unset
    (14b-0 never touches the live tree). This is the last line of defense behind copy-only
    inputs, per-copy contract regen, and the _assert_safe_copy_root guard.
    """
    live_tree = _live_tree()
    if live_tree is None:
        yield
        return
    state = live_tree / STATE_RELPATH
    ledger = live_tree / LEDGER_RELPATH
    before = (_sha256(state), _sha256(ledger))
    try:
        yield
    finally:
        after = (_sha256(state), _sha256(ledger))
        assert after == before, (
            "LIVE state mutated during shadow-replay tests (never-touch-live violated): "
            f"state.json {before[0]}->{after[0]} ledger {before[1]}->{after[1]}"
        )


# ============================== the engine subprocess driver ======================================

_IMPORT_ORIGIN_OK: dict[str, bool] = {}


def _engine_env(engine: str, fake_ao_bin: Path, overrides: dict[str, str] | None) -> dict[str, str]:
    src = _engine_src(engine)
    # Start from the inherited env MINUS AO identity and MINUS any inherited PYTHONPATH, then set
    # PYTHONPATH to EXACTLY the chosen src (codex: never append inherited — same package name on a
    # second src would shadow the engine under test).
    env = {k: v for k, v in os.environ.items() if k not in _AO_IDENTITY_VARS and k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(src)
    env["PATH"] = os.pathsep.join([str(fake_ao_bin), env.get("PATH", "")])
    if overrides:
        env.update(overrides)
    return env


def _assert_import_origin(engine: str, root_copy: Path, env: dict[str, str]) -> None:
    """Prove the subprocess imports ao_state_writer from the EXPECTED src, not a shadow."""
    if _IMPORT_ORIGIN_OK.get(engine):
        return
    probe = subprocess.run(
        [sys.executable, "-c", "import ao_state_writer.cli as m, sys; sys.stdout.write(m.__file__)"],
        cwd=str(root_copy),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert probe.returncode == 0, f"import-origin probe failed for {engine}: {probe.stderr}"
    origin = Path(probe.stdout.strip()).resolve()
    expected = _engine_src(engine)
    assert _is_within(origin, expected), (
        f"engine {engine!r} imported ao_state_writer from {origin}, expected under {expected}"
    )
    _IMPORT_ORIGIN_OK[engine] = True


def run_engine(
    engine: str,
    root_copy: Path,
    *args: str,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one engine as a subprocess against its OWN copy. Never touches the live tree."""
    _assert_safe_copy_root(root_copy)
    fake_ao_bin = root_copy / "bin"
    env = _engine_env(engine, fake_ao_bin, env_overrides)
    _assert_import_origin(engine, root_copy, env)
    return subprocess.run(
        [sys.executable, "-m", "ao_state_writer.cli", *args],
        cwd=str(root_copy),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


# ============================== per-copy project scaffold =========================================


def _engine_action_tuples(engine: str, root_copy: Path) -> dict[str, list[str]]:
    """Extract the engine's OWN action vocabulary (so the regenerated contract matches it).

    compat.validate_contract_compat exact-compares every configured array, so the contract must
    be derived from the loaded engine, not hardcoded. Extracted via subprocess to stay engine-
    agnostic (the engine under test is whichever src is on PYTHONPATH).
    """
    code = (
        "import json; from ao_state_writer import continuation as c; "
        "print(json.dumps({"
        "'auto_spawn': list(c.AUTO_SPAWN_ACTIONS),"
        "'gated': list(c.GATED_ACTIONS),"
        "'non_executable': list(c.NON_EXECUTABLE_ACTIONS)}))"
    )
    fake_ao_bin = root_copy / "bin"
    env = _engine_env(engine, fake_ao_bin, None)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(root_copy),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, f"action-tuple extraction failed for {engine}: {proc.stderr}"
    return json.loads(proc.stdout)


def _action_array(values: list[str]) -> str:
    items = ",\n".join(f'  "{value}"' for value in values)
    return f"[\n{items}\n]"


def write_contract(root: Path, action_tuples: dict[str, list[str]]) -> Path:
    """Regenerate DIRECT_PROJECT_CONTRACT.toml for THIS copy: active_root=copy, arrays from engine."""
    contract = root / "DIRECT_PROJECT_CONTRACT.toml"
    contract.write_text(
        "version = 1\n"
        "\n"
        "[ao_clone_isolation]\n"
        f'active_root = "{root.resolve().as_posix()}"\n'
        "\n"
        "[owner_proxy]\n"
        'project_id = "example-project"\n'
        "\n"
        "[continuation_policy]\n"
        'orchestrator_session = "example-orchestrator"\n'
        f"auto_spawn_actions = {_action_array(action_tuples['auto_spawn'])}\n"
        f"gated_actions = {_action_array(action_tuples['gated'])}\n"
        f"non_executable_actions = {_action_array(action_tuples['non_executable'])}\n"
        "\n"
        "[dispatch_templates.next_step_plan_mode]\n"
        'template = """\n'
        "Use canonical next locked action. Do not modify MASTER_PLAN.md.\n"
        "No external submission, merge, release, or production side effect without exact authorization.\n"
        '"""\n',
        encoding="utf-8",
    )
    return contract


def write_todo(root: Path) -> Path:
    todo = root / "TODO.md"
    todo.write_text(
        "# Example Project TODO\n"
        "\n"
        "## Current Execution State\n"
        "\n"
        "- current_phase: example-phase\n"
        "- next_locked_action: implement-example-slice\n"
        "- review_gate_state: none\n"
        "- latest_session_log_anchor: example-session-log-anchor\n",
        encoding="utf-8",
    )
    return todo


def write_fake_ao(root: Path) -> Path:
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "ao"
    shim.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "if len(sys.argv) > 1 and sys.argv[1] == 'spawn':\n"
        "    print('View: https://example.invalid/session')\n"
        f"    print('SESSION={FAKE_SPAWN_SESSION_ID}')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return bin_dir


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        GIT_AUTHOR_NAME="harness",
        GIT_AUTHOR_EMAIL="harness@example.invalid",
        GIT_COMMITTER_NAME="harness",
        GIT_COMMITTER_EMAIL="harness@example.invalid",
    )
    return env


def _git_add_commit(root: Path, message: str) -> None:
    """Stage everything and commit; tolerate an empty (nothing-changed) commit."""
    env = _git_env()
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "--allow-empty", "-m", message],
        check=True,
        env=env,
    )


def git_commit_all(root: Path) -> None:
    env = _git_env()
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True, env=env)
    _git_add_commit(root, "seed")


def make_seed_root(base: Path, name: str, engine: str) -> Path:
    """Build a fresh, git-clean project root for one engine and return it (no state yet)."""
    root = (base / name).resolve()
    root.mkdir(parents=True, exist_ok=True)
    write_fake_ao(root)  # before action-tuple extraction so bin/ exists; needs no contract
    tuples = _engine_action_tuples(engine, root)
    write_contract(root, tuples)
    write_todo(root)
    git_commit_all(root)
    return root


def reseal_contract(root: Path, engine: str) -> None:
    """Regenerate the contract for THIS root (active_root=root, arrays from `engine`) and re-commit.

    Mandatory after copytree: a copied contract still pins the SOURCE's active_root, so every
    guarded write would fail-close non_canonical_root (and continuation spawn cwd/prompt would point
    back at the source). Re-committing keeps the governance tree clean for read-only preflights.
    """
    tuples = _engine_action_tuples(engine, root)
    write_contract(root, tuples)
    _git_add_commit(root, "reseal-contract")


def copy_and_reseal(seed: Path, dest: Path, engine: str) -> Path:
    """Copy a seed root to its own copy and reseal the contract to that copy's active_root."""
    dest = dest.resolve()
    shutil.copytree(seed, dest)
    reseal_contract(dest, engine)
    return dest


def state_path(root: Path) -> Path:
    return root / STATE_RELPATH


def read_state(root: Path) -> dict | None:
    sp = state_path(root)
    if not sp.exists():
        return None
    return json.loads(sp.read_text(encoding="utf-8"))


def write_proposal(root: Path, proposal: dict, name: str = "proposal.json") -> Path:
    """Write a proposal file UNDER the copy root (codex Q1: keep non-root path args in the copy)."""
    p = root / name
    p.write_text(json.dumps(proposal), encoding="utf-8")
    return p


SEED_PROPOSAL = {
    "proposal_id": "p-seed-1",
    "target_kind": "small_chapter",
    "target_id": "chapter-1",
    "base_state_revision": 0,
    "requested_state": "evidence_pending",
    "actor_role": "implementer",
    "evidence_refs": ["evidence#1"],
}


# ============================== NORMALIZE (brand-collapse + volatile scrub) ========================

# Canonicalize the LIVE external-review vocabulary to the PUBLIC spelling so a pure brand rename
# does not read as a behavioural divergence. LIVE tokens are built from the AST gate's placeholder
# primitives (no forbidden literal in this source); PUBLIC tokens are the brand-neutral spelling.
# Order is MOST-SPECIFIC FIRST (longest match wins) so substrings don't get mangled.
_PUB_ROOT = "escalated_review"
_LIVE_TO_PUBLIC: list[tuple[str, str]] = [
    (_RENAMED_FROM.replace("_", "-") + "-actuate", _PUB_ROOT.replace("_", "-") + "-actuate"),
    (_live("@_#_review"), _PUB_ROOT),               # LIVE GATED action token -> escalated_review
    (_live("@_#_uncertain"), _PUB_ROOT + "_uncertain"),
    (_live("@_#_bridge"), _PUB_ROOT + "_actuator"),  # actuator module stem
    (_live("@_review"), _PUB_ROOT),                  # LIVE <root>_review_* -> escalated_review_*
    (_RENAMED_FROM, _PUB_ROOT),                       # LIVE root token -> escalated_review
]

# Volatile / engine-text fields whose values legitimately differ run-to-run or engine-to-engine.
# Dropped from BOTH sides before structural compare (codex Q6). Exact key names + suffix patterns.
_VOLATILE_EXACT = frozenset(
    {
        "time",
        "attested_time",
        "elapsed_seconds",
        "prompt_preview",
        "spawn_cwd",
        "would_run",
        "package_path",
        "prompt_path",
        "canonical_active_root",
        "review_transport",
        _live("#_transport"),  # LIVE-only transport field (built from placeholder; literal is forbidden)
        "state_writer_cmd",
        "required_source_commit",
        "required_source_ref",
    }
)
_VOLATILE_SUFFIXES = ("_time", "_at", "_nonce", "_sha256", "_commit", "_ref", "_cmd")

# Placeholder a volatile key collapses to in the TOMBSTONE variant (codex 14b-3 Q1): instead of
# dropping a volatile key, keep the (brand-renamed) key with this sentinel value. Two states that are
# plain-normalize-equal but carry DIFFERENT volatile-key SHAPES (one engine emits an extra timestamp
# the other doesn't) then become tombstone-UNequal — so plain-normalize equality can never be
# manufactured by an asymmetric volatile drop.
_VOLATILE_TOMBSTONE = "<VOLATILE>"


def _rename_brand(text: str) -> str:
    for live_token, pub_token in _LIVE_TO_PUBLIC:
        if live_token in text:
            text = text.replace(live_token, pub_token)
    return text


def _is_volatile_key(key: str) -> bool:
    return key in _VOLATILE_EXACT or key.endswith(_VOLATILE_SUFFIXES)


def _canon_root_strs(roots) -> list[str]:
    # Longest-first so a nested root is replaced before its parent.
    return sorted({Path(r).resolve().as_posix() for r in roots}, key=len, reverse=True)


def _canon_paths(text: str, root_strs: list[str]) -> str:
    for root in root_strs:
        if root in text:
            text = text.replace(root, "<ROOT>")
    return text


def normalize(obj, roots=(), tombstone_volatile: bool = False):
    """Recursively brand-collapse (LIVE->PUBLIC) keys+string values, drop volatile keys, and
    canonicalize each copy's own root path to <ROOT> (the only inherently copy-specific prefix).

    This does NOT strip the convergence-spawn / dispatch_kind divergences — those are checked as
    EXACT per-case deltas in the behavioural slices (codex Q5: a broad strip would mask real bugs).

    With ``tombstone_volatile=True`` a volatile key is KEPT (brand-renamed) with a ``<VOLATILE>``
    sentinel value instead of being dropped, so asymmetric volatile-key shapes cannot hide.
    """
    return _normalize(obj, _canon_root_strs(roots), tombstone_volatile)


def normalize_with_volatile_tombstones(obj, roots=()):
    """``normalize`` but volatile keys survive as ``<VOLATILE>`` tombstones (codex 14b-3 Q1).

    Used to prove a plain-normalize equality is not an artifact of one engine carrying an extra
    volatile key the other lacks: under tombstones such an asymmetry surfaces as inequality."""
    return normalize(obj, roots, tombstone_volatile=True)


def _normalize(obj, root_strs: list[str], tombstone_volatile: bool = False):
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if isinstance(key, str) and _is_volatile_key(key):
                if tombstone_volatile:
                    out[_rename_brand(key)] = _VOLATILE_TOMBSTONE
                continue
            new_key = _rename_brand(key) if isinstance(key, str) else key
            out[new_key] = _normalize(value, root_strs, tombstone_volatile)
        return out
    if isinstance(obj, list):
        return [_normalize(item, root_strs, tombstone_volatile) for item in obj]
    if isinstance(obj, str):
        return _canon_paths(_rename_brand(obj), root_strs)
    return obj


# ============================== ALWAYS-ON SELF-TESTS (no LIVE tree needed) =========================


def test_normalize_is_idempotent():
    sample = {
        "result": _PUB_ROOT,
        "time": 123,
        "nested": [{"requested_state": _PUB_ROOT + "_pending", "created_at": 9}],
        "package_sha256": "deadbeef",
    }
    once = normalize(sample)
    assert normalize(once) == once
    # volatile keys are gone; brand value preserved (already PUBLIC spelling).
    assert "time" not in once and "created_at" not in once["nested"][0]
    assert "package_sha256" not in once
    assert once["result"] == _PUB_ROOT


def test_normalize_collapses_live_brand_to_public():
    """A LIVE-spelled envelope must normalize EQUAL to its PUBLIC-spelled twin (pure rename)."""
    live_side = {
        _live("@_review_gates"): {"g1": {"required_caller_type": _live("@_review_actuator")}},
        "requested_state": _live("@_review_pending"),
        "next_required_action": _live("@_#_review"),
        "blocker_code": _live("@_actuator_failure"),
        "subcommand": _RENAMED_FROM.replace("_", "-") + "-actuate",
    }
    public_side = {
        _PUB_ROOT + "_gates": {"g1": {"required_caller_type": _PUB_ROOT + "_actuator"}},
        "requested_state": _PUB_ROOT + "_pending",
        "next_required_action": _PUB_ROOT,
        "blocker_code": _PUB_ROOT + "_actuator_failure",
        "subcommand": _PUB_ROOT.replace("_", "-") + "-actuate",
    }
    assert normalize(live_side) == normalize(public_side)
    # And the LIVE root token must not survive normalization anywhere.
    assert _RENAMED_FROM not in json.dumps(normalize(live_side))


def test_run_engine_refuses_root_under_live_tree(monkeypatch, tmp_path):
    """The negative guard must raise for any root at/under the LIVE tree, independent of a real
    live checkout — synthesize a live tree via the env var and a child root under it."""
    fake_live_tree = tmp_path / "fake-clone" / "commander" / "src" / "ao_state_writer"
    fake_live_tree.mkdir(parents=True)
    monkeypatch.setenv(_LIVE_ROOT_ENV, str(fake_live_tree))
    # _live_tree() == parents[2] == tmp_path/'fake-clone'
    derived = _live_tree()
    assert derived == (tmp_path / "fake-clone").resolve()
    child = derived / ".omx" / "state"
    child.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="under the LIVE tree"):
        run_engine("public", child, "list-ready", "--root", str(child))
    # A sibling tmp root is fine (no raise from the guard itself).
    safe = tmp_path / "safe-root"
    safe.mkdir()
    assert not _is_within(safe, derived)


def test_public_engine_import_origin_is_public(tmp_path):
    """Proves the import-origin assertion + driver wiring: the public engine resolves under src."""
    root = make_seed_root(tmp_path, "origin", "public")
    proc = run_engine("public", root, "list-ready", "--root", str(root))
    assert proc.returncode in (0, 3), proc.stderr  # ready/projection (0) or fail-closed preflight (3)
    assert _IMPORT_ORIGIN_OK.get("public") is True


def test_public_vs_public_readonly_parity_and_sha_invariance(tmp_path):
    """One read-only case: list-ready PUBLIC-vs-PUBLIC on an identical seeded state must yield equal
    normalized envelopes AND leave both copies' state.json byte-unchanged (projection wrote nothing)."""
    seed = make_seed_root(tmp_path, "ro-seed", "public")
    # Give the seed one applied proposal so list-ready has something to project, then commit clean.
    prop = write_proposal(seed, SEED_PROPOSAL)
    applied = run_engine("public", seed, "apply", "--root", str(seed), "--proposal", str(prop))
    assert applied.returncode == 0, applied.stderr
    _git_add_commit(seed, "seed-state")

    pub = copy_and_reseal(seed, tmp_path / "ro-pub", "public")
    live = copy_and_reseal(seed, tmp_path / "ro-live", "public")  # PUBLIC engine on both in 14b-0
    pre = (_sha256(state_path(pub)), _sha256(state_path(live)))

    rp = run_engine("public", pub, "list-ready", "--root", str(pub))
    rl = run_engine("public", live, "list-ready", "--root", str(live))
    assert rp.returncode == rl.returncode, (rp.stderr, rl.stderr)
    assert normalize(json.loads(rp.stdout), roots=(pub, live)) == normalize(
        json.loads(rl.stdout), roots=(pub, live)
    )

    post = (_sha256(state_path(pub)), _sha256(state_path(live)))
    assert pre == post, "read-only list-ready must not mutate state.json"


def test_public_vs_public_apply_true_parity(tmp_path):
    """One mutating true-parity case: apply the SAME proposal to two copies via the PUBLIC engine;
    decision envelopes AND post-state must be normalized-equal (the cleanest zero-divergence proof)."""
    seed = make_seed_root(tmp_path, "apply-seed", "public")
    pub = copy_and_reseal(seed, tmp_path / "apply-pub", "public")
    live = copy_and_reseal(seed, tmp_path / "apply-live", "public")

    pp = write_proposal(pub, SEED_PROPOSAL)
    lp = write_proposal(live, SEED_PROPOSAL)
    rp = run_engine("public", pub, "apply", "--root", str(pub), "--proposal", str(pp))
    rl = run_engine("public", live, "apply", "--root", str(live), "--proposal", str(lp))

    assert rp.returncode == 0 and rl.returncode == 0, (rp.stderr, rl.stderr)
    assert normalize(json.loads(rp.stdout), roots=(pub, live)) == normalize(
        json.loads(rl.stdout), roots=(pub, live)
    )
    assert normalize(read_state(pub), roots=(pub, live)) == normalize(read_state(live), roots=(pub, live))


# ============================== LIVE-COMPARISON TESTS (need AO_PARITY_LIVE_ROOT) ===================
# 14b-1..14b-5 land here. Intentionally empty in 14b-0 except the gate-presence sanity check below.


def test_live_root_env_well_formed_if_set():
    """If AO_PARITY_LIVE_ROOT is set it must point at a real LIVE ao_state_writer package dir, and
    the derived src/tree must exist — a supplied-but-wrong env must FAIL loudly, never silently pass."""
    pkg = _live_pkg_root()
    if pkg is None:
        pytest.skip(f"{_LIVE_ROOT_ENV} not set — LIVE comparison slices (14b-1+) are skipped")
    assert pkg.is_dir(), f"{_LIVE_ROOT_ENV}={pkg} is not a directory"
    assert (pkg / "cli.py").exists(), f"{_LIVE_ROOT_ENV}={pkg} has no cli.py (not a LIVE engine pkg)"
    assert _live_src() is not None and _live_src().is_dir()
    assert _live_tree() is not None and _live_tree().is_dir()


# ------------------------------ 14b-1: SEED-A read-only smoke parity ------------------------------


def _require_live_state() -> tuple[Path, Path]:
    """Return (live_state_path, live_ledger_path); SKIP loudly if env unset, FAIL if the tree is wrong."""
    tree = _live_tree()
    if tree is None:
        pytest.skip(f"{_LIVE_ROOT_ENV} not set — SEED-A LIVE-copy parity (14b-1) skipped")
    state = tree / STATE_RELPATH
    if not state.exists():
        pytest.fail(f"LIVE state.json not found at {state} ({_LIVE_ROOT_ENV} points at a non-AO tree?)")
    return state, tree / LEDGER_RELPATH


def make_seed_a_copy(base: Path, name: str, engine: str) -> Path:
    """Build a COPY-ONLY SEED-A root for `engine`: contract(engine)+todo+fake-ao, then copy the REAL
    live state.json (+ledger) in READ-ONLY, then git-commit. Never mutates the live tree."""
    live_state, live_ledger = _require_live_state()
    root = (base / name).resolve()
    _assert_safe_copy_root(root)  # the destination must be a tmp copy, never at/under the live tree
    root.mkdir(parents=True, exist_ok=True)
    write_fake_ao(root)
    write_contract(root, _engine_action_tuples(engine, root))
    write_todo(root)
    dest_state = state_path(root)
    dest_state.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(live_state, dest_state)  # copy-only: open live read-only, write the copy
    if live_ledger.exists():
        shutil.copy2(live_ledger, root / LEDGER_RELPATH)
    git_commit_all(root)
    return root


def _assert_subcommand_present(engine: str, root: Path, subcommand: str) -> None:
    """codex Q4: probe presence and FAIL LOUD if a curated subcommand is missing — never silently
    shrink coverage. ``<subcommand> --help`` exits 0 iff the subparser exists."""
    proc = run_engine(engine, root, subcommand, "--help")
    assert proc.returncode == 0, (
        f"{engine} engine is missing curated subcommand {subcommand!r} (rc={proc.returncode}): {proc.stderr}"
    )


def _assert_seed_a_substantial(root: Path) -> None:
    """Prove SEED-A is a RICH real state, so the smoke parity is not vacuously equal over an empty
    or stubbed state (codex 14b-1 nit). Floors are conservative (the real state has 129/163/163 and
    only grows); a fresh/empty AO pointed at by AO_PARITY_LIVE_ROOT would FAIL here, loud."""
    state = read_state(root)
    assert state is not None, "SEED-A copy has no state.json — cannot run a meaningful smoke parity"
    dispatched = len(state.get("dispatched_proposals", {}))
    results = len(state.get("proposal_results", {}))
    revision = state.get("state_revision", 0)
    assert dispatched >= 50, f"SEED-A dispatched_proposals too few ({dispatched}) — not a rich live state"
    assert results >= 50, f"SEED-A proposal_results too few ({results}) — not a rich live state"
    assert revision >= 50, f"SEED-A state_revision too low ({revision}) — not a rich live state"


# SEED-A read-only projections dominated by the SHARED liveness obligation, hence parity-clean across
# engines on the real live-state copy (empirically verified). reconcile-once is intentionally NOT here:
# it emits a LIVE-only ``convergence_candidates`` key + an engine-specific advisory reconcile-command
# argv, handled as EXACT deltas in slice 14b-1b (not a broad strip).
SEED_A_READONLY_SUBCOMMANDS = ("list-ready", "list-gated")


@pytest.mark.parametrize("subcommand", SEED_A_READONLY_SUBCOMMANDS)
def test_seed_a_readonly_projection_parity(tmp_path, subcommand):
    """SEED-A SMOKE parity (14b-1): PUBLIC and LIVE, each fed a COPY of the REAL ~465KB live
    state.json, must produce equal NORMALIZED read-only projections AND mutate nothing. Proves the
    real state shape is read identically + safely by both engines; the controlled behavioural deltas
    live in the synthetic SEED-B slices."""
    pub = make_seed_a_copy(tmp_path, f"seedA-pub-{subcommand}", "public")
    live = make_seed_a_copy(tmp_path, f"seedA-live-{subcommand}", "live")
    _assert_seed_a_substantial(pub)  # both copies share the same live state bytes; check one
    _assert_subcommand_present("public", pub, subcommand)
    _assert_subcommand_present("live", live, subcommand)

    pre = (_sha256(state_path(pub)), _sha256(state_path(live)))
    rp = run_engine("public", pub, subcommand, "--root", str(pub))
    rl = run_engine("live", live, subcommand, "--root", str(live))

    assert rp.returncode == rl.returncode, (
        subcommand,
        rp.returncode,
        rl.returncode,
        rp.stderr,
        rl.stderr,
    )
    np = normalize(json.loads(rp.stdout), roots=(pub, live))
    nl = normalize(json.loads(rl.stdout), roots=(pub, live))
    assert np == nl, (
        f"{subcommand} cross-engine projection diverged after normalize:\n"
        f"PUB ={json.dumps(np)[:800]}\nLIVE={json.dumps(nl)[:800]}"
    )

    post = (_sha256(state_path(pub)), _sha256(state_path(live)))
    assert pre == post, f"{subcommand} mutated a state.json copy (read-only projection must not write)"


# ------------------------------ 14b-1b: reconcile-once exact-delta parity -------------------------
#
# reconcile-once is the one read-only projection that diverges cross-engine. The divergence is EXACTLY
# two non-behavioural deltas (empirically verified; codex before-consult PROCEED-WITH-CHANGES):
#   A. LIVE-only top-level key ``convergence_candidates`` — PUBLIC has no convergence-spawn lane
#      (KEEP-PUBLIC), so the key is structurally absent. Handled by ASSERT-PUBLIC-absent then POP-LIVE
#      (never a blind drop — proving PUBLIC really lacks the lane), regardless of the list's contents.
#   B. each ``historical_dispatch_records[*].spawned_dispatch_reconcile_commands.<slot>`` advisory argv
#      differs only in the engine's own INVOCATION FORM prefix (console-script vs ``env PYTHONPATH=...
#      python -m ao_state_writer.cli``); the SEMANTIC TAIL (subcommand + --root + --proposal-id +
#      evidence + terminal/refresh flag) is identical. Canonicalized to the tail; the dropped prefix is
#      also where the private claw-side path literals live, so the tail is leak-clean.
# Both deltas are kept OUT of the generic normalize() (codex Q5): they are exact, auditable, per-case.


def _reconcile_argv_tail(argv: list) -> list:
    """Slice an advisory reconcile-command argv to its engine-agnostic SEMANTIC TAIL.

    LIVE form: ``[env, PYTHONPATH=..., <python>, -m, ao_state_writer.cli, <subcommand>, ...]`` -> tail
    after ``ao_state_writer.cli``. PUBLIC form: ``[ao-state-writer, <subcommand>, ...]`` -> drop head.
    Any other shape passes through unchanged so a genuinely different argv surfaces as a real diff
    (codex Q3)."""
    if "ao_state_writer.cli" in argv:
        return argv[argv.index("ao_state_writer.cli") + 1:]
    if argv and argv[0] == "ao-state-writer":
        return argv[1:]
    return argv


def _canon_reconcile_cmds(obj):
    """Recursively canonicalize every ``spawned_dispatch_reconcile_commands`` argv to its semantic
    tail, asserting the known shape (tail starts with ``reconcile-spawned-dispatch``) so a malformed
    advisory command fails close to the bad shape (codex Q3 tightening). Pure (returns a new tree)."""
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if key == "spawned_dispatch_reconcile_commands" and isinstance(value, dict):
                canon = {}
                for slot, argv in value.items():
                    if isinstance(argv, list):
                        tail = _reconcile_argv_tail(argv)
                        assert tail and tail[0] == "reconcile-spawned-dispatch", (
                            f"unexpected reconcile-command shape in slot {slot!r}: {argv}"
                        )
                        canon[slot] = tail
                    else:
                        canon[slot] = value[slot]
                out[key] = canon
            else:
                out[key] = _canon_reconcile_cmds(value)
        return out
    if isinstance(obj, list):
        return [_canon_reconcile_cmds(item) for item in obj]
    return obj


def test_seed_a_reconcile_once_exact_delta_parity(tmp_path):
    """SEED-A reconcile-once parity (14b-1b): equal after the TWO exact non-behavioural deltas are
    handled — assert PUBLIC lacks the LIVE-only convergence-spawn key, then pop it from LIVE; and
    canonicalize the advisory reconcile-command argv invocation prefix. Everything else must match."""
    pub = make_seed_a_copy(tmp_path, "seedA-pub-reconcile", "public")
    live = make_seed_a_copy(tmp_path, "seedA-live-reconcile", "live")
    _assert_seed_a_substantial(pub)
    _assert_subcommand_present("public", pub, "reconcile-once")
    _assert_subcommand_present("live", live, "reconcile-once")

    pre = (_sha256(state_path(pub)), _sha256(state_path(live)))
    rp = run_engine("public", pub, "reconcile-once", "--root", str(pub))
    rl = run_engine("live", live, "reconcile-once", "--root", str(live))
    assert rp.returncode == rl.returncode == 0, (rp.returncode, rl.returncode, rp.stderr, rl.stderr)

    np = normalize(json.loads(rp.stdout), roots=(pub, live))
    nl = normalize(json.loads(rl.stdout), roots=(pub, live))

    # Delta A — convergence-spawn lane (KEEP-PUBLIC): PUBLIC must structurally lack the key.
    assert "convergence_candidates" not in np, (
        "PUBLIC unexpectedly grew a convergence-spawn lane (convergence_candidates present)"
    )
    nl.pop("convergence_candidates", None)

    # codex Q4: stay in the exercised regime — both must carry a non-empty historical_dispatch_records
    # (PUBLIC always emits the key even empty; LIVE omits it when empty, which would be a false diff on
    # a sparse seed). SEED-A is rich, so both are populated here.
    assert np.get("historical_dispatch_records"), "PUBLIC historical_dispatch_records empty/absent"
    assert nl.get("historical_dispatch_records"), "LIVE historical_dispatch_records empty/absent"

    # Delta B — canonicalize the advisory reconcile-command invocation prefix to its semantic tail.
    np = _canon_reconcile_cmds(np)
    nl = _canon_reconcile_cmds(nl)

    assert np == nl, (
        "reconcile-once cross-engine projection diverged after exact-delta handling:\n"
        f"PUB ={json.dumps(np)[:900]}\nLIVE={json.dumps(nl)[:900]}"
    )

    post = (_sha256(state_path(pub)), _sha256(state_path(live)))
    assert pre == post, "reconcile-once mutated a state.json copy (read-only projection must not write)"


# ------------------------------ 14b-2: mutating apply true-parity (synthetic SEED-B) --------------
#
# The first MUTATING cross-engine slice. On a synthetic CLEAN seed (empty state, byte-identical for
# both engines), apply the SAME proposal through PUBLIC and LIVE and assert the decision envelope AND
# the resulting state.json are normalized-equal — the cleanest zero-divergence proof that the two
# engines' apply() paths behave identically — plus idempotent-replay parity. apply legitimately
# mutates the tmp COPIES; never-touch-live is guaranteed by the copy-only seeds + the module tripwire
# (the live sha is rechecked at teardown). The brand-affected vocabulary does not appear on the
# evidence_pending -> codex_cc_review seed path (those tokens are SHARED across engines), so this is a
# TRUE-parity case with no allowlisted divergence.


def _seed_b_cross_engine_copies(base: Path, name: str) -> tuple[Path, Path]:
    """Build a shared CLEAN (empty-state) seed and return (pub_copy, live_copy), each resealed to its
    own engine's contract. Both start from byte-identical empty state."""
    seed = make_seed_root(base, f"{name}-seed", "public")  # contract vocab is reset per copy below
    pub = copy_and_reseal(seed, base / f"{name}-pub", "public")
    live = copy_and_reseal(seed, base / f"{name}-live", "live")
    return pub, live


def test_seed_b_apply_true_parity(tmp_path):
    """14b-2: cross-engine apply TRUE parity on a synthetic clean seed — identical decision envelope +
    post-state, and identical idempotent-replay decision. The first mutating behavioural slice."""
    _require_live_state()  # ensure the LIVE engine is available; skip loudly otherwise
    pub, live = _seed_b_cross_engine_copies(tmp_path, "applyB")

    pp = write_proposal(pub, SEED_PROPOSAL)
    lp = write_proposal(live, SEED_PROPOSAL)
    rp = run_engine("public", pub, "apply", "--root", str(pub), "--proposal", str(pp))
    rl = run_engine("live", live, "apply", "--root", str(live), "--proposal", str(lp))
    assert rp.returncode == rl.returncode == 0, (rp.returncode, rl.returncode, rp.stderr, rl.stderr)
    dp, dl = json.loads(rp.stdout), json.loads(rl.stdout)
    # Pin the decision-bearing fields explicitly (shared vocab) so equality cannot be satisfied by two
    # vacuous/empty envelopes — the parity must be over a REAL accepted seed decision (codex V1 nit).
    assert dp["decision"] == "accepted" and dp["next_required_action"] == "codex_cc_review", dp
    assert dl["decision"] == "accepted" and dl["next_required_action"] == "codex_cc_review", dl
    assert normalize(dp, roots=(pub, live)) == normalize(dl, roots=(pub, live)), (
        f"apply decision diverged:\nPUB ={rp.stdout[:400]}\nLIVE={rl.stdout[:400]}"
    )
    assert normalize(read_state(pub), roots=(pub, live)) == normalize(
        read_state(live), roots=(pub, live)
    ), "apply post-state diverged cross-engine"
    sha_after_apply = (_sha256(state_path(pub)), _sha256(state_path(live)))

    # Idempotent replay: the SAME proposal again must (a) yield the SAME decision + identical state on
    # both engines (cross-engine parity) AND (b) leave EACH copy's state byte-unchanged (standalone
    # idempotency — a true replay, not a double-apply; codex V3 nit).
    rp2 = run_engine("public", pub, "apply", "--root", str(pub), "--proposal", str(pp))
    rl2 = run_engine("live", live, "apply", "--root", str(live), "--proposal", str(lp))
    assert rp2.returncode == rl2.returncode, (rp2.returncode, rl2.returncode, rp2.stderr, rl2.stderr)
    assert normalize(json.loads(rp2.stdout), roots=(pub, live)) == normalize(
        json.loads(rl2.stdout), roots=(pub, live)
    ), f"idempotent re-apply decision diverged:\nPUB ={rp2.stdout[:400]}\nLIVE={rl2.stdout[:400]}"
    assert normalize(read_state(pub), roots=(pub, live)) == normalize(
        read_state(live), roots=(pub, live)
    ), "post-replay state diverged cross-engine"
    assert (_sha256(state_path(pub)), _sha256(state_path(live))) == sha_after_apply, (
        "re-applying the same proposal mutated a copy's state (not an idempotent replay)"
    )


# ------------------------------ 14b-3: convergence divergence-pin (synthetic SEED-B1) -------------
#
# The BEHAVIOURAL HEART. Drive both engines through the IDENTICAL multi-step escalation to
# ``repair_attempts_exhausted`` (a synthetic repair-ladder-exhaustion target), then prove the convergence-consumption
# DIVERGENCE is EXACTLY the Owner-decided KEEP-PUBLIC shape. Unlike 14b-1/1b/2 (equality tests where the
# engines are SUPPOSED to be identical), this is a DIVERGENCE-PINNING test where the engines are SUPPOSED
# to differ — so it asserts (a) the shared escalation is identical (incl. volatile-key SHAPE) and (b) the
# divergence is BOUNDED to the allowlisted convergence/dispatch deltas, every other subtree still equal.
#
#   PUBLIC (deterministic-convergence):  continue -> owner_proxy_convergence_required (blocked, no spawn);
#       record-final-convergence -> records deterministically.
#   LIVE (spawn-convergence-lane + cap):  continue -> enters the UN-PORTED spawn lane; record-final-
#       convergence -> fail-closed rejects (final-convergence cap: no review consumed).
#
# codex 14b-3 before-consult = APPROVE-WITH-NITS. Folded in: (Q1) tombstone-equality proves the shared
# escalation parity is not an artifact of asymmetric volatile-drop; (Q2) the LIVE continue divergence is
# pinned by a spawn-lane INVARIANT, not the exact ``spawn_baseline_unverified`` token (a minimal-shim
# artifact); (Q3) the fake-ao shim stays minimal (no LIVE session-metadata emulation — full convergence-
# review is a later LIVE-happy-path test); (Q4) the post-state divergence is bounded — only the
# allowlisted deltas are popped, the remainder asserted equal; (Q5) LIVE's rejection is worded as the
# minimal-shim "no review consumed" consequence, NOT LIVE happy-path behaviour.

_CODEX_CC_ENV = {"AO_CALLER_TYPE": "codex_cc"}
_ORCHESTRATOR_ENV = {"AO_CALLER_TYPE": "orchestrator", "AO_SESSION_ID": "example-orchestrator"}

# The next_required_action progression the 5-step drive must yield on BOTH engines (empirically verified;
# identical cross-engine — the shared escalation ladder).
_SEED_B1_DRIVE_PROGRESSION = [
    "codex_cc_review",
    "state_writer_closure",
    "dispatch_next_slice_plan_mode",
    "codex_cc_review",
    "repair_attempts_exhausted",
]

# The dispatched-proposal blocker record (dispatched_proposals["p-blocker-2"]) diverges by EXACTLY these
# shared-but-different fields (KEEP-PUBLIC): the PUBLIC deterministic terminal record vs the LIVE spawn
# lane. Allowlisted in step 5b so a reconciliation bug in any OTHER (shared) field of that record surfaces
# (codex confirm V4 — do not blind-pop the whole record).
_BLOCKER_DIVERGENT_FIELDS = {"status", "authorized_by"}
# LIVE spawn-lane-only fields on that record (structurally absent on PUBLIC). Allowlisted by NAME — their
# values are minimal-shim artifacts and are NOT pinned (codex Q2). Verified exhaustive against the real
# engines; a NEW LIVE-only field would (correctly) fail the step-5b remainder check, flagging a review.
_LIVE_SPAWN_LANE_FIELDS = {
    "ao_project_id",
    "baseline_attestation_result",
    "reason",
    "spawn_attestation",
    "spawn_session_id",
}


def _engine_codex_cc_receipt_vocab(engine: str, root_copy: Path) -> tuple[str, str]:
    """Extract (CODEX_CC_MODEL, CODEX_CC_REASONING_EFFORT) from the engine under test, so the drive's
    codex_cc receipt proposal matches the engine's own model gate without hardcoding a model literal in
    this PUBLIC source (the leak gate would otherwise have to allow it). Extracted via subprocess to stay
    engine-agnostic; the test additionally asserts the two engines agree (a real receipt-vocab parity)."""
    code = (
        "import json; from ao_state_writer.writer import CODEX_CC_MODEL, CODEX_CC_REASONING_EFFORT; "
        "print(json.dumps([CODEX_CC_MODEL, CODEX_CC_REASONING_EFFORT]))"
    )
    env = _engine_env(engine, root_copy / "bin", None)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(root_copy),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, f"codex_cc receipt-vocab extraction failed for {engine}: {proc.stderr}"
    model, reasoning = json.loads(proc.stdout)
    return model, reasoning


def _drive_seed_b1_to_exhausted(engine: str, root: Path, model: str, reasoning: str) -> list[str]:
    """Drive ONE engine's copy through the 5-step apply chain to ``repair_attempts_exhausted`` and return
    the next_required_action progression. Builds a SEED-B1: seed+close chapter-1 (with a real codex_cc
    receipt: AO_CALLER_TYPE=codex_cc + a transcript artifact under the copy + its sha256), then drives
    chapter-2 into a non_retryable blocker that exhausts the repair ladder. Engine-agnostic; every step is
    fail-loud (asserts accepted + the expected next action). Mutates only this tmp copy."""
    receipt = root / "reports" / "codex-cc-receipts" / "p-cc-1.txt"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text("codex-cc transcript ok", encoding="utf-8")
    receipt_sha = hashlib.sha256(receipt.read_bytes()).hexdigest()

    steps: list[tuple[dict, dict | None, str]] = [
        (
            dict(proposal_id="p-seed-1", target_kind="small_chapter", target_id="chapter-1",
                 base_state_revision=0, requested_state="evidence_pending", actor_role="implementer",
                 evidence_refs=["evidence#1"]),
            None, "codex_cc_review",
        ),
        (
            dict(proposal_id="p-cc-1", target_kind="small_chapter", target_id="chapter-1",
                 base_state_revision=1, requested_state="evidence_pending", actor_role="codex_cc",
                 evidence_refs=["transcript#1"], review_scope="codex_cc", verdict="pass",
                 model=model, reasoning_effort=reasoning, codex_cc_transcript_sha256=receipt_sha,
                 codex_cc_transcript_artifact_ref="artifact:reports/codex-cc-receipts/p-cc-1.txt"),
            _CODEX_CC_ENV, "state_writer_closure",
        ),
        (
            dict(proposal_id="p-close-1", target_kind="small_chapter", target_id="chapter-1",
                 base_state_revision=2, requested_state="closed", actor_role="implementer",
                 evidence_refs=["evidence#close"]),
            None, "dispatch_next_slice_plan_mode",
        ),
        (
            dict(proposal_id="p-seed-2", target_kind="small_chapter", target_id="chapter-2",
                 base_state_revision=3, requested_state="evidence_pending", actor_role="implementer",
                 evidence_refs=["evidence#1"]),
            None, "codex_cc_review",
        ),
        (
            dict(proposal_id="p-blocker-2", target_kind="small_chapter", target_id="chapter-2",
                 base_state_revision=4, requested_state="review_blocked", actor_role="codex_cc",
                 review_scope="codex_cc", evidence_refs=["evidence#blocker"], verdict="blocker",
                 blocker_code="hard_fail", model=model, reasoning_effort=reasoning, non_retryable=True),
            _CODEX_CC_ENV, "repair_attempts_exhausted",
        ),
    ]

    progression: list[str] = []
    for i, (proposal, env, expected) in enumerate(steps):
        pf = write_proposal(root, proposal, name=f"prop-{i}.json")
        r = run_engine(engine, root, "apply", "--root", str(root), "--proposal", str(pf), env_overrides=env)
        assert r.returncode == 0, (
            f"[{engine}] SEED-B1 drive step {i} ({proposal['proposal_id']}) failed rc={r.returncode}: {r.stderr}"
        )
        decision = json.loads(r.stdout)
        assert decision.get("decision") == "accepted", f"[{engine}] step {i} not accepted: {decision}"
        nxt = decision.get("next_required_action")
        assert nxt == expected, f"[{engine}] step {i} next={nxt!r} expected={expected!r}"
        progression.append(nxt)
    _git_add_commit(root, "seed-b1-exhausted")
    return progression


def test_seed_b1_convergence_divergence_parity(tmp_path):
    """14b-3 (behavioural heart): the shared escalation to ``repair_attempts_exhausted`` is byte-identical
    cross-engine; the convergence consumption DIVERGES by exactly the KEEP-PUBLIC shape, bounded to the
    allowlisted convergence/dispatch deltas (every other post-state subtree still equal)."""
    _require_live_state()  # skip loudly if the LIVE engine is unavailable
    pub, live = _seed_b_cross_engine_copies(tmp_path, "convB1")

    # The codex_cc receipt model gate must itself be cross-engine identical (else the two drives would be
    # silently gated by different models). Pin it as a real parity, then feed the shared value to both.
    pub_vocab = _engine_codex_cc_receipt_vocab("public", pub)
    live_vocab = _engine_codex_cc_receipt_vocab("live", live)
    assert pub_vocab == live_vocab, f"codex_cc receipt vocab diverged cross-engine: {pub_vocab} vs {live_vocab}"
    model, reasoning = pub_vocab

    pub_prog = _drive_seed_b1_to_exhausted("public", pub, model, reasoning)
    live_prog = _drive_seed_b1_to_exhausted("live", live, model, reasoning)
    assert pub_prog == live_prog == _SEED_B1_DRIVE_PROGRESSION, (pub_prog, live_prog)

    # (1) SHARED-ESCALATION PARITY — identical post-drive state, incl. volatile-key SHAPE (codex Q1: the
    #     tombstone variant makes an asymmetric volatile key surface as inequality, so the plain equality
    #     below cannot be manufactured by a one-sided volatile drop).
    sp, sl = read_state(pub), read_state(live)
    assert normalize(sp, roots=(pub, live)) == normalize(sl, roots=(pub, live)), (
        "shared escalation to repair_attempts_exhausted diverged cross-engine (before any convergence)"
    )
    assert normalize_with_volatile_tombstones(sp, roots=(pub, live)) == normalize_with_volatile_tombstones(
        sl, roots=(pub, live)
    ), "shared escalation diverged in volatile-key shape (tombstone check)"

    # (2) PUBLIC continue — DETERMINISTIC convergence block, no spawn (KEEP-PUBLIC).
    cp = run_engine("public", pub, "continue", "--root", str(pub))
    cl = run_engine("live", live, "continue", "--root", str(live))
    assert cp.returncode == 3 and cl.returncode == 3, (cp.returncode, cl.returncode, cp.stderr, cl.stderr)
    pub_cont, live_cont = json.loads(cp.stdout), json.loads(cl.stdout)
    assert pub_cont["result"] == "owner_proxy_convergence_required", pub_cont
    assert {"dispatch", "authorize", "spawn"} <= set(pub_cont.get("forbidden_actions", [])), pub_cont
    assert "owner_proxy_final_convergence_command" in pub_cont, pub_cont

    # (3) LIVE continue — enters the UN-PORTED spawn-convergence lane. Pin a spawn-lane INVARIANT, NOT the
    #     exact ``spawn_baseline_unverified`` token (a minimal-shim artifact; codex Q2): same proposal,
    #     different result than PUBLIC, no deterministic convergence command, and spawn evidence present.
    assert live_cont["proposal_id"] == pub_cont["proposal_id"] == "p-blocker-2", (live_cont, pub_cont)
    assert live_cont["result"] != pub_cont["result"], live_cont
    assert "owner_proxy_final_convergence_command" not in live_cont, live_cont
    assert (
        "spawn" in str(live_cont.get("result", ""))
        or live_cont.get("spawn_session_id")
        or any("spawn" in action for action in live_cont.get("allowed_repair_actions", []))
    ), f"LIVE continue did not enter the spawn-convergence lane: {live_cont}"

    # (4) record-final-convergence — PUBLIC records deterministically; LIVE (minimal-shim spawn lane, NO
    #     convergence-review consumed) fail-closed rejects via the final-convergence cap. codex Q5: this is
    #     the shim-path "no review consumed" consequence (reviews_used==0), NOT LIVE happy-path behaviour —
    #     a completed convergence-review would let LIVE record, which a later LIVE-happy-path test covers.
    rp = run_engine(
        "public", pub, "record-final-convergence", "--root", str(pub),
        "--proposal-id", "p-blocker-2", "--evidence", "owner-proxy:final-convergence",
        env_overrides=_ORCHESTRATOR_ENV,
    )
    rl = run_engine(
        "live", live, "record-final-convergence", "--root", str(live),
        "--proposal-id", "p-blocker-2", "--evidence", "owner-proxy:final-convergence",
        env_overrides=_ORCHESTRATOR_ENV,
    )
    assert rp.returncode == 0, rp.stderr
    assert rl.returncode == 3, rl.stderr
    pub_rec, live_rec = json.loads(rp.stdout), json.loads(rl.stdout)
    assert pub_rec["decision"] == "recorded", pub_rec
    assert pub_rec["result"] == "owner_proxy_final_convergence_recorded", pub_rec
    assert live_rec["decision"] == "rejected", live_rec
    assert live_rec["result"] == "final_convergence_not_required", live_rec
    assert live_rec["convergence_reviews_used"] == 0, live_rec  # shim spawn baseline failed -> no review consumed
    assert {
        "convergence_review_limit",
        "convergence_review_limit_scope",
        "convergence_reviews_used",
    } <= set(live_rec), live_rec

    # (5) POST-STATE BOUND (codex Q4): pop ONLY the allowlisted convergence/dispatch deltas, each with an
    #     exact-shape assert; EVERY other subtree must remain normalized-equal.
    post_pub = normalize(read_state(pub), roots=(pub, live))
    post_live = normalize(read_state(live), roots=(pub, live))

    # 5a. PUBLIC-only deterministic final-convergence record (the KEEP-PUBLIC terminal). LIVE rejected, so
    #     it is structurally absent there.
    assert "owner_proxy_final_convergence_records" in post_pub, sorted(post_pub)
    assert "owner_proxy_final_convergence_records" not in post_live, sorted(post_live)
    pub_final = post_pub.pop("owner_proxy_final_convergence_records")
    assert "p-blocker-2" in pub_final, pub_final  # recorded against the exhausted target

    # 5b. dispatched_proposals[p-blocker-2] is the single proposal whose record diverges: PUBLIC terminal-
    #     recorded vs LIVE spawn-lane. Rather than blind-popping the whole record (codex confirm V4 — that
    #     would mask a reconciliation bug in a NON-divergent field), pop it off both sides, PIN the expected
    #     divergent fields, then assert every REMAINING field is cross-engine equal.
    pub_bl = post_pub.get("dispatched_proposals", {}).pop("p-blocker-2", None)
    live_bl = post_live.get("dispatched_proposals", {}).pop("p-blocker-2", None)
    assert pub_bl and live_bl, (pub_bl, live_bl)
    # status diverges: PUBLIC deterministic terminal-record vs LIVE spawn-lane (don't pin the exact LIVE
    # token — a minimal-shim artifact; codex Q2).
    assert pub_bl.get("status") == "final_convergence_recorded", pub_bl
    assert live_bl.get("status") != pub_bl.get("status"), live_bl
    assert "spawn" in str(live_bl.get("status", "")) or live_bl.get("spawn_session_id"), live_bl
    # authorized_by diverges: PUBLIC deterministic 'orchestrator' vs LIVE spawn-lane authorizer.
    assert pub_bl.get("authorized_by") == "orchestrator", pub_bl
    assert live_bl.get("authorized_by") != pub_bl.get("authorized_by"), live_bl
    # Every OTHER field of the blocker record must be cross-engine equal: allowlist exactly the divergent
    # fields + the LIVE spawn-lane-only fields, then compare the remainder (codex confirm V4 tightening).
    pub_rest = {k: v for k, v in pub_bl.items() if k not in _BLOCKER_DIVERGENT_FIELDS}
    live_rest = {
        k: v
        for k, v in live_bl.items()
        if k not in _BLOCKER_DIVERGENT_FIELDS and k not in _LIVE_SPAWN_LANE_FIELDS
    }
    assert pub_rest == live_rest, (
        "blocker dispatch record diverged beyond the allowlisted convergence fields "
        f"(a reconciliation bug in a shared field would surface here):\nPUB ={pub_rest}\nLIVE={live_rest}"
    )

    assert post_pub == post_live, (
        "post-state diverged BEYOND the allowlisted convergence/dispatch deltas (a real reconciliation "
        "bug would surface here):\n"
        f"PUB ={json.dumps(post_pub)[:900]}\nLIVE={json.dumps(post_live)[:900]}"
    )


# ------------------------------ 14b-4: spawn / dispatch_kind allowlist (synthetic SEED-B2) --------
#
# On a CLEAN AUTO_SPAWN, the spawn dispatch must be cross-engine parity-equal EXCEPT the LIVE-only
# {dispatch_kind, ao_project_id} keys. Getting BOTH engines to a clean spawn requires bridging TWO real
# KEEP-PUBLIC divergences (each pinned/documented here, but neither is the comparison target):
#   (i)  S-GAP1 governance-filename generalization — the AUTO_SPAWN `continue` runs a governance preflight
#        that looks for the canonical doc trio. PUBLIC (generalized) resolves it from the contract
#        [canonical], default GENERIC {MASTER_PLAN,TODO,SESSION_LOG}.md; LIVE (un-generalized) HARDCODES a
#        private prefixed trio (a leak-forbidden literal). We feed each engine its OWN trio, derived from
#        the engine at runtime (never hardcoding the private name), and assert the S-GAP1 shape.
#   (ii) spawn-baseline is LIVE-ONLY — PUBLIC clean-spawns with no baseline check; LIVE verifies a session
#        baseline (a session-metadata JSON whose worktree HEAD must match the source HEAD). We satisfy
#        LIVE's baseline inside a TMP AO projects root via AO_PROJECTS_ROOT (NEVER ~/.agent-orchestrator),
#        pointing the worktree at the (git) LIVE copy root so the baseline verifies clean.
#
# codex design before-consult = APPROVE-WITH-CHANGES (Option A); all V1-V6 folded: V1 assert LIVE baseline
# VERIFIED (no baseline-failure fields), not merely written; V2 pin+assert PUBLIC has no spawn-baseline;
# V3 exact per-record delta (allowlist only dispatch_kind/ao_project_id, shared fields equal); V4 assert the
# S-GAP1 filename shape without the forbidden literal; V5 never-touch-live guards on AO_PROJECTS_ROOT and
# the tmp session path/worktree; V6 non-vacuity asserts that a real spawn happened on BOTH engines.

EXAMPLE_PROJECT_ID = "example-project"  # matches write_contract's [owner_proxy] project_id
_GENERIC_CANONICAL_TRIO = frozenset({"MASTER_PLAN.md", "TODO.md", "SESSION_LOG.md"})
# LIVE-only keys on a CLEAN spawn dispatch record (absent on PUBLIC's spawn record). Allowlisted as an EXACT
# per-record delta (codex V3); their presence/values are LIVE confirm_dispatch metadata, not a divergence in
# the spawn decision itself.
_LIVE_DISPATCH_ONLY_FIELDS = {"dispatch_kind", "ao_project_id"}


def _engine_governance_md(engine: str, root: Path) -> list[str]:
    """Extract this engine's required governance filenames at runtime (so the private LIVE trio never
    appears as a literal in this PUBLIC source). Subprocess-imports the engine's preflight GOVERNANCE_FILES."""
    code = (
        "import json; from ao_state_writer import preflight as p; "
        "print(json.dumps(sorted(getattr(p, 'GOVERNANCE_FILES', []))))"
    )
    env = _engine_env(engine, root / "bin", None)
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=str(root), capture_output=True, text=True, check=False, env=env
    )
    assert proc.returncode == 0, f"governance-file extraction failed for {engine}: {proc.stderr}"
    return json.loads(proc.stdout)


def _write_engine_governance(engine: str, root: Path) -> list[str]:
    """Write each governance .md file this engine's preflight requires (so the AUTO_SPAWN governance gate
    passes) and return the TOP-LEVEL .md filenames. Each engine gets its OWN canonical trio — this is how the
    S-GAP1 generalization is neutralized for this behavioural test without putting the private literal here."""
    todo_body = (
        "# Example Project TODO\n\n## Current Execution State\n\n"
        "- current_phase: example-phase\n- next_locked_action: implement-example-slice\n"
        "- review_gate_state: none\n- latest_session_log_anchor: example-session-log-anchor\n"
    )
    top_level: list[str] = []
    for fname in _engine_governance_md(engine, root):
        if not fname.endswith(".md"):
            continue
        target = root / fname
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(todo_body if "TODO" in fname else f"# {fname}\n", encoding="utf-8")
        if "/" not in fname:
            top_level.append(fname)
    return top_level


def _write_spawn_session_metadata(ao_home: Path, project_id: str, session_id: str, worktree: Path) -> Path:
    """Write LIVE's spawn-baseline session metadata into a TMP AO projects root (NEVER ~/.agent-orchestrator).
    LIVE checks the session's worktree HEAD against the source-root HEAD; pointing the worktree at the (git)
    copy root itself makes the baseline verify clean. PUBLIC has no spawn-baseline and ignores all of this."""
    sess = ao_home / project_id / "sessions" / f"{session_id}.json"
    sess.parent.mkdir(parents=True, exist_ok=True)
    sess.write_text(json.dumps({"worktree": str(Path(worktree).resolve())}), encoding="utf-8")
    return sess


def test_seed_b2_spawn_dispatch_kind_parity(tmp_path):
    """14b-4: on a clean AUTO_SPAWN, the spawn dispatch is cross-engine parity-equal EXCEPT the LIVE-only
    {dispatch_kind, ao_project_id} keys — bridging the S-GAP1 governance-filename divergence and the
    spawn-baseline-is-LIVE-only divergence (both pinned below, neither the comparison target)."""
    _require_live_state()
    pub, live = _seed_b_cross_engine_copies(tmp_path, "spawnB2")

    # --- (i) S-GAP1 governance-filename divergence: feed each engine its OWN derived canonical trio. ---
    pub_md = _write_engine_governance("public", pub)
    live_md = _write_engine_governance("live", live)
    pub_only = set(pub_md) - set(live_md)
    live_only = set(live_md) - set(pub_md)
    # V4 — assert the S-GAP1 shape WITHOUT hardcoding the forbidden private literal: PUBLIC's divergent trio
    # is exactly the generic canonical names; LIVE's are non-generic prefixed variants of the same stems.
    assert pub_only == set(_GENERIC_CANONICAL_TRIO), pub_only
    assert len(live_only) == len(_GENERIC_CANONICAL_TRIO), live_only
    assert live_only.isdisjoint(_GENERIC_CANONICAL_TRIO), live_only
    assert all(any(name.endswith(stem) for stem in _GENERIC_CANONICAL_TRIO) for name in live_only), live_only

    # --- drive each target to an AUTO_SPAWN next_required_action. ---
    auto_spawn = set(_engine_action_tuples("public", pub)["auto_spawn"])
    for engine, root in (("public", pub), ("live", live)):
        pf = write_proposal(root, SEED_PROPOSAL, name="p0.json")
        r = run_engine(engine, root, "apply", "--root", str(root), "--proposal", str(pf))
        assert r.returncode == 0, (engine, r.stderr)
        nxt = json.loads(r.stdout).get("next_required_action")
        assert nxt in auto_spawn, f"[{engine}] seed did not land on an AUTO_SPAWN action: {nxt!r}"
        _git_add_commit(root, "seed-b2")

    # --- (ii) spawn-baseline is LIVE-only. PUBLIC gets an EMPTY tmp AO home (proving it needs no session
    #     metadata); LIVE gets a populated one whose session worktree = the LIVE copy root (verifies clean). ---
    pub_ao_home = tmp_path / "pub-ao-home"  # intentionally empty — PUBLIC must spawn without AO metadata
    live_ao_home = tmp_path / "live-ao-home"
    sess_path = _write_spawn_session_metadata(live_ao_home, EXAMPLE_PROJECT_ID, FAKE_SPAWN_SESSION_ID, live)
    pub_env = {"AO_PROJECTS_ROOT": str(pub_ao_home)}
    live_env = {"AO_PROJECTS_ROOT": str(live_ao_home)}

    cp = run_engine("public", pub, "continue", "--root", str(pub), env_overrides=pub_env)
    cl = run_engine("live", live, "continue", "--root", str(live), env_overrides=live_env)
    pub_cont, live_cont = json.loads(cp.stdout), json.loads(cl.stdout)

    # V6 — a REAL spawn happened on BOTH (not blocked/failed/no-op); shared session id present on both.
    assert cp.returncode == 0 and pub_cont.get("result") == "spawned", (cp.returncode, pub_cont)
    assert cl.returncode == 0 and live_cont.get("result") == "spawned", (cl.returncode, live_cont)
    assert pub_cont.get("spawn_session_id") == live_cont.get("spawn_session_id") == FAKE_SPAWN_SESSION_ID, (
        pub_cont,
        live_cont,
    )

    pub_state = normalize(read_state(pub), roots=(pub, live))
    live_state = normalize(read_state(live), roots=(pub, live))
    pub_rec = pub_state.get("dispatched_proposals", {}).get("p-seed-1")
    live_rec = live_state.get("dispatched_proposals", {}).get("p-seed-1")
    assert pub_rec and live_rec, (pub_rec, live_rec)  # V6 — both records exist

    # V1 — LIVE baseline VERIFIED (not merely written): the clean record carries NO baseline-failure fields.
    assert "baseline_attestation_result" not in live_rec and "reason" not in live_rec, live_rec
    assert live_rec.get("status") == "spawned", live_rec
    assert "dispatch_kind" in live_rec, live_rec  # V6 — LIVE actually wrote dispatch_kind on a clean dispatch
    # V2 — PUBLIC clean-spawns with NO spawn-baseline (KEEP-PUBLIC: not ported). Even with an EMPTY AO home,
    # PUBLIC spawned, and its record carries none of the LIVE baseline / dispatch_kind machinery.
    assert pub_rec.get("status") == "spawned", pub_rec
    assert not ({"baseline_attestation_result", "reason", "dispatch_kind", "ao_project_id"} & set(pub_rec)), pub_rec

    # V3 — EXACT per-record delta (not a generic normalize strip): allowlist ONLY the LIVE-only
    # {dispatch_kind, ao_project_id}; the shared spawn fields must be equal and the remainder must match.
    for shared in ("status", "authorized_by", "spawn_session_id", "spawn_attestation"):
        assert live_rec.get(shared) == pub_rec.get(shared), (shared, pub_rec, live_rec)
    # codex confirm W4 — pin the shared fields' exact SEMANTIC values (not just cross-engine equality), so a
    # change that alters BOTH engines identically (e.g. a different authorizer/attestation) cannot pass silently.
    assert pub_rec.get("authorized_by") == "orchestrator_policy", pub_rec
    assert pub_rec.get("spawn_attestation") == "session", pub_rec
    assert pub_rec.get("spawn_session_id") == FAKE_SPAWN_SESSION_ID, pub_rec
    # ... and the LIVE-only dispatch metadata's exact values (a clean NON-convergence spawn for this project).
    assert live_rec.get("dispatch_kind") == "non_convergence_spawn", live_rec
    assert live_rec.get("ao_project_id") == EXAMPLE_PROJECT_ID, live_rec
    live_rest = {k: v for k, v in live_rec.items() if k not in _LIVE_DISPATCH_ONLY_FIELDS}
    assert live_rest == pub_rec, (
        f"spawn record diverged beyond {sorted(_LIVE_DISPATCH_ONLY_FIELDS)}:\nPUB ={pub_rec}\nLIVE_rest={live_rest}"
    )

    # The rest of post-spawn state (minus the one divergent dispatched record) must match cross-engine.
    pub_state.get("dispatched_proposals", {}).pop("p-seed-1", None)
    live_state.get("dispatched_proposals", {}).pop("p-seed-1", None)
    assert pub_state == live_state, "post-spawn state diverged beyond the allowlisted spawn dispatch record"

    # V5 — never-touch-live guards: AO_PROJECTS_ROOT propagated; the LIVE session path + worktree resolve
    # under tmp and OUTSIDE the LIVE tree (the real ~/.agent-orchestrator is never read or written).
    assert "AO_PROJECTS_ROOT" in live_env and "AO_PROJECTS_ROOT" in pub_env
    assert _is_within(sess_path, tmp_path) and not _is_within(sess_path, _live_tree())
    assert _is_within(live, tmp_path)


# ------------------------------ 14b-5: reconcile/authorize inert + applicable + out-of-scope ------
#
# The FINAL shadow-replay slice. Covers the remaining orchestrator-caller reconcile/authorize subcommands
# and documents the subcommands that cannot be behaviourally replayed on a frozen copy. codex design
# before-consult = APPROVE-WITH-CHANGES; all folded:
#   * reconcile-spawn-attestation + authorize on a clean spawned (and already-attested / non-gated) target:
#     CLEAN inert-parity — identical reject envelope, no state mutation (Q6 non-vacuity asserts).
#   * reconcile-spawned-dispatch (NO mode flag) on a NON-reconcile proposal (action codex_cc_review):
#     an OFF-PATH divergence codex adjudicated an ACCEPTABLE KEEP-PUBLIC CLI/writer gating relocation (PUBLIC
#     writer rejects `ambiguous_spawned_dispatch_reconcile` rc2 before action classification; LIVE CLI early-
#     returns `not_spawned_dispatch_reconcile_action` rc3). Both REJECT a codex_cc_review dispatch and leave
#     state equal. PINNED as an exact divergence (Q3 — not excluded), so a future change to either
#     classification fails the gate.
#   * reconcile-spawned-dispatch --refresh-time on a NORMAL spawned lease whose action IS a reconcile action
#     (dispatch_next_slice_plan_mode): APPLICABLE-parity — both engines take the same applicable path and
#     refresh the lease identically (Q4/V2 — the stronger proof). Post-state equal after stripping the LIVE-
#     only confirm_dispatch metadata, which the refresh also embeds in its reconciliation audit record.
#   * out-of-scope subcommands (need a live `ao session ls` readback / external review-bridge subprocess /
#     observed timeout): assert CLI/API SURFACE parity (presence + arg surface) — explicitly NOT behavioural
#     replay parity (Q5) — incl. the escalated-review-actuate brand-rename at the subcommand level.

_RECONCILE_AUTH_RECEIPT_BODY = "codex-cc transcript ok"
# Out-of-scope-for-replay subcommands that nonetheless share an IDENTICAL CLI arg surface cross-engine.
_OUT_OF_SCOPE_SHARED_SUBCOMMANDS = ("reconcile-spawned-dispatch", "authorize", "watchdog")
# M1 forward-ported subcommands: ALSO behaviourally replayed (14b-6/7/8 below); listed here so the
# cheap surface-parity check still pins their arg surface against silent cross-engine drift.
_M1_SHARED_SUBCOMMANDS = ("pause", "resume", "dispatch-stall-check", "retire-dead-sessions")


def _strip_live_dispatch_metadata(obj):
    """Recursively drop the LIVE-only confirm_dispatch metadata keys ({dispatch_kind, ao_project_id})
    wherever they appear — the top-level dispatched_proposals record AND the dispatch-record snapshots a
    refresh embeds in its reconciliation audit log. These are LIVE-only (PUBLIC never emits them), so
    stripping them cannot hide a PUBLIC bug, and ONLY these two named keys are removed (a narrow vocabulary
    allowlist, not a broad behavioural strip)."""
    if isinstance(obj, dict):
        return {
            k: _strip_live_dispatch_metadata(v)
            for k, v in obj.items()
            if k not in _LIVE_DISPATCH_ONLY_FIELDS
        }
    if isinstance(obj, list):
        return [_strip_live_dispatch_metadata(x) for x in obj]
    return obj


def _spawn_seed_b2(tmp_path, name: str, *, to_dispatch: bool):
    """Build a clean-spawn SEED-B2 on both engines (governance + LIVE spawn-baseline in a tmp AO home) and
    return (pub, live, pub_env, live_env, spawned_pid). to_dispatch=False -> apply only p-seed-1 so the
    spawned dispatch's action is codex_cc_review (NOT a reconcile action). to_dispatch=True -> drive 3 steps
    (seed -> codex_cc receipt -> close chapter-1) so p-close-1 is spawned with action
    dispatch_next_slice_plan_mode (a reconcile action). Orchestrator-caller envs include AO_PROJECTS_ROOT."""
    pub, live = _seed_b_cross_engine_copies(tmp_path, name)
    _write_engine_governance("public", pub)
    _write_engine_governance("live", live)
    model, reasoning = _engine_codex_cc_receipt_vocab("public", pub)
    for engine, root in (("public", pub), ("live", live)):
        if to_dispatch:
            receipt = root / "reports" / "codex-cc-receipts" / "p-cc-1.txt"
            receipt.parent.mkdir(parents=True, exist_ok=True)
            receipt.write_text(_RECONCILE_AUTH_RECEIPT_BODY, encoding="utf-8")
            receipt_sha = hashlib.sha256(receipt.read_bytes()).hexdigest()
            steps = [
                (SEED_PROPOSAL, None),
                (
                    dict(proposal_id="p-cc-1", target_kind="small_chapter", target_id="chapter-1",
                         base_state_revision=1, requested_state="evidence_pending", actor_role="codex_cc",
                         evidence_refs=["transcript#1"], review_scope="codex_cc", verdict="pass",
                         model=model, reasoning_effort=reasoning, codex_cc_transcript_sha256=receipt_sha,
                         codex_cc_transcript_artifact_ref="artifact:reports/codex-cc-receipts/p-cc-1.txt"),
                    _CODEX_CC_ENV,
                ),
                (
                    dict(proposal_id="p-close-1", target_kind="small_chapter", target_id="chapter-1",
                         base_state_revision=2, requested_state="closed", actor_role="implementer",
                         evidence_refs=["evidence#close"]),
                    None,
                ),
            ]
        else:
            steps = [(SEED_PROPOSAL, None)]
        for i, (proposal, env) in enumerate(steps):
            pf = write_proposal(root, proposal, name=f"{name}-s{i}.json")
            r = run_engine(engine, root, "apply", "--root", str(root), "--proposal", str(pf), env_overrides=env)
            assert r.returncode == 0 and json.loads(r.stdout).get("decision") == "accepted", (
                engine, i, r.stdout[:300], r.stderr[:200],
            )
        _git_add_commit(root, f"{name}-drive")

    live_home = tmp_path / f"{name}-live-home"
    _write_spawn_session_metadata(live_home, EXAMPLE_PROJECT_ID, FAKE_SPAWN_SESSION_ID, live)
    pub_env = {**_ORCHESTRATOR_ENV, "AO_PROJECTS_ROOT": str(tmp_path / f"{name}-pub-home")}
    live_env = {**_ORCHESTRATOR_ENV, "AO_PROJECTS_ROOT": str(live_home)}
    cp = run_engine("public", pub, "continue", "--root", str(pub), env_overrides=pub_env)
    cl = run_engine("live", live, "continue", "--root", str(live), env_overrides=live_env)
    pub_cont, live_cont = json.loads(cp.stdout), json.loads(cl.stdout)
    assert cp.returncode == 0 and pub_cont.get("result") == "spawned", (cp.returncode, pub_cont)
    assert cl.returncode == 0 and live_cont.get("result") == "spawned", (cl.returncode, live_cont)
    spawned_pid = pub_cont["proposal_id"]
    assert live_cont["proposal_id"] == spawned_pid, (pub_cont, live_cont)
    return pub, live, pub_env, live_env, spawned_pid


def test_seed_b2_reconcile_spawn_attestation_inert_parity(tmp_path):
    """14b-5: reconcile-spawn-attestation on an already-attested spawned dispatch is INERT and cross-engine
    parity-equal — both reject `not_spawned_unattested` with no state mutation."""
    pub, live, penv, lenv, pid = _spawn_seed_b2(tmp_path, "ra", to_dispatch=False)
    # non-vacuity (Q6): the target really is a spawned + attested dispatch before the inert command.
    rec = read_state(pub)["dispatched_proposals"][pid]
    assert rec["status"] == "spawned" and rec["spawn_attestation"] == "session", rec
    pre = (_sha256(state_path(pub)), _sha256(state_path(live)))
    rp = run_engine("public", pub, "reconcile-spawn-attestation", "--proposal-id", pid,
                    "--evidence", "owner:attest", "--spawn-session-id", FAKE_SPAWN_SESSION_ID,
                    "--root", str(pub), env_overrides=penv)
    rl = run_engine("live", live, "reconcile-spawn-attestation", "--proposal-id", pid,
                    "--evidence", "owner:attest", "--spawn-session-id", FAKE_SPAWN_SESSION_ID,
                    "--root", str(live), env_overrides=lenv)
    assert rp.returncode == rl.returncode, (rp.returncode, rl.returncode, rp.stderr, rl.stderr)
    np = normalize(json.loads(rp.stdout), roots=(pub, live))
    nl = normalize(json.loads(rl.stdout), roots=(pub, live))
    assert np == nl, (np, nl)
    # decision-bearing pin (non-vacuous): both REJECT not_spawned_unattested (already attested).
    assert np["decision"] == "rejected" and np["reason"] == "not_spawned_unattested", np
    assert np["result"] == "spawn_attestation_reconcile_rejected", np
    assert (_sha256(state_path(pub)), _sha256(state_path(live))) == pre, "inert reconcile mutated a copy"


def test_seed_b2_authorize_inert_parity(tmp_path):
    """14b-5: authorize on a NON-gated target is INERT and cross-engine parity-equal — both return
    `not_authorizable` (the target's obligation is codex_cc_review, not a gated action) with no mutation."""
    pub, live, penv, lenv, pid = _spawn_seed_b2(tmp_path, "au", to_dispatch=False)
    # non-vacuity (codex confirm V1/V3): pin the explicit spawned + attested precondition (as the
    # attestation test does) so the not_authorizable parity is over a REAL spawned target, not an empty one.
    rec = read_state(pub)["dispatched_proposals"][pid]
    assert rec["status"] == "spawned" and rec["spawn_attestation"] == "session", rec
    pre = (_sha256(state_path(pub)), _sha256(state_path(live)))
    rp = run_engine("public", pub, "authorize", "--proposal-id", pid, "--evidence", "owner:authorize",
                    "--root", str(pub), env_overrides=penv)
    rl = run_engine("live", live, "authorize", "--proposal-id", pid, "--evidence", "owner:authorize",
                    "--root", str(live), env_overrides=lenv)
    assert rp.returncode == rl.returncode, (rp.returncode, rl.returncode, rp.stderr, rl.stderr)
    np = normalize(json.loads(rp.stdout), roots=(pub, live))
    nl = normalize(json.loads(rl.stdout), roots=(pub, live))
    assert np == nl, (np, nl)
    assert np["result"] == "not_authorizable" and np["next_required_action"] == "codex_cc_review", np
    assert (_sha256(state_path(pub)), _sha256(state_path(live))) == pre, "inert authorize mutated a copy"


def test_seed_b2_reconcile_spawned_dispatch_offpath_divergence_pin(tmp_path):
    """14b-5: reconcile-spawned-dispatch (no mode flag) on a NON-reconcile proposal (action
    codex_cc_review) diverges by an ACCEPTABLE KEEP-PUBLIC CLI/writer gating relocation (codex-adjudicated,
    not a bug): PUBLIC's writer rejects `ambiguous_spawned_dispatch_reconcile` (rc2) before action
    classification; LIVE's CLI early-returns `not_spawned_dispatch_reconcile_action` (rc3). Both REJECT a
    codex_cc_review dispatch and leave state equal. PINNED as an exact divergence (not excluded), so a
    future change to either classification fails the gate."""
    pub, live, penv, lenv, pid = _spawn_seed_b2(tmp_path, "rsd", to_dispatch=False)
    pre = (_sha256(state_path(pub)), _sha256(state_path(live)))
    rp = run_engine("public", pub, "reconcile-spawned-dispatch", "--proposal-id", pid,
                    "--evidence", "owner:reconcile", "--root", str(pub), env_overrides=penv)
    rl = run_engine("live", live, "reconcile-spawned-dispatch", "--proposal-id", pid,
                    "--evidence", "owner:reconcile", "--root", str(live), env_overrides=lenv)
    pub_env_out, live_env_out = json.loads(rp.stdout), json.loads(rl.stdout)
    # Both REJECT (neither reconciles a codex_cc_review dispatch), with the pinned engine-specific shapes.
    assert rp.returncode == 2, (rp.returncode, pub_env_out)
    assert rl.returncode == 3, (rl.returncode, live_env_out)
    assert pub_env_out["result"] == "spawned_dispatch_reconcile_rejected", pub_env_out
    assert pub_env_out["reason"] == "ambiguous_spawned_dispatch_reconcile", pub_env_out
    assert live_env_out["result"] == "not_spawned_dispatch_reconcile_action", live_env_out
    assert live_env_out["next_required_action"] == "codex_cc_review", live_env_out
    # Both refused to mutate; post-state equal after stripping the LIVE-only dispatch metadata.
    assert _strip_live_dispatch_metadata(
        normalize(read_state(pub), roots=(pub, live))
    ) == _strip_live_dispatch_metadata(
        normalize(read_state(live), roots=(pub, live))
    ), "off-path reconcile-spawned-dispatch diverged in post-state"
    assert (_sha256(state_path(pub)), _sha256(state_path(live))) == pre, "off-path reconcile mutated a copy"


def test_seed_b2_reconcile_spawned_dispatch_refresh_applicable_parity(tmp_path):
    """14b-5 (Q4/V2 stronger proof): on a NORMAL spawned lease whose action IS a reconcile action
    (dispatch_next_slice_plan_mode), reconcile-spawned-dispatch --refresh-time is APPLICABLE and refreshes
    the lease IDENTICALLY cross-engine — the SAME applicable path on both engines (no off-path divergence).
    Post-state equal after stripping the LIVE-only confirm_dispatch metadata, which the refresh embeds both
    in the dispatched record AND in its reconciliation audit record's dispatch-record snapshots."""
    pub, live, penv, lenv, pid = _spawn_seed_b2(tmp_path, "rfr", to_dispatch=True)
    assert pid == "p-close-1", pid  # the spawned dispatch whose action is dispatch_next_slice_plan_mode
    rp = run_engine("public", pub, "reconcile-spawned-dispatch", "--proposal-id", pid,
                    "--evidence", "owner:refresh", "--refresh-time", "--root", str(pub), env_overrides=penv)
    rl = run_engine("live", live, "reconcile-spawned-dispatch", "--proposal-id", pid,
                    "--evidence", "owner:refresh", "--refresh-time", "--root", str(live), env_overrides=lenv)
    assert rp.returncode == rl.returncode == 0, (rp.returncode, rl.returncode, rp.stderr, rl.stderr)
    np = normalize(json.loads(rp.stdout), roots=(pub, live))
    nl = normalize(json.loads(rl.stdout), roots=(pub, live))
    assert np == nl, (np, nl)
    # decision-bearing pin (non-vacuous): both ACTUALLY refreshed the lease (applicable path taken).
    assert np["decision"] == "refreshed" and np["outcome"] == "refreshed_time", np
    assert np["result"] == "spawned_dispatch_time_refreshed", np
    assert _strip_live_dispatch_metadata(
        normalize(read_state(pub), roots=(pub, live))
    ) == _strip_live_dispatch_metadata(
        normalize(read_state(live), roots=(pub, live))
    ), "refresh post-state diverged beyond the LIVE-only confirm_dispatch metadata"


def _subcommand_arg_flags(engine: str, root: Path, subcommand: str) -> set[str]:
    """Return the set of long-option flags a subcommand's parser exposes (its CLI surface)."""
    proc = run_engine(engine, root, subcommand, "--help")
    assert proc.returncode == 0, (engine, subcommand, proc.stderr)
    flags: set[str] = set()
    for line in proc.stdout.splitlines():
        for token in line.replace("=", " ").split():
            if token.startswith("--"):
                flags.add(token.rstrip(","))
    return flags


def test_out_of_scope_subcommand_surface_parity(tmp_path):
    """14b-5: the subcommands that cannot be behaviourally replayed on a frozen copy (they need a live
    `ao session ls` readback / an external review-bridge subprocess / an observed timeout) still share an
    IDENTICAL CLI/API surface cross-engine. This asserts SURFACE parity (presence + arg flags) — explicitly
    NOT behavioural replay parity — plus the escalated-review-actuate brand-rename at the subcommand level."""
    pub = make_seed_root(tmp_path, "oos-pub", "public")
    live = make_seed_root(tmp_path, "oos-live", "live")  # skips loudly if the LIVE engine is unavailable
    for sub in _OUT_OF_SCOPE_SHARED_SUBCOMMANDS + _M1_SHARED_SUBCOMMANDS:
        _assert_subcommand_present("public", pub, sub)
        _assert_subcommand_present("live", live, sub)
        pub_flags = _subcommand_arg_flags("public", pub, sub)
        live_flags = _subcommand_arg_flags("live", live, sub)
        assert pub_flags == live_flags, (
            f"{sub} CLI arg surface diverged: PUB-only={sorted(pub_flags - live_flags)} "
            f"LIVE-only={sorted(live_flags - pub_flags)}"
        )
    # The actuate subcommand is brand-renamed at the subcommand level: PUBLIC exposes the neutral
    # `escalated-review-actuate`; the LIVE (private brand) spelling is a leak-forbidden literal, so assert
    # PUBLIC has the neutral spelling and LIVE does NOT (proving the rename without naming the LIVE token).
    assert run_engine("public", pub, "escalated-review-actuate", "--help").returncode == 0, (
        "PUBLIC missing the neutral actuate subcommand"
    )
    assert run_engine("live", live, "escalated-review-actuate", "--help").returncode != 0, (
        "LIVE unexpectedly exposes the PUBLIC actuate spelling (brand-rename not as expected)"
    )


# ------------------------------ 14b-6/7/8: M1 forward-ported subcommand parity --------------------
#
# The M1 engine sync (slices S1-S4) forward-ported four LIVE anti-stall subcommands. These slices
# close the loop: each ported subcommand is BEHAVIOURALLY replayed cross-engine on synthetic clean
# seeds (not just surface-pinned above):
#   * 14b-6 pause/resume TRUE parity — identical pause envelope + post-state (set_at is volatile),
#     an identical refusal of `continue` while paused with ZERO state mutation, and an identical
#     resume envelope + post-state with the pause block gone.
#   * 14b-7 dispatch-stall-check READ-ONLY parity — identical stalled and not-stalled envelopes on
#     the same caller-passed watermark, with the state file byte-unchanged on both engines (the
#     engine never persists the watermark; the caller owns it).
#   * 14b-8 retire-dead-sessions DRY-RUN parity — both engines classify an identical fabricated
#     sessions directory (one dead-stale worker, one runtime-alive worker) identically, with both
#     the canonical state and the session files byte-unchanged (dry-run is read-only). The
#     fabricated tmux names cannot exist locally, so the classification is deterministic whether or
#     not a live tmux server is present (no-server -> snapshot None -> both engines fail closed to
#     the SAME skip reason; server present -> both classify the dead session RETIRE).


def test_pause_resume_true_parity(tmp_path):
    """14b-6: P5 operator pause/resume behaves identically cross-engine — envelope, post-state,
    the paused refusal of a side-effectful projection command, and the resume round-trip."""
    _require_live_state()
    pub, live = _seed_b_cross_engine_copies(tmp_path, "pauseB")
    for engine, root in (("public", pub), ("live", live)):
        pf = write_proposal(root, SEED_PROPOSAL, name="pauseB-seed.json")
        r = run_engine(engine, root, "apply", "--root", str(root), "--proposal", str(pf))
        assert r.returncode == 0 and json.loads(r.stdout)["decision"] == "accepted", (
            engine, r.stdout[:300], r.stderr[:200],
        )

    # pause (orchestrator caller): identical decision-bearing envelope + post-state.
    rp = run_engine("public", pub, "pause", "--root", str(pub), "--reason", "maintenance window",
                    "--set-by", "operator", env_overrides=_ORCHESTRATOR_ENV)
    rl = run_engine("live", live, "pause", "--root", str(live), "--reason", "maintenance window",
                    "--set-by", "operator", env_overrides=_ORCHESTRATOR_ENV)
    assert rp.returncode == rl.returncode == 0, (rp.returncode, rl.returncode, rp.stderr, rl.stderr)
    dp, dl = json.loads(rp.stdout), json.loads(rl.stdout)
    assert dp["result"] == dl["result"] == "operator_paused", (dp, dl)  # non-vacuous pin
    assert normalize(dp, roots=(pub, live)) == normalize(dl, roots=(pub, live)), (dp, dl)
    sp, sl = read_state(pub), read_state(live)
    assert sp["operator_pause"]["paused"] is True and sl["operator_pause"]["paused"] is True
    assert normalize(sp, roots=(pub, live)) == normalize(sl, roots=(pub, live)), (
        "paused post-state diverged cross-engine"
    )

    # While paused, a side-effectful projection command refuses IDENTICALLY with zero mutation.
    pre = (_sha256(state_path(pub)), _sha256(state_path(live)))
    cp = run_engine("public", pub, "continue", "--root", str(pub), env_overrides=_ORCHESTRATOR_ENV)
    cl = run_engine("live", live, "continue", "--root", str(live), env_overrides=_ORCHESTRATOR_ENV)
    assert cp.returncode == cl.returncode != 0, (cp.returncode, cl.returncode, cp.stdout, cl.stdout)
    np_, nl = json.loads(cp.stdout), json.loads(cl.stdout)
    assert np_["result"] == nl["result"] == "operator_paused", (np_, nl)  # the refusal IS the pause
    assert normalize(np_, roots=(pub, live)) == normalize(nl, roots=(pub, live)), (np_, nl)
    assert (_sha256(state_path(pub)), _sha256(state_path(live))) == pre, (
        "a paused continue mutated canonical state"
    )

    # resume: identical envelope + post-state, pause block gone on both engines.
    rp2 = run_engine("public", pub, "resume", "--root", str(pub), env_overrides=_ORCHESTRATOR_ENV)
    rl2 = run_engine("live", live, "resume", "--root", str(live), env_overrides=_ORCHESTRATOR_ENV)
    assert rp2.returncode == rl2.returncode == 0, (rp2.returncode, rl2.returncode, rp2.stderr, rl2.stderr)
    ep, el = json.loads(rp2.stdout), json.loads(rl2.stdout)
    assert ep["result"] == el["result"] == "operator_resumed" and ep["was_paused"] is el["was_paused"] is True
    assert normalize(ep, roots=(pub, live)) == normalize(el, roots=(pub, live)), (ep, el)
    sp2, sl2 = read_state(pub), read_state(live)
    assert "operator_pause" not in sp2 and "operator_pause" not in sl2
    assert normalize(sp2, roots=(pub, live)) == normalize(sl2, roots=(pub, live)), (
        "resumed post-state diverged cross-engine"
    )


def test_dispatch_stall_check_readonly_parity(tmp_path):
    """14b-7: V1 dispatch-stall-check produces identical stalled / not-stalled envelopes on the
    same caller-passed watermark and NEVER writes canonical state on either engine."""
    _require_live_state()
    pub, live = _seed_b_cross_engine_copies(tmp_path, "stallB")
    for engine, root in (("public", pub), ("live", live)):
        pf = write_proposal(root, SEED_PROPOSAL, name="stallB-seed.json")
        r = run_engine(engine, root, "apply", "--root", str(root), "--proposal", str(pf))
        assert r.returncode == 0 and json.loads(r.stdout)["decision"] == "accepted", (
            engine, r.stdout[:300], r.stderr[:200],
        )
    pre = (_sha256(state_path(pub)), _sha256(state_path(live)))

    # Stalled shape: the ready candidate persisted across >= threshold checks with no advance.
    args = ("--last-seen-revision", "1", "--no-advance-count", "2", "--threshold-checks", "3")
    rp = run_engine("public", pub, "dispatch-stall-check", "--root", str(pub), *args)
    rl = run_engine("live", live, "dispatch-stall-check", "--root", str(live), *args)
    assert rp.returncode == rl.returncode, (rp.returncode, rl.returncode, rp.stdout, rl.stdout)
    dp, dl = json.loads(rp.stdout), json.loads(rl.stdout)
    assert dp["dispatch_stalled"] is True and dl["dispatch_stalled"] is True, (dp, dl)  # non-vacuous
    assert normalize(dp, roots=(pub, live)) == normalize(dl, roots=(pub, live)), (dp, dl)

    # Not-stalled shape: a fresh watermark (the revision just advanced) resets the count.
    args = ("--last-seen-revision", "0", "--no-advance-count", "2", "--threshold-checks", "3")
    rp2 = run_engine("public", pub, "dispatch-stall-check", "--root", str(pub), *args)
    rl2 = run_engine("live", live, "dispatch-stall-check", "--root", str(live), *args)
    assert rp2.returncode == rl2.returncode, (rp2.returncode, rl2.returncode, rp2.stdout, rl2.stdout)
    np_, nl = json.loads(rp2.stdout), json.loads(rl2.stdout)
    assert np_["dispatch_stalled"] is False and nl["dispatch_stalled"] is False, (np_, nl)
    assert normalize(np_, roots=(pub, live)) == normalize(nl, roots=(pub, live)), (np_, nl)

    # READ-ONLY: the engine never persists the watermark — canonical state is byte-unchanged.
    assert (_sha256(state_path(pub)), _sha256(state_path(live))) == pre, (
        "dispatch-stall-check mutated canonical state (it must be read-only)"
    )


def test_retire_dead_sessions_dry_run_parity(tmp_path):
    """14b-8: V2 retire-dead-sessions dry-run classifies an identical fabricated sessions layout
    identically cross-engine and mutates NOTHING (neither canonical state nor session files)."""
    _require_live_state()
    pub, live = _seed_b_cross_engine_copies(tmp_path, "reapB")
    for engine, root in (("public", pub), ("live", live)):
        pf = write_proposal(root, SEED_PROPOSAL, name="reapB-seed.json")
        r = run_engine(engine, root, "apply", "--root", str(root), "--proposal", str(pf))
        assert r.returncode == 0, (engine, r.stdout[:300], r.stderr[:200])

    # One SHARED fabricated sessions dir (dry-run is read-only, so sharing cannot cross-contaminate):
    # a dead-stale retire candidate + a runtime-alive sibling. The tmux names cannot exist locally.
    old_ts = "2000-01-01T00:00:00.000Z"
    sessions = tmp_path / "reap-projects" / "example-project" / "sessions"
    sessions.mkdir(parents=True)

    def _session(name: str, runtime_state: str) -> dict:
        return {
            "worktree": f"/tmp/wt/{name}",
            "branch": f"session/{name}",
            "tmuxName": name,
            "agent": "claude-code",
            "userPrompt": "Execute the current next_required_action now.",
            "lifecycle": {
                "version": 2,
                "session": {
                    "kind": "worker", "state": "detecting", "reason": "runtime_lost",
                    "startedAt": old_ts, "completedAt": None, "terminatedAt": None,
                    "lastTransitionAt": old_ts,
                },
                "pr": {"state": "none", "reason": "not_created", "number": None, "url": None},
                "runtime": {
                    "state": runtime_state, "reason": "tmux_missing",
                    "lastObservedAt": old_ts, "tmuxName": name,
                },
            },
        }

    (sessions / "example-orch-1-9991.json").write_text(
        json.dumps(_session("example-orch-1-9991", "missing")), encoding="utf-8"
    )
    (sessions / "example-orch-1-9992.json").write_text(
        json.dumps(_session("example-orch-1-9992", "attached")), encoding="utf-8"
    )
    env = {"AO_PROJECTS_ROOT": str(tmp_path / "reap-projects")}
    pre_state = (_sha256(state_path(pub)), _sha256(state_path(live)))
    pre_sessions = tuple(_sha256(p) for p in sorted(sessions.glob("*.json")))

    rp = run_engine("public", pub, "retire-dead-sessions", "--root", str(pub),
                    "--project-id", "example-project", env_overrides=env)
    rl = run_engine("live", live, "retire-dead-sessions", "--root", str(live),
                    "--project-id", "example-project", env_overrides=env)
    assert rp.returncode == rl.returncode == 0, (rp.returncode, rl.returncode, rp.stderr, rl.stderr)
    dp, dl = json.loads(rp.stdout), json.loads(rl.stdout)
    assert dp == dl, f"retire-dead-sessions dry-run plan diverged:\nPUB ={dp}\nLIVE={dl}"
    # Non-vacuous: both engines actually scanned the two fabricated sessions, and the runtime-alive
    # sibling is NEVER a retire candidate on either engine (whatever the tmux snapshot state).
    assert dp["result"] == "retire_plan" and dp["scanned"] == 2, dp
    assert "example-orch-1-9992" not in dp["retire"], dp

    # DRY-RUN: nothing mutated — canonical state AND every session file byte-unchanged.
    assert (_sha256(state_path(pub)), _sha256(state_path(live))) == pre_state
    assert tuple(_sha256(p) for p in sorted(sessions.glob("*.json"))) == pre_sessions
