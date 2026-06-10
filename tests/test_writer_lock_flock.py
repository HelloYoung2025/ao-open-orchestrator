"""C-FIX-8 regression suite (forward-port slice M1-S4): the single-flight state lock is an
fcntl.flock advisory lock, not an O_EXCL lockfile with manual stale-reclaim.

WHY THIS EXISTS (the spec, not just a check): the previous O_EXCL + rename-by-path stale-reclaim
had a TOCTOU — a waiter that had classified the lock stale would later rename the path, stealing
a lock a SECOND waiter had freshly created in the classify->rename window, admitting two writers
into the critical section -> lost update on canonical state.json (a corrupted state.json hard-
stalls the whole orchestrator). Under fcntl.flock the kernel grants the lock to exactly one open
file description at a time ACROSS processes and releases it AUTOMATICALLY on fd close / process
exit / SIGKILL: a hard kill structurally cannot deadlock the writer, a held lock structurally
cannot be stolen, and the entire manual stale-reclaim (and its TOCTOU) is gone. These tests pin
the flock semantics: a held lock excludes (never steals), release makes it immediately
acquirable, a leftover lockfile from a dead holder is acquired promptly, and concurrent applies
still serialize with the lockfile persisting by design (the path IS the lock namespace).
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time

import pytest

from test_public_core import _writer

import ao_state_writer.writer as ao_writer
from ao_state_writer.writer import (
    CODEX_CC_MODEL,
    CODEX_CC_REASONING_EFFORT,
    StateTransitionProposal,
)


def _read_state(writer) -> dict:
    return json.loads(writer.state_path.read_text(encoding="utf-8"))


def _lock_path(writer):
    # Construct the lock path exactly as StateWriter._single_flight does
    # (state.json -> state.json.lock), and make sure its parent dir exists so a
    # pre-seeded lockfile can be written before the first state write.
    lock_path = writer.state_path.with_suffix(writer.state_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    return lock_path


def test_single_flight_excludes_concurrent_flock_holder(tmp_path, monkeypatch) -> None:
    """The core mutual-exclusion invariant under fcntl.flock. A lock held by ANOTHER open file
    description (a second fd here stands in for another CLI process) blocks this acquirer — it
    is NEVER stolen, so two writers can never both enter _single_flight() and lose an update on
    state.json. Releasing the holder makes the lock immediately acquirable. (The old
    O_EXCL+rename reclaim could be raced into stealing a lock freshly acquired in its
    classify->rename window; a held flock has no such window.) Shrink LOCK_TIMEOUT_SECONDS so
    the contention-deadline assertion stays fast+deterministic."""
    writer = _writer(tmp_path)
    lock_path = _lock_path(writer)
    holder = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(holder, fcntl.LOCK_EX)  # another holder owns the lock
    try:
        monkeypatch.setattr(ao_writer, "LOCK_TIMEOUT_SECONDS", 0.3)
        started = time.monotonic()
        with pytest.raises(FileExistsError):
            writer.claim_dispatch("p")  # blocked by the held flock -> raises at deadline
        assert time.monotonic() - started < 1.0
        monkeypatch.setattr(ao_writer, "LOCK_TIMEOUT_SECONDS", 10.0)
        # The held lock was never stolen and no state was written.
        assert not writer.state_path.exists()
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)
    # With the holder released, the lock is immediately acquirable (no deadlock).
    assert writer.claim_dispatch("p")
    assert _read_state(writer)["dispatched_proposals"]["p"]["status"] == "pending"


def test_single_flight_acquires_leftover_lockfile_after_holder_death(tmp_path) -> None:
    """The state lock is an fcntl.flock advisory lock, which the kernel releases automatically
    when the holder exits or is SIGKILLed. A holder killed mid-hold leaves only the lock FILE
    behind (its flock is already gone), so the next acquirer must lock it IMMEDIATELY — no
    manual stale-reclaim, no deadlock, no contention-timeout hang. The leftover body is
    forensics-only now; flock liveness is structural, not body-classified."""
    writer = _writer(tmp_path)
    lock_path = _lock_path(writer)
    # A leftover lockfile from a dead holder: stale body, NO live flock held on it.
    lock_path.write_text("pid=999999 time=2000-01-01T00:00:00+0000\n", encoding="utf-8")
    started = time.monotonic()
    assert writer.claim_dispatch("p")
    assert time.monotonic() - started < 2.0
    assert _read_state(writer)["dispatched_proposals"]["p"]["status"] == "pending"


def test_concurrent_applies_serialize_via_file_lock(tmp_path, monkeypatch) -> None:
    """Two distinct accepted proposals applied concurrently must both land without clobbering
    each other's state revision: the single-flight file lock serializes them."""
    writer = _writer(tmp_path)
    seed = writer.apply(
        StateTransitionProposal(
            proposal_id="seed",
            target_kind="small_chapter",
            target_id="chapter-1",
            base_state_revision=0,
            requested_state="evidence_pending",
            actor_role="worker",
            evidence_refs=["session-log#chapter-1"],
        )
    )
    assert seed.decision == "accepted"

    results: list[str] = []
    barrier = threading.Barrier(2)

    def apply_blocker(i: int) -> None:
        w = _writer(tmp_path)
        barrier.wait()
        decision = w.apply(
            StateTransitionProposal(
                proposal_id=f"blk{i}",
                target_kind="small_chapter",
                target_id="chapter-1",
                # Both racers see revision 1; the lock serializes them so only the
                # first to acquire is accepted and the loser is rejected stale.
                base_state_revision=1,
                requested_state="closure_candidate",
                actor_role="codex_cc",
                review_scope="codex_cc",
                verdict="blocker",
                blocker_code=f"code{i}",
                model=CODEX_CC_MODEL,
                reasoning_effort=CODEX_CC_REASONING_EFFORT,
                evidence_refs=[f"cc-review#chapter-1-{i}"],
            )
        )
        results.append(decision.decision)

    monkeypatch.setenv("AO_CALLER_TYPE", "codex_cc")
    threads = [threading.Thread(target=apply_blocker, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    monkeypatch.delenv("AO_CALLER_TYPE", raising=False)

    # Exactly one accepted, one rejected (stale) — no lost update, lock held cleanly.
    assert sorted(results) == ["accepted", "rejected"]
    assert _read_state(writer)["state_revision"] == 2
    # Under fcntl.flock the lockfile persists by design (the path IS the lock namespace);
    # the post-condition is that the flock was RELEASED cleanly — i.e. immediately re-acquirable.
    with writer._single_flight():
        pass
