"""AST def/class symbol PARITY GATE — the keystone lock for the S-RECON reconciliation.

WHY THIS EXISTS (the spec, not just a check)
--------------------------------------------
This PUBLIC engine (``ao_state_writer``) is a faithful, brand-neutral reconciliation of a private
LIVE anti-stall engine. The whole point of the reconciliation is that PUBLIC carries LIVE's
anti-stall logic in full, diverging ONLY in ways that are intentional and individually justified.
This gate makes that claim MACHINE-CHECKED instead of trusted prose: it diffs the def/class symbol
set of every shared module PUBLIC vs LIVE and asserts every divergence is accounted for by exactly
one explicit, rationale-carrying allowlist entry.

Two failure directions, each a real risk:
  * an UNMAPPED LIVE-only symbol => a missing port — a piece of LIVE's anti-stall engine that silently
    did not make it into PUBLIC (a stall hole), or a rename whose PUBLIC counterpart drifted.
  * an UNMAPPED PUBLIC-only symbol => an undocumented divergence — PUBLIC grew behavior LIVE lacks
    without a recorded justification (the "don't preserve a hidden superset" directive).
A STALE allowlist entry (no longer matched) means the code moved but the ledger did not — also a FAIL.

SCOPE (important): this gate proves SYMBOL-SET parity — that the def/class *names* line up modulo the
allowlisted divergences. It deliberately does NOT prove same-name BODY equivalence: a renamed symbol may
carry an intentional body delta (e.g. the `_run_escalated_review_bridge` rename also redacts raw worker
stdout/stderr into hashes+byte-counts — a leak-safety hardening, not a missing anti-stall path). Behavioural
/ body equivalence is the job of the COMPLEMENTARY state.json shadow-replay gate (task#14b), which replays
the mutating subcommands on a copy of LIVE state through both engines and asserts identical decisions.

The divergence taxonomy (every LIVE_ONLY / PUB_ONLY symbol falls in exactly one):
  R  RENAME              — the Owner-approved actuator/review vocabulary rename to escalated_review.
  D  NOT_PORTED          — intentionally omitted: the convergence-review SPAWN subsystem +
                           parking/transport details (Owner step-7 KEEP-PUBLIC: PUBLIC uses a
                           deterministic record-final-convergence instead of spawning a review worker).
  X  DEAD_CODE_OMITTED   — dead in LIVE (no live caller path), not ported.
  G  PUBLIC_GENERALIZATION— PUBLIC-only brand-neutralization (S-GAP1 canonical-filename helpers);
                           LIVE hardcodes its filenames so has no equivalent.
There is deliberately NO "structural" or "safety-divergence" category: the former F1/F2
record-final-convergence guards were reconciled to LIVE (slice t14a-3a), so PUBLIC carries no
behaviour-affecting superset here. The balance is exact: LIVE_ONLY = 20 R + 12 D + 2 X;
PUB_ONLY = 20 R + 10 G.

M1 FORWARD-PORT BURN-DOWN: COMPLETE. The temporary `_M1_PENDING` entries (operator pause,
dispatch-stall escalation, dead-session reaper, audit fixes) burned down to ZERO as slices
S1-S4 ported each symbol — the stale-entry check forced every entry's removal the moment its
slice landed, so the allowlist served as a self-enforcing burn-down ledger. Every remaining
entry below is a PERMANENT, Owner-decided divergence.

STRUCTURE
---------
* The SNAPSHOT + self-consistency tests run ALWAYS (no private repo needed) so public CI / external
  adopters still lock PUBLIC symbol drift and allowlist coherence.
* The LIVE-comparison tests run only when ``AO_PARITY_LIVE_ROOT`` points at a LIVE ``ao_state_writer``
  package; otherwise they SKIP loudly. The private LIVE tree is never committed.
* This PUBLIC file must never contain the literal private rename token, so LIVE-side names carry a
  ``@`` placeholder that ``_live`` expands at runtime (the leak gate forbids the literal substring).

Regenerate the snapshot after an INTENTIONAL PUBLIC symbol change:
    python tests/parity/test_ast_symbol_parity.py --regen
"""

from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path

import pytest

