"""Dead-session retirement (state-writer maintenance reconcile).

WHY this exists
---------------
AO (`@aoagents/ao` orchestrator) has no automatic garbage collection for
*non-merged* sessions. Its only auto-teardown is ``maybeAutoCleanupOnMerge``
(ao-core ``lifecycle-manager.js``), hard-gated on ``status === MERGED``. Every
other terminal-ish state — ``needs_input`` / ``stuck`` / ``detecting`` with a
lost runtime — is left on disk as a session JSON forever, and the dashboard
keeps it in its "needs attention" column while the report-watcher re-audits it
on every poll (observed ``reportWatcherTriggerCount`` climbing into the
thousands). Because each dispatched slice mints a strictly-monotonic new session
and the only reap path is MERGE (a tiny fraction of slices), the dead set grows
without bound.

This module retires *provably dead* worker sessions by writing the EXACT
terminal lifecycle AO itself writes in ``sessionManager.kill()`` for a lost
runtime (``session.state="terminated"``, ``reason="runtime_lost"``). AO's own
``deriveLegacyStatus`` maps that to the terminal status ``killed``, so on AO's
next poll the report-watcher stops re-auditing it and the dashboard retires the
card — AO cannot distinguish a reaper-retired session from one it killed.

WHAT this deliberately does NOT do
----------------------------------
- It issues NO runtime command / poke / dispatch / ``ao`` subprocess /
  ``tmux send-keys``. It reads a read-only ``tmux ls`` snapshot and reads/writes
  session JSON. AO discovers the change passively on its own next poll.
- It NEVER deletes a session JSON or removes a worktree (auditable history is
  preserved; worktree/RAM reclamation stays a separate, human-gated path).
- It NEVER touches runtime-ALIVE sessions (those holding live panes / RAM): a
  safe predicate for live-pane reclamation needs canonical target-closure proof
  and is intentionally out of scope here.

Concurrency safety (the single biggest risk, per codex cc review)
-----------------------------------------------------------------
AO writes session JSON through a per-file lock (``withFileLockSync`` over
``<path>.lock``) plus an atomic temp+rename. This module replicates that exact
lock protocol, and inside the lock it RE-READS and RE-CLASSIFIES the file before
writing, and refuses to write if the file changed since the dry-run snapshot
(``metadata_changed_during_gc``). That prevents clobbering a fresh agent report,
PR discovery, restore, counter bump, or lifecycle transition AO wrote in the
meantime.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

# --- verdict constants -------------------------------------------------------
RETIRE = "retire"
SKIP_NO_LIFECYCLE = "skip_no_lifecycle"
SKIP_UNSUPPORTED_LIFECYCLE = "skip_unsupported_lifecycle_version"
SKIP_ORCHESTRATOR = "skip_orchestrator_kind"
SKIP_ALREADY_TERMINAL = "skip_already_terminal"
SKIP_RUNTIME_NOT_MISSING = "skip_runtime_not_missing"
SKIP_MISSING_STALENESS_EVIDENCE = "skip_missing_staleness_evidence"
SKIP_NOT_STALE_ENOUGH = "skip_not_stale_enough"
SKIP_PR_VALUE_PRESENT = "skip_pr_value_present"
SKIP_TMUX_NAME_UNKNOWN = "skip_tmux_name_unknown"
SKIP_TMUX_SNAPSHOT_UNAVAILABLE = "skip_tmux_snapshot_unavailable"
SKIP_TMUX_ALIVE = "skip_tmux_alive"
SKIP_CHANGED_DURING_GC = "skip_metadata_changed_during_gc"

# Terminal canonical session states (mirror ao-core lifecycle-state.js
# TERMINAL_CANONICAL_SESSION_STATES). A session already in one of these is left
# untouched — AO already considers it done.
_TERMINAL_SESSION_STATES = frozenset({"terminated", "done"})

# Runtime states that prove the runtime is gone. ``missing`` (tmux_missing /
# runtime_lost) and ``exited`` are the dead-runtime markers AO sets.
_DEAD_RUNTIME_STATES = frozenset({"missing", "exited"})

# PR lifecycle states that carry value we must never retire over.
_PR_VALUE_STATES = frozenset({"open", "merged"})

# Stale "detecting" probe markers cleared on terminal transition (parity with
# AO's clearTerminalMarkers behaviour). Popped from session/runtime if present.
_DETECTING_MARKERS = ("detectingAttempts", "detectingStartedAt", "detectingEvidenceHash")

DEFAULT_GRACE_SECONDS = 900  # 15 min, comfortably > AO's 5-min report freshness
DEFAULT_LOCK_TIMEOUT_MS = 10_000
DEFAULT_LOCK_STALE_MS = 60_000
AUDIT_FILENAME = ".session-reaper-audit.jsonl"


# --- timestamp helpers -------------------------------------------------------
def _parse_iso_epoch(raw: object) -> float | None:
    """Parse an ISO-8601 timestamp (``...Z`` or offset) to epoch seconds."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def now_iso() -> str:
    """Current UTC time as an AO-style ISO string with trailing ``Z``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
    )


# --- classification (pure; fail-closed AND of every guard) -------------------
def classify(
    payload: object,
    *,
    now_epoch: float,
    grace_seconds: float,
    live_tmux: set[str] | None,
) -> str:
    """Decide whether one session payload is a retire candidate.

    Returns :data:`RETIRE` only when EVERY guard passes; otherwise a specific
    ``skip_*`` reason. ``live_tmux`` is the set of live tmux session names, or
    ``None`` when the snapshot could not be taken (fail-closed: skip all).
    """
    if live_tmux is None:
        return SKIP_TMUX_SNAPSHOT_UNAVAILABLE
    if not isinstance(payload, dict):
        return SKIP_NO_LIFECYCLE
    lifecycle = payload.get("lifecycle")
    if not isinstance(lifecycle, dict):
        return SKIP_NO_LIFECYCLE
    if lifecycle.get("version") != 2:
        return SKIP_UNSUPPORTED_LIFECYCLE
    session = lifecycle.get("session")
    runtime = lifecycle.get("runtime")
    pr = lifecycle.get("pr")
    if not isinstance(session, dict) or not isinstance(runtime, dict):
        return SKIP_NO_LIFECYCLE

    # Never touch the orchestrator session.
    if session.get("kind") == "orchestrator":
        return SKIP_ORCHESTRATOR

    # Already terminal → AO already retired it.
    if session.get("state") in _TERMINAL_SESSION_STATES:
        return SKIP_ALREADY_TERMINAL

    # Runtime must be provably gone.
    if runtime.get("state") not in _DEAD_RUNTIME_STATES:
        return SKIP_RUNTIME_NOT_MISSING

    # Staleness floor: runtime.lastObservedAt AND session.lastTransitionAt are
    # REQUIRED and must be older than the grace window; agentReportedAt is
    # checked only when present. ``missing`` alone is too eager (codex #1).
    required_ts = [
        _parse_iso_epoch(runtime.get("lastObservedAt")),
        _parse_iso_epoch(session.get("lastTransitionAt")),
    ]
    if any(ts is None for ts in required_ts):
        return SKIP_MISSING_STALENESS_EVIDENCE
    optional_reported = payload.get("agentReportedAt")
    if optional_reported is not None:
        reported_epoch = _parse_iso_epoch(optional_reported)
        if reported_epoch is not None:
            required_ts.append(reported_epoch)
    if any((now_epoch - ts) < grace_seconds for ts in required_ts):  # type: ignore[operator]
        return SKIP_NOT_STALE_ENOUGH

    # PR preservation (codex #3): never retire a session with ANY PR value —
    # not just lifecycle.pr.state, but any url/number evidence.
    if isinstance(pr, dict):
        if pr.get("state") in _PR_VALUE_STATES:
            return SKIP_PR_VALUE_PRESENT
        if pr.get("url") or pr.get("number"):
            return SKIP_PR_VALUE_PRESENT

    # tmux evidence (codex #4): require a concrete tmuxName, absent from the
    # live snapshot. Missing/malformed name → skip by default.
    tmux_name = runtime.get("tmuxName") or payload.get("tmuxName")
    if not isinstance(tmux_name, str) or not tmux_name.strip():
        return SKIP_TMUX_NAME_UNKNOWN
    if tmux_name.strip() in live_tmux:
        return SKIP_TMUX_ALIVE

    return RETIRE


# --- terminal mutation (mirror sessionManager.kill for a lost runtime) -------
def apply_terminal_lifecycle(payload: dict[str, Any], *, stamp: str) -> None:
    """Mutate ``payload['lifecycle']`` in place into AO's lost-runtime terminal
    shape. Only lifecycle fields are touched; every other key is preserved."""
    lifecycle = payload["lifecycle"]
    session = lifecycle["session"]
    runtime = lifecycle["runtime"]

    session["state"] = "terminated"
    session["reason"] = "runtime_lost"
    session["terminatedAt"] = stamp
    session["lastTransitionAt"] = stamp

    # Keep runtime missing; preserve its reason (e.g. tmux_missing). Stamp the
    # reaper observation time.
    if runtime.get("state") not in _DEAD_RUNTIME_STATES:
        runtime["state"] = "missing"
    runtime["lastObservedAt"] = stamp

    # Clear stale detecting probe markers if present (parity with AO terminal
    # transition; no-op when absent).
    for container in (session, runtime):
        for marker in _DETECTING_MARKERS:
            container.pop(marker, None)


# --- AO-compatible file lock (mirror ao-core file-lock.js withFileLockSync) ---
@contextmanager
def file_lock(
    lock_path: Path,
    *,
    timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
    stale_ms: int = DEFAULT_LOCK_STALE_MS,
) -> Iterator[None]:
    """Acquire ``<path>.lock`` with the same O_EXCL + staleness + backoff
    protocol AO uses, so reaper writes interleave safely with AO's writes."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() * 1000 + timeout_ms
    wait_ms = 10
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                mtime_ms = os.stat(lock_path).st_mtime * 1000
                if time.time() * 1000 - mtime_ms > stale_ms:
                    os.remove(lock_path)
                    continue
            except FileNotFoundError:
                continue
            if time.time() * 1000 > deadline:
                raise TimeoutError(f"Timed out waiting for file lock: {lock_path}")
            time.sleep(wait_ms / 1000.0)
            wait_ms = min(wait_ms * 2, 250)
    try:
        yield
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.remove(lock_path)
        except OSError:
            pass


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomic temp+rename write matching AO's serializeMetadata (2-space JSON,
    UTF-8, trailing newline). POSIX rename is atomic."""
    content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{int(time.time() * 1000)}.tmp")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


# --- live tmux snapshot (read-only; NOT a poke) ------------------------------
def live_tmux_names() -> set[str] | None:
    """Return the set of live tmux session names, or ``None`` if the snapshot
    cannot be taken (caller must then fail closed)."""
    try:
        proc = subprocess.run(
            ["tmux", "ls", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        # `tmux ls` exits non-zero when there is NO server. That genuinely means
        # zero live sessions — but it is indistinguishable from a transient
        # failure, so we fail closed and skip this pass.
        return None
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


# --- iteration helpers -------------------------------------------------------
def _session_files(sessions_dir: Path) -> list[Path]:
    if not sessions_dir.is_dir():
        return []
    return sorted(p for p in sessions_dir.glob("*.json") if p.is_file())


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


# --- public entry points -----------------------------------------------------
def plan(
    sessions_dir: Path,
    *,
    now_epoch: float,
    grace_seconds: float,
    live_tmux: set[str] | None,
) -> dict[str, Any]:
    """Dry-run: classify every session, returning the decision breakdown."""
    decisions: dict[str, str] = {}
    retire: list[str] = []
    skipped: dict[str, list[str]] = {}
    for path in _session_files(sessions_dir):
        sid = path.stem
        payload = _load_json(path)
        verdict = SKIP_NO_LIFECYCLE if payload is None else classify(
            payload,
            now_epoch=now_epoch,
            grace_seconds=grace_seconds,
            live_tmux=live_tmux,
        )
        decisions[sid] = verdict
        if verdict == RETIRE:
            retire.append(sid)
        else:
            skipped.setdefault(verdict, []).append(sid)
    return {
        "result": "retire_plan",
        "scanned": len(decisions),
        "retire_count": len(retire),
        "retire": retire,
        "skipped": skipped,
        "tmux_snapshot": "unavailable" if live_tmux is None else "ok",
    }


def apply_retirements(
    sessions_dir: Path,
    *,
    now_epoch: float,
    stamp: str,
    grace_seconds: float,
    live_tmux: set[str] | None,
    audit_path: Path | None = None,
) -> dict[str, Any]:
    """Apply terminal-lifecycle retirement to every retire candidate, under the
    AO-compatible lock with an in-lock re-read + re-classify + CAS guard."""
    applied: list[str] = []
    changed_during: list[str] = []
    skipped: dict[str, list[str]] = {}
    audit_rows: list[dict[str, Any]] = []

    for path in _session_files(sessions_dir):
        sid = path.stem
        pre = _load_json(path)
        verdict = SKIP_NO_LIFECYCLE if pre is None else classify(
            pre, now_epoch=now_epoch, grace_seconds=grace_seconds, live_tmux=live_tmux
        )
        if verdict != RETIRE:
            skipped.setdefault(verdict, []).append(sid)
            continue
        pre_digest = _digest(path)

        lock_path = path.with_name(path.name + ".lock")
        with file_lock(lock_path):
            # Re-read and re-classify INSIDE the lock; refuse to write if the
            # file changed since the dry-run snapshot.
            if _digest(path) != pre_digest:
                changed_during.append(sid)
                skipped.setdefault(SKIP_CHANGED_DURING_GC, []).append(sid)
                continue
            fresh = _load_json(path)
            if fresh is None:
                skipped.setdefault(SKIP_NO_LIFECYCLE, []).append(sid)
                continue
            reverdict = classify(
                fresh, now_epoch=now_epoch, grace_seconds=grace_seconds, live_tmux=live_tmux
            )
            if reverdict != RETIRE:
                skipped.setdefault(reverdict, []).append(sid)
                continue
            old_session = dict(fresh["lifecycle"]["session"])
            old_runtime = dict(fresh["lifecycle"]["runtime"])
            apply_terminal_lifecycle(fresh, stamp=stamp)
            _atomic_write_json(path, fresh)
            applied.append(sid)
            audit_rows.append(
                {
                    "ts": stamp,
                    "session_id": sid,
                    "action": "retire",
                    "old_session_state": old_session.get("state"),
                    "old_session_reason": old_session.get("reason"),
                    "old_runtime_state": old_runtime.get("state"),
                    "old_runtime_reason": old_runtime.get("reason"),
                    "new_session_state": "terminated",
                    "new_session_reason": "runtime_lost",
                    "tmux_live": False,
                    "decided_by": "state_writer_session_reaper",
                }
            )

    if audit_path is not None and audit_rows:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with audit_path.open("a", encoding="utf-8") as handle:
            for row in audit_rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    return {
        "result": "retire_applied",
        "applied_count": len(applied),
        "applied": applied,
        "changed_during_gc": changed_during,
        "skipped": skipped,
        "tmux_snapshot": "unavailable" if live_tmux is None else "ok",
    }