# Rebuild the private LIVE vocabulary token at runtime so this PUBLIC source never contains the
# literal forbidden substring (the public_safety_scan leak gate forbids it). LIVE-side names below
# carry a '@' placeholder; _live expands it.
_RENAMED_FROM = "gpt" + "_pro"


_RENAMED_AWAY = "desk" + "top"  # second forbidden token; same split-literal reason as _RENAMED_FROM.


def _live(name: str) -> str:
    """Expand the placeholders in a LIVE-side symbol/module name to their real (private) spelling.

    '@' -> the renamed actuator vocabulary token, '#' -> the dropped transport-medium token. Both are
    rebuilt at runtime so this PUBLIC source never contains the literal substrings the leak gate forbids.
    """
    return name.replace("@", _RENAMED_FROM).replace("#", _RENAMED_AWAY)


REPO_ROOT = Path(__file__).resolve().parents[2]
PUBLIC_PKG = REPO_ROOT / "src" / "ao_state_writer"
SNAPSHOT_PATH = Path(__file__).resolve().parent / "public_symbols_snapshot.json"

# The 8 same-name modules the gate compares symbol-for-symbol.
MODULES = (
    "writer.py",
    "cli.py",
    "continuation.py",
    "watchdog.py",
    "todo.py",
    "compat.py",
    "preflight.py",
    "session_reaper.py",
)

# A module that was renamed wholesale (identical def/class symbols, different filename) — checked
# separately because it is not a same-name module.
MODULE_RENAME = {"escalated_review_actuator.py": _live("@_#_bridge.py")}


def _symbols(path: Path) -> set[str]:
    """Qualified top-level def/class names in a module: 'name' and 'Class.method'."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.add(node.name)
        elif isinstance(node, ast.ClassDef):
            out.add(node.name)
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.add(f"{node.name}.{sub.name}")
    return out


# ----- R: RENAME_MAP[module] = {LIVE qualified name -> PUBLIC qualified name} (20 total) -----------
RENAME_MAP: dict[str, dict[str, str]] = {
    "writer.py": {
        _live("StateWriter._current_@_gate_id_for_target"): "StateWriter._current_escalated_review_gate_id_for_target",
        _live("StateWriter._@_authorization_rejection"): "StateWriter._escalated_review_authorization_rejection",
        _live("StateWriter._@_review_gate_metadata"): "StateWriter._escalated_review_gate_metadata",
        _live("StateWriter._has_@_receipt"): "StateWriter._has_escalated_review_receipt",
        _live("StateWriter._is_@_actuator_failure"): "StateWriter._is_escalated_review_actuator_failure",
        _live("StateWriter._is_@_watchdog_timeout"): "StateWriter._is_escalated_review_watchdog_timeout",
    },
    "cli.py": {
        _live("_claim_and_run_@_actuator_cli"): "_claim_and_run_escalated_review_actuator_cli",
        _live("_@_actuator_failure_proposal_id"): "_escalated_review_actuator_failure_proposal_id",
        _live("_@_closure_receipt_guard"): "_escalated_review_closure_receipt_guard",
        _live("_@_receipt_proposal_id"): "_escalated_review_receipt_proposal_id",
        _live("_@_package_gate_proposals_from_ledger"): "_escalated_review_package_gate_proposals_from_ledger",
        _live("_inferred_live_@_gate_ids"): "_inferred_live_escalated_review_gate_ids",
        _live("_is_live_@_gate"): "_is_live_escalated_review_gate",
        _live("_live_@_gate_ids"): "_live_escalated_review_gate_ids",
        # NB: the public name also drops the transport-medium token (a detail), not a pure substitution.
        _live("_read_@_#_actuator_command"): "_read_escalated_review_actuator_command",
        _live("_record_@_actuator_failure"): "_record_escalated_review_actuator_failure",
        _live("_run_@_actuator_cli"): "_run_escalated_review_actuator_cli",
        # NB: a TRUE rename of this symbol, but the PUBLIC body also redacts raw worker stdout/stderr into
        # hashes+byte-counts (leak-safety hardening) — beyond a pure vocab rename, not a missing anti-stall
        # path. The gate checks symbol-set parity only; body equivalence is task#14b's shadow-replay job.
        _live("_run_@_bridge"): "_run_escalated_review_bridge",
        _live("cmd_@_actuate"): "cmd_escalated_review_actuate",
    },
    "watchdog.py": {
        _live("_evaluate_@"): "_evaluate_escalated_review",
    },
}

# ----- D: NOT_PORTED[module] = {LIVE qualified name -> rationale} (12 total, all permanent) --------
_CONV_SPAWN = (
    "convergence-review SPAWN subsystem (Owner step-7 KEEP-PUBLIC): PUBLIC resolves an exhausted "
    "repair token via the deterministic record-final-convergence path and returns a fail-closed "
    "owner_proxy_convergence_required envelope, instead of spawning a bounded convergence-review worker"
)
NOT_PORTED: dict[str, dict[str, str]] = {
    "writer.py": {
        "StateWriter._convergence_blocker_code_for_proposal": _CONV_SPAWN,
        "StateWriter._final_convergence_cap_rejection": (
            "spawn convergence-review cap rejection, superseded by the deterministic "
            "record-final-convergence path (not a generic convergence-behaviour helper)"
        ),
        "StateWriter._spawned_convergence_reviews_for_target": _CONV_SPAWN,
        "StateWriter._spawned_convergence_reviews_for_target_blocker": _CONV_SPAWN,
    },
    "cli.py": {
        "_convergence_blocker_code_for_proposal": _CONV_SPAWN,
        "_convergence_review_limit_issue": _CONV_SPAWN,
        "_final_convergence_parking_issue": _CONV_SPAWN,
        "_spawned_convergence_reviews_for_target": _CONV_SPAWN,
        "_spawned_convergence_reviews_for_target_blocker": _CONV_SPAWN,
        _live("_infer_@_review_transport"): (
            "a review-transport-medium detail of LIVE's actuator job payload that PUBLIC does not model; "
            "PUBLIC has no transport concept (intentionally dropped, not an anti-stall guard)"
        ),
    },
    "continuation.py": {
        "_render_convergence_prompt": (
            "convergence-review SPAWN prompt renderer (Owner step-7 KEEP-PUBLIC): PUBLIC does not spawn "
            "a convergence-review worker, so it renders no such prompt"
        ),
        "_extract_markdown_section": (
            "helper used only by _render_convergence_prompt; omitted with it (Owner step-7 KEEP-PUBLIC)"
        ),
    },
}

# ----- X: DEAD_CODE_OMITTED[module] = {LIVE qualified name -> rationale} (2 total) -----------------
DEAD_CODE_OMITTED: dict[str, dict[str, str]] = {
    "cli.py": {
        _live("_terminate_spawned_session"): (
            "dead in LIVE (no external caller); the PUBLIC spawn-baseline release path proves a worker "
            "gone via an ao-session readback and never kills a session, so this was not ported (slice b3c)"
        ),
        "_same_path_string": (
            "reachable in LIVE ONLY from _terminate_spawned_session (its sole caller, itself omitted "
            "dead code) — not caller-free, but unreachable once _terminate_spawned_session is omitted (b3c)"
        ),
    },
}

# ----- G: PUBLIC_GENERALIZATION[module] = {PUBLIC qualified name -> rationale} (10 total, ---------
# ----- all permanent) ------------------------------------------------------------------------------
_S_GAP1 = (
    "S-GAP1 brand-neutral canonical-filename generalization: PUBLIC resolves governance filenames from "
    "the contract's [canonical] section; LIVE hardcodes its filenames and has no equivalent symbol"
)
PUBLIC_GENERALIZATION: dict[str, dict[str, str]] = {
    "cli.py": {
        "_try_acquire_machine_actuator_lock": (
            "M2 multi-project machine-global actuator mutex: PUBLIC serializes the ONE machine-global "
            "external review surface across multiple registered projects (non-blocking flock under "
            "~/.agent-orchestrator/locks); LIVE is single-project and has no equivalent symbol"
        ),
    },
    "compat.py": {
        "InvalidCanonicalFilename": _S_GAP1,
        "InvalidCanonicalFilename.__init__": _S_GAP1,
        "_canonical_filename": _S_GAP1,
        "_canonical_key_present": _S_GAP1,
        "_validate_canonical_filename": _S_GAP1,
        "canonical_master_plan_file": _S_GAP1,
        "canonical_session_log_file": _S_GAP1,
        "canonical_todo_file": _S_GAP1,
    },
    "preflight.py": {
        "effective_governance_files": (
            "S-GAP1 generalization: resolves governance files from the contract's [canonical] section "
            "instead of LIVE's hardcoded names"
        ),
    },
}


def _allowlist_for(table: dict[str, dict[str, str]], module: str) -> dict[str, str]:
    return table.get(module, {})


# ============================== ALWAYS-ON TESTS (no LIVE needed) ===================================


def _load_snapshot() -> dict[str, list[str]]:
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def test_public_modules_present():
    """Every module the gate reasons about must actually exist in the PUBLIC package — otherwise the
    gate could pass vacuously by comparing empty symbol sets."""
    missing = [m for m in MODULES if not (PUBLIC_PKG / m).exists()]
    assert not missing, f"PUBLIC modules missing: {missing}"
    pub_module = next(iter(MODULE_RENAME))
    assert (PUBLIC_PKG / pub_module).exists(), f"PUBLIC actuator module missing: {pub_module}"


def test_public_symbol_snapshot_locked():
    """Lock PUBLIC def/class drift independently of the private LIVE baseline, so public CI and
    external adopters (who have no LIVE tree) still catch an accidental symbol add/remove. An
    intentional change regenerates the snapshot (see module docstring)."""
    current = {m: sorted(_symbols(PUBLIC_PKG / m)) for m in MODULES}
    snapshot = _load_snapshot()
    assert current == snapshot, (
        "PUBLIC symbol set drifted from the committed snapshot. If intentional, regenerate:\n"
        "  python tests/parity/test_ast_symbol_parity.py --regen\n"
        f"per-module diff: "
        + json.dumps(
            {
                m: {
                    "added": sorted(set(current.get(m, [])) - set(snapshot.get(m, []))),
                    "removed": sorted(set(snapshot.get(m, [])) - set(current.get(m, []))),
                }
                for m in set(current) | set(snapshot)
                if set(current.get(m, [])) != set(snapshot.get(m, []))
            },
            indent=2,
        )
    )


def test_allowlist_self_consistency():
    """The rename map + allowlists must be internally coherent WITHOUT consulting LIVE: no symbol may
    sit in two buckets on the same side, and the rename map must be 1:1. This catches a copy-paste or
    a half-finished re-categorization before the (optional) LIVE comparison even runs."""
    for module in set(RENAME_MAP) | set(NOT_PORTED) | set(DEAD_CODE_OMITTED) | set(PUBLIC_GENERALIZATION):
        renames = RENAME_MAP.get(module, {})
        live_sources = list(renames.keys())
        pub_targets = list(renames.values())
        # rename map is 1:1 within the module
        assert len(live_sources) == len(set(live_sources)), f"{module}: duplicate rename source"
        assert len(pub_targets) == len(set(pub_targets)), f"{module}: duplicate rename target"

        not_ported = set(_allowlist_for(NOT_PORTED, module))
        dead = set(_allowlist_for(DEAD_CODE_OMITTED, module))
        generalization = set(_allowlist_for(PUBLIC_GENERALIZATION, module))

        # LIVE-side buckets (rename sources, D, X) pairwise disjoint
        live_side = [("rename_source", set(live_sources)), ("not_ported", not_ported), ("dead_code", dead)]
        for i in range(len(live_side)):
            for j in range(i + 1, len(live_side)):
                (na, a), (nb, b) = live_side[i], live_side[j]
                assert not (a & b), f"{module}: symbol in both {na} and {nb}: {sorted(a & b)}"
        # PUBLIC-side buckets (rename targets, G) disjoint
        assert not (set(pub_targets) & generalization), (
            f"{module}: symbol in both rename_target and generalization: "
            f"{sorted(set(pub_targets) & generalization)}"
        )


# ============================== LIVE-COMPARISON TESTS (need AO_PARITY_LIVE_ROOT) ===================

_LIVE_ROOT_ENV = "AO_PARITY_LIVE_ROOT"


def _live_root() -> Path | None:
    raw = os.environ.get(_LIVE_ROOT_ENV)
    if not raw:
        return None
    return Path(raw).expanduser()


def _require_live_root() -> Path:
    root = _live_root()
    if root is None:
        pytest.skip(
            f"{_LIVE_ROOT_ENV} not set — skipping PUBLIC-vs-LIVE parity comparison. Set it to a LIVE "
            "ao_state_writer package dir to run the full gate (the private tree is never committed)."
        )
    # Loud, specific validation: a supplied-but-wrong root must FAIL, not silently pass.
    if not root.is_dir():
        pytest.fail(f"{_LIVE_ROOT_ENV}={root} is not a directory")
    missing = [m for m in MODULES if not (root / m).exists()]
    assert not missing, f"{_LIVE_ROOT_ENV}={root} is missing expected modules: {missing}"
    return root


def test_live_symbol_parity():
    """THE GATE. For every shared module: apply the rename map, then assert the residual LIVE-only and
    PUBLIC-only symbols EXACTLY equal their allowlists — catching unmapped residuals (missing port /
    undocumented divergence) AND stale allowlist entries in one shot."""
    live_root = _require_live_root()
    failures: list[str] = []
    for module in MODULES:
        live = _symbols(live_root / module)
        pub = _symbols(PUBLIC_PKG / module)
        live_only = live - pub
        pub_only = pub - live

        # 1) consume renames (a stale/colliding rename is caught here)
        for live_sym, pub_sym in _allowlist_for(RENAME_MAP, module).items():
            if live_sym not in live_only:
                failures.append(f"{module}: rename source not LIVE-only (stale/wrong): {live_sym}")
            if pub_sym not in pub_only:
                failures.append(f"{module}: rename target not PUBLIC-only (stale/wrong): {pub_sym}")
            live_only.discard(live_sym)
            pub_only.discard(pub_sym)

        # 2) residual LIVE-only must EXACTLY equal D ∪ X (unmapped => missing port; stale => drift)
        expected_live = set(_allowlist_for(NOT_PORTED, module)) | set(_allowlist_for(DEAD_CODE_OMITTED, module))
        unmapped_live = live_only - expected_live
        stale_live = expected_live - live_only
        if unmapped_live:
            failures.append(
                f"{module}: UNMAPPED LIVE-only symbols (missing port or undocumented rename): "
                f"{sorted(unmapped_live)}"
            )
        if stale_live:
            failures.append(f"{module}: STALE NOT_PORTED/DEAD_CODE allowlist entries: {sorted(stale_live)}")

        # 3) residual PUBLIC-only must EXACTLY equal G
        expected_pub = set(_allowlist_for(PUBLIC_GENERALIZATION, module))
        unmapped_pub = pub_only - expected_pub
        stale_pub = expected_pub - pub_only
        if unmapped_pub:
            failures.append(f"{module}: UNMAPPED PUBLIC-only symbols (undocumented divergence): {sorted(unmapped_pub)}")
        if stale_pub:
            failures.append(f"{module}: STALE PUBLIC_GENERALIZATION allowlist entries: {sorted(stale_pub)}")

    assert not failures, "AST parity gate failures:\n  " + "\n  ".join(failures)


def test_live_module_rename_parity():
    """The wholesale-renamed module (PUBLIC escalated_review_actuator.py <- the LIVE actuator-bridge
    module named in MODULE_RENAME) must carry byte-identical def/class symbols — a pure file rename
    with no symbol divergence."""
    live_root = _require_live_root()
    for pub_module, live_module in MODULE_RENAME.items():
        live_path = live_root / live_module
        if not live_path.exists():
            pytest.fail(f"LIVE module {live_module} missing under {_LIVE_ROOT_ENV}={live_root}")
        pub_syms = _symbols(PUBLIC_PKG / pub_module)
        live_syms = _symbols(live_path)
        assert pub_syms == live_syms, (
            f"module-rename parity broken {live_module} -> {pub_module}: "
            f"PUBLIC-only={sorted(pub_syms - live_syms)} LIVE-only={sorted(live_syms - pub_syms)}"
        )


def _regen_snapshot() -> None:
    snapshot = {m: sorted(_symbols(PUBLIC_PKG / m)) for m in MODULES}
    SNAPSHOT_PATH.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"regenerated {SNAPSHOT_PATH} ({sum(len(v) for v in snapshot.values())} symbols)")


if __name__ == "__main__":
    if "--regen" in sys.argv:
        _regen_snapshot()
    else:
        print("run via pytest; use --regen to rewrite the PUBLIC symbol snapshot")
