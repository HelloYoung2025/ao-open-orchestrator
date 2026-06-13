#!/usr/bin/env bash
# orchestrator-liveness.sh — operator-run sidecar via `nohup`; NOT part of the AO process.
#
# Usage:
#   nohup bash .../orchestrator-liveness.sh > ~/.agent-orchestrator/orchestrator-liveness.log 2>&1 &
#
# This script polls for the orchestrator tmux session and revives/pokes it when:
#   (a) the orchestrator session is absent, AND
#   (b) at least one live worker session exists OR canonical state still has ready/gated obligations.
# When the orchestrator session is present and appears healthy but canonical obligations remain,
# the sidecar sends a reconcile-sweep poke without restarting the session. This covers dropped
# edge-triggered AO_DISPATCH_POKE delivery while preserving the orchestrator as the sole dispatcher.
#
# ✓ LIVE-VERIFIED (2026-05-30): `ao start <projectId>` on a running daemon REUSES the
#   daemon (PID unchanged) and recreates the absent orchestrator tmux session from the
#   authored orchestrator-prompt file. Revival latency ~9s. The revive call stays GUARDED
#   (failure logged, not fatal) as defence-in-depth.
#
# A freshly-revived orchestrator agent idles until it receives input, so revival ALONE
# does NOT auto-run the reconcile sweep — a slice stranded while the orchestrator was down
# (e.g. its worker reported needs_input into a dropped poke) would never be swept. This
# sidecar therefore sends an explicit post-revival reconcile-sweep poke so stranded slices
# are recovered from canonical state.
#
# REQUIRED env vars (no defaults — the sidecar fails loudly if any is unset; the launchd plist
# template ships them via EnvironmentVariables):
#   ORCHESTRATOR_SESSION — tmux session name to watch (e.g. "<your-prefix>-orchestrator")
#   PROJECT_ID           — AO project id passed to `ao start`
#   ACTIVE_ROOT          — canonical ao-state-writer state root (passed to the engine as --root)
#
# Tunable env vars (override before launching):
#   INTERVAL_S          — poll interval in seconds (default: 180)
#   COOLDOWN_S          — minimum seconds between revive attempts (default: 180)
#   MAX_CONSECUTIVE_FAILURES — consecutive failed revives before a backoff pause (default: 5; the
#                              sidecar then backs off BACKOFF_S and keeps trying — it never gives up)
#   BACKOFF_S               — pause after MAX_CONSECUTIVE_FAILURES failed revives, then retry (default: 600)
#   STATE_WRITER_CMD        — engine invocation (default: the installed "ao-state-writer" console
#                             script). MACHINERY is decoupled from ACTIVE_ROOT: the state dir is passed
#                             as --root, the engine command is this. For a source (non-pip) install,
#                             override e.g. STATE_WRITER_CMD='PYTHONPATH=/path/to/src python3 -m ao_state_writer.cli'.
#   PYTHON_BIN              — interpreter for the inline JSON classifiers (default: python3)
#   LOG_FILE                — log destination (default: ~/.agent-orchestrator/orchestrator-liveness.$PROJECT_ID.log)
#   PIDFILE                 — singleton-guard pidfile (default: ~/.agent-orchestrator/orchestrator-liveness.$PROJECT_ID.pid;
#                             per-project, NOT machine-global — N projects' sidecars run side by side)
#   SESSIONS_DIR            — AO sessions dir (default: ~/.agent-orchestrator/projects/$PROJECT_ID/sessions)
#   POKE_ESCAPE_SETTLE_S / POKE_BOOT_DELAY_S — poke timing overrides for tests only
#   LIVENESS_MAX_TICKS     — optional test-only loop bound (unset in production)

set -u

# Fail loud on missing required identity/state config (a misconfigured sidecar must not silently
# poke the wrong session or guess a state root).
: "${ORCHESTRATOR_SESSION:?ORCHESTRATOR_SESSION must be set (tmux orchestrator session to watch)}"
: "${PROJECT_ID:?PROJECT_ID must be set (AO project id for \`ao start\`)}"
: "${ACTIVE_ROOT:?ACTIVE_ROOT must be set (canonical ao-state-writer state root)}"

INTERVAL_S="${INTERVAL_S:-180}"
COOLDOWN_S="${COOLDOWN_S:-180}"
MAX_CONSECUTIVE_FAILURES="${MAX_CONSECUTIVE_FAILURES:-5}"
BACKOFF_S="${BACKOFF_S:-600}"
# Engine command (MACHINERY) — independent of ACTIVE_ROOT (the project state dir). Defaults to the
# installed console script; an operator-owned shell fragment is honoured via run_state_writer().
STATE_WRITER_CMD="${STATE_WRITER_CMD:-ao-state-writer}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SESSIONS_DIR="${SESSIONS_DIR:-${HOME}/.agent-orchestrator/projects/${PROJECT_ID}/sessions}"
# Per-project default (multi-project: N sidecars share one HOME; a shared log would interleave
# two daemons' lines). The canonical plist passes LOG_FILE explicitly with the same derivation.
LOG_FILE="${LOG_FILE:-${HOME}/.agent-orchestrator/orchestrator-liveness.${PROJECT_ID}.log}"

# Ensure log directory exists
mkdir -p "$(dirname "$LOG_FILE")"

# --- singleton guard (prevent concurrent sidecar instances; portable, no flock dependency) ---
# Identity-checked (2026-06-05): a bare `kill -0 <pid>` only proves the pid is ALIVE, not that it is
# THIS sidecar. Under launchd KeepAlive={SuccessfulExit:false} a false positive is terminal — a stale
# pidfile whose pid was recycled onto an unrelated live process would make us exit 0, and launchd
# treats exit 0 as success and does NOT relaunch, leaving no sidecar running. So we also require the
# live pid's command line to name this script before honoring the pidfile; otherwise we treat the
# pidfile as stale (dead pid, empty, or a recycled non-sidecar pid) and claim it.
# The pidfile is PER-PROJECT (multi-project: this guard protects against two instances of THIS
# project's sidecar; a machine-global pidfile would make project B's sidecar mistake project A's
# live sidecar for itself — same script name — and exit 0, which launchd treats as terminal).
PIDFILE="${PIDFILE:-${HOME}/.agent-orchestrator/orchestrator-liveness.${PROJECT_ID}.pid}"
existing_pid="$(cat "$PIDFILE" 2>/dev/null || true)"
if [ -n "$existing_pid" ] && kill -0 "$existing_pid" 2>/dev/null \
   && ps -ww -p "$existing_pid" -o command= 2>/dev/null | grep -q "orchestrator-liveness.sh"; then
  echo "orchestrator-liveness: another instance (pid ${existing_pid}) is already running — exiting." >&2
  exit 0
fi
echo "$$" > "$PIDFILE"
trap 'rm -f "$PIDFILE"' EXIT

log() {
  local ts
  ts="$(date '+%Y-%m-%dT%H:%M:%S%z')"
  # Write once to the log file. The canonical launch redirects stdout to LOG_FILE too,
  # so a `tee -a "$LOG_FILE"` here would double-log every line (was mis-read as concurrent
  # sidecar instances). File-only write keeps the log single-sourced.
  echo "${ts}  $*" >> "$LOG_FILE"
}

run_state_writer() {
  # Execute the configured engine command (STATE_WRITER_CMD) with the trailing subcommand/args
  # SAFELY QUOTED, so a state root containing spaces survives. STATE_WRITER_CMD is an operator-owned
  # shell fragment (default: the "ao-state-writer" console script; or e.g.
  # 'PYTHONPATH=/path/to/src python3 -m ao_state_writer.cli'), so it is eval'd — a bare
  # "$STATE_WRITER_CMD" call could not parse a leading `VAR=value` env prefix or multi-token command.
  local quoted="" arg
  for arg in "$@"; do quoted+=" $(printf '%q' "$arg")"; done
  eval "${STATE_WRITER_CMD}${quoted}"
}

last_revive_ts=0
last_stuck_poke_ts=0
consecutive_failures=0
# AO-001 (2026-06-06, codex-reviewed PASS-WITH-CHANGES): nudge-once fingerprints so a persistently
# fail-closed exit-3 obligation no longer drives an inert 180s poke loop (was: 109 inert re-pokes).
# last_nudge_sig is EPOCH-AWARE — it is cleared whenever the blocker disappears (actionable / none /
# self-inflicted / unavailable), so a blocker that reappears AFTER being resolved still earns one
# fresh nudge. CANON_CATEGORY/CANON_SIGNATURE are set by canonical_obligations_present for the caller.
last_nudge_sig=""
last_selfinflict_sig=""
CANON_CATEGORY=""
CANON_SIGNATURE=""

log "orchestrator-liveness started. orchestrator=${ORCHESTRATOR_SESSION} project=${PROJECT_ID} interval=${INTERVAL_S}s cooldown=${COOLDOWN_S}s max_failures=${MAX_CONSECUTIVE_FAILURES}"
# Log resolved command paths at startup: launchd's PATH is minimal and NOT the user
# shell's — an unresolvable command here silently blinds a whole detection leg (a missing
# tmux made a live sidecar misjudge "orchestrator absent", 2026-06-12). One line per tool
# makes version/install drift auditable from the log alone.
for _cmd in tmux ao "${PYTHON_BIN}"; do
  log "resolve ${_cmd} -> $(command -v "${_cmd}" 2>/dev/null || echo MISSING)"
done
log "resolve state-writer (${STATE_WRITER_CMD%% *}) -> $(command -v "${STATE_WRITER_CMD%% *}" 2>/dev/null || echo MISSING-or-shell-fragment)"
if [ ! -d "$SESSIONS_DIR" ]; then
  log "WARN  AO sessions dir not found (${SESSIONS_DIR}); present-but-stuck detection will be unavailable until this path exists or SESSIONS_DIR is set."
fi

json_has_candidates() {
  "$PYTHON_BIN" - "$1" <<'PY'
import json
import sys

try:
    payload = json.loads(sys.argv[1])
except Exception as exc:
    print(f"invalid candidate JSON: {exc}", file=sys.stderr)
    sys.exit(2)

sys.exit(0 if payload.get("candidates") else 1)
PY
}

classify_canonical_payload() {
  # AO-001: map a list-ready/list-gated payload to a sidecar action category. Only
  # preflight_reconcile() results are reachable from these read commands, so this set is CLOSED.
  #   actionable         -> real tier-1/tier-2 work; poke per cooldown (UNCHANGED behavior)
  #   none               -> ready/gated with empty candidates; nothing to do
  #   governance_blocked -> working-tree governance dirty; an LLM commit can clear it -> nudge once
  #   stranded           -> a KNOWN orchestrator-repairable non-dispatch obligation -> nudge once
  #   no_poke            -> self-inflicted (wrong root/schema/contract/proof) OR ANY UNKNOWN exit-3
  #                         result -> degrade CLOSED, never poke (the orchestrator LLM cannot resolve it)
  "$PYTHON_BIN" - "$1" <<'PY'
import json
import sys

try:
    p = json.loads(sys.argv[1])
except Exception as exc:
    print(f"parse_error: {exc}", file=sys.stderr)
    sys.exit(2)

result = p.get("result")
if result in ("ready", "gated"):
    print("actionable" if p.get("candidates") else "none")
    sys.exit(0)
if result == "blocked":
    reasons = p.get("reasons") or []
    print("governance_blocked\tblocked:" + ",".join(sorted(map(str, reasons))))
    sys.exit(0)

# KNOWN orchestrator-repairable non-dispatch obligations (each has an LLM-runnable resolver).
# Anything NOT in this allowlist degrades CLOSED via the final no_poke branch (codex review (e)).
STRANDED_REPAIRABLE = {
    "spawned_review_timeout_due",
    "owner_proxy_convergence_required",
    "spawn_attestation_reconciliation_required",
    "spawned_dispatch_liveness_reconcile_required",
    "spawned_dispatch_time_reconciliation_required",
}
if result in STRANDED_REPAIRABLE:
    sig = "|".join(
        str(p.get(k) or "")
        for k in ("result", "proposal_id", "target_id", "review_scope", "next_required_action")
    )
    print("stranded\t" + sig)
    sys.exit(0)

# self-inflicted env/root/schema/contract/proof AND any unknown exit-3 result -> never poke.
print("no_poke\t" + str(result))
sys.exit(0)
PY
}

classify_nonzero_payload() {
  # AO-001: classify a NON-ZERO (exit-3) list-ready/list-gated payload and decide whether the
  # orchestrator should be poked. Sets the GLOBALS CANON_CATEGORY/CANON_SIGNATURE for the caller.
  # Returns 0 = poke-worthy (actionable/governance_blocked/stranded), 1 = do-not-poke
  # (none / self-inflicted / unknown -> degrade closed), 2 = UNAVAILABLE (empty/parse-fail).
  local out="$1" label="$2" parsed rc
  if [ -z "$out" ]; then
    log "WARN  canonical ${label} check UNAVAILABLE (empty output; CLI could not run, e.g. TCC)."
    return 2
  fi
  parsed="$(classify_canonical_payload "$out" 2>>"$LOG_FILE")"; rc=$?
  if [ "$rc" -eq 2 ]; then
    log "WARN  canonical ${label} check UNAVAILABLE (unparseable structured output); output=${out}"
    return 2
  fi
  CANON_CATEGORY="${parsed%%$'\t'*}"
  CANON_SIGNATURE="${parsed#*$'\t'}"
  [ "$CANON_SIGNATURE" = "$parsed" ] && CANON_SIGNATURE=""
  case "$CANON_CATEGORY" in
    actionable|governance_blocked|stranded)
      log "INFO  canonical ${label} ${CANON_CATEGORY} (sig=${CANON_SIGNATURE:-n/a}); orchestrator action expected."
      return 0
      ;;
    no_poke)
      if [ "$CANON_SIGNATURE" != "$last_selfinflict_sig" ]; then
        last_selfinflict_sig="$CANON_SIGNATURE"
        log "ERROR canonical ${label} non-pokeable preflight envelope (result=${CANON_SIGNATURE}); NOT poking — orchestrator LLM cannot resolve it (fix sidecar/env/root/schema/contract, or wait for a real obligation)."
      fi
      return 1
      ;;
    *)
      # none, or any unexpected classifier token -> degrade closed (do not poke).
      return 1
      ;;
  esac
}

session_unhealthy_reason() {
  "$PYTHON_BIN" - "$1" <<'PY'
import json
import re
import sys
from datetime import datetime, timezone

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        payload = json.load(handle)
except Exception as exc:
    print(f"invalid session JSON: {exc}", file=sys.stderr)
    sys.exit(2)

session = payload.get("lifecycle", {}).get("session", {})
runtime = payload.get("lifecycle", {}).get("runtime", {})
reason = session.get("reason") or payload.get("reason")
top_status = payload.get("status")
session_state = session.get("state")
runtime_state = runtime.get("state")
evidence = payload.get("lifecycleEvidence") or ""

def parse_ts(value):
    if not value or not isinstance(value, str):
        return None
    candidate = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed

def latest_lifecycle_activity_at():
    candidates = [
        runtime.get("lastObservedAt"),
        session.get("lastTransitionAt"),
        payload.get("restoredAt"),
    ]
    parsed = [ts for ts in (parse_ts(candidate) for candidate in candidates) if ts]
    return max(parsed) if parsed else None

def idle_evidence_at():
    match = re.search(r"\bat=([0-9T:+\-\.Z]+)", evidence)
    return parse_ts(match.group(1)) if match else None

if session_state == "stuck" and reason == "probe_failure":
    print("probe_failure")
    sys.exit(0)
if top_status == "stuck" and runtime_state and runtime_state != "alive":
    print("runtime_not_alive")
    sys.exit(0)
if "idle_beyond_threshold" in evidence:
    idle_at = idle_evidence_at()
    latest_activity_at = latest_lifecycle_activity_at()
    if idle_at and latest_activity_at and idle_at > latest_activity_at:
        print("idle_beyond_threshold")
        sys.exit(0)
sys.exit(1)
PY
}

canonical_obligations_present() {
  # Three-state (2026-06-05, codex-endorsed degrade-CLOSED): the canonical ao-state-writer state
  # lives under ACTIVE_ROOT, which is in ~/Documents — UNREADABLE by a launchd-spawned process under
  # macOS TCC. Earlier this failed OPEN (return 0 = "obligations present"), which under TCC would
  # poke a HEALTHY orchestrator every cooldown forever (burning orchestrator LLM turns). Now:
  #   return 0 = obligations confirmed present
  #   return 1 = confirmed none
  #   return 2 = UNAVAILABLE (state unreadable / module import / CLI / parse failure)
  # Callers degrade CLOSED on 2: do NOT poke a healthy orchestrator on an unverified guess. A
  # HARD-failed orchestrator (probe_failure / runtime_not_alive) is still restarted from the
  # TCC-free session-health signal alone (see the present-unhealthy branch).
  local ready_out gated_out ready_has gated_has ready_status gated_status
  # AO-001: a NON-ZERO exit is an exit-3 preflight envelope. Classify it by its `result` field and
  # poke ONLY for genuinely-pokeable categories; a governance/stranded blocker is nudged at most once
  # (handled at the poke site below), and self-inflicted/unknown envelopes are degraded CLOSED. This
  # replaces the old "any parseable non-empty structured result == obligations present" rule, which
  # misclassified every fail-closed exit-3 envelope as work and drove an inert 180s poke loop.
  ready_out="$(run_state_writer list-ready --root "$ACTIVE_ROOT" 2>>"$LOG_FILE")" || {
    classify_nonzero_payload "$ready_out" "ready"; return $?
  }
  gated_out="$(run_state_writer list-gated --root "$ACTIVE_ROOT" 2>>"$LOG_FILE")" || {
    classify_nonzero_payload "$gated_out" "gated"; return $?
  }
  ready_has=0
  gated_has=0
  if json_has_candidates "$ready_out" 2>>"$LOG_FILE"; then
    ready_has=1
  else
    ready_status=$?
    if [ "$ready_status" -eq 2 ]; then
      log "WARN  canonical ready JSON UNAVAILABLE (parse failed)."
      return 2
    fi
  fi
  if json_has_candidates "$gated_out" 2>>"$LOG_FILE"; then
    gated_has=1
  else
    gated_status=$?
    if [ "$gated_status" -eq 2 ]; then
      log "WARN  canonical gated JSON UNAVAILABLE (parse failed)."
      return 2
    fi
  fi
  if [ "$ready_has" = "1" ] || [ "$gated_has" = "1" ]; then
    CANON_CATEGORY="actionable"
    CANON_SIGNATURE=""
    log "INFO  canonical obligations present. ready=${ready_out} gated=${gated_out}"
    return 0
  fi
  CANON_CATEGORY="none"
  CANON_SIGNATURE=""
  return 1
}

# CURE-2: deterministic orphaned-lease janitor. Each tick, before any revive/poke, delete
# provably-orphaned fast-AO-spawn 'pending' dispatch leases — those left when the claimer
# (usually the orchestrator LLM session) was SIGKILLed between claim_dispatch and confirm_dispatch.
# Such a lease otherwise only gets reclaimed by a re-dispatch (which spawns a worker), so a stopped
# orchestrator leaves it wedged. Clearing it lets a revived/poked orchestrator find a clean lease
# table. This is the SINGLE narrowly-scoped exception to "the sidecar does not modify canonical
# state": reconcile-leases only deletes a provably-dead FAST AO-spawn orphan (its effective action
# is in the _is_spawned_dispatch_reconcile_action whitelist), never an escalated-review /
# codex_cc_review actuator/review lease (those legitimately hold pending up to the 7200s bridge
# timeout), and it grants the sidecar no dispatch authority. Fail-safe: any error is logged and the
# tick continues.
reclaim_orphaned_leases() {
  local out rc
  out="$(run_state_writer reconcile-leases --root "$ACTIVE_ROOT" --apply 2>>"$LOG_FILE")"
  rc=$?
  if [ "$rc" -ne 0 ]; then
    log "WARN  reconcile-leases janitor exited rc=${rc}; continuing tick."
    return 0
  fi
  case "$out" in
    *'"reclaimed": []'*) : ;;  # nothing orphaned this tick — stay quiet
    *) log "OK    reconcile-leases reclaimed orphaned lease(s): ${out}" ;;
  esac
  return 0
}

send_reconcile_poke() {
  local reason="$1"
  # The state-writer compatibility boundary rejects worker worktrees as --root. Keep the
  # wake message pinned to the canonical active root; worker_session is only liveness context.
  local poke_msg state_writer_cmd root_q
  # The engine command is the operator-owned STATE_WRITER_CMD fragment, interpolated as instruction
  # text. ACTIVE_ROOT is DATA, so it is shell-quoted everywhere it appears as a --root argument that
  # the orchestrator will paste into a shell.
  state_writer_cmd="${STATE_WRITER_CMD}"
  root_q="$(printf '%q' "$ACTIVE_ROOT")"
  poke_msg="AO_DISPATCH_POKE reconcile-sweep (${reason}): enumerate live workers via 'ao session ls -p ${PROJECT_ID}' for liveness only, then reconcile-from-state using canonical active_root ${ACTIVE_ROOT} — run ${state_writer_cmd} list-ready --root ${root_q} and ${state_writer_cmd} list-gated --root ${root_q}; dispatch every tier-1 ready pid via ${state_writer_cmd} dispatch --root ${root_q} --proposal-id <pid>; if tier-2 gated: run ${state_writer_cmd} authorize --root ${root_q} --proposal-id <pid> --evidence <ref>, then immediately run ${state_writer_cmd} dispatch --root ${root_q} --proposal-id <pid> for the same pid in this sweep. Never use a worker worktree as --root."
  local poke_ok=0
  local attempt send_out send_rc
  if tmux send-keys -t "$ORCHESTRATOR_SESSION" Escape >> "$LOG_FILE" 2>&1; then
    log "OK    sent Escape before reconcile-sweep poke to clear compact/busy UI."
  else
    log "WARN  failed to send Escape before reconcile-sweep poke; continuing with ao send."
  fi
  # Boot/settle delays are env-overridable so the test harness can neutralize them
  # (same intent as INTERVAL_S/COOLDOWN_S=0); prod defaults are unchanged (1s settle, 12s boot).
  sleep "${POKE_ESCAPE_SETTLE_S:-1}"
  for attempt in 1 2 3; do
    sleep "${POKE_BOOT_DELAY_S:-12}"
    send_out="$(ao send "$ORCHESTRATOR_SESSION" "$poke_msg" 2>&1)"
    send_rc=$?
    printf '%s\n' "$send_out" >> "$LOG_FILE"
    if [ "$send_rc" -eq 0 ] && ! printf '%s' "$send_out" | grep -qiE "could not|not received|unable|no such session|no session|not found|timed out"; then
      log "OK    reconcile-sweep poke delivered for ${reason} (attempt ${attempt})."
      poke_ok=1
      break
    fi
    log "WARN  reconcile-sweep poke for ${reason} attempt ${attempt} not confirmed (rc=${send_rc}); retrying."
  done
  [ "$poke_ok" -eq 1 ] || log "ERROR reconcile-sweep poke for ${reason} FAILED after 3 attempts; stranded slices may need a manual reconcile sweep."
}

liveness_tick=0
while true; do
  sleep "$INTERVAL_S"
  # LIVENESS_MAX_TICKS bounds the loop for deterministic testing (default: unset = run forever).
  if [ -n "${LIVENESS_MAX_TICKS:-}" ]; then
    liveness_tick=$(( liveness_tick + 1 ))
    [ "$liveness_tick" -gt "$LIVENESS_MAX_TICKS" ] && break
  fi

  # CURE-2: clear provably-orphaned fast-AO-spawn dispatch leases first, before any revive/poke,
  # so a revived/poked orchestrator never re-stalls on a dead lease. Idempotent + fail-safe.
  reclaim_orphaned_leases

  # --- check orchestrator presence ---
  if tmux has-session -t "$ORCHESTRATOR_SESSION" 2>/dev/null; then
    # The tmux session exists, but AO may still have marked it unhealthy/stuck. A
    # present-but-unhealthy orchestrator will not be revived by the "missing session" path.
    # Restart/poke it when live workers or canonical obligations prove work remains,
    # without adding another dispatcher.
    session_json="${SESSIONS_DIR}/${ORCHESTRATOR_SESSION}.json"
    if [ -f "$session_json" ]; then
      session_health_status=0
      session_health_reason="$(session_unhealthy_reason "$session_json" 2>>"$LOG_FILE")"
      session_health_status=$?
      if [ "$session_health_status" -eq 2 ]; then
        log "WARN  session JSON parse failed for ${session_json}; sending no stuck-poke this tick."
      fi
    else
      session_health_status=1
      session_health_reason=""
    fi
    if [ "$session_health_status" -eq 0 ]; then
      live_workers="$(tmux ls 2>/dev/null | grep "^${ORCHESTRATOR_SESSION%-orchestrator}-" | grep -v -- "-orchestrator" || true)"
      stuck_recovery_reason=""
      canonical_obligations_present; obl_rc=$?
      if [ -n "$live_workers" ]; then
        stuck_recovery_reason="live-workers"
      elif [ "$obl_rc" -eq 0 ]; then
        stuck_recovery_reason="canonical-obligations"
      elif [ "$obl_rc" -eq 2 ]; then
        # Canonical state UNREADABLE (e.g. ~/Documents blocked by TCC under launchd) and no live
        # workers. Degrade CLOSED on the obligation guess, but a HARD-failed orchestrator
        # (probe_failure / runtime_not_alive) is an unambiguous "broken" signal on its own — restart
        # it on the TCC-free session-health alone. This is the dominant recovery case and covers a
        # probe_failure orchestrator with a stranded slice we cannot enumerate.
        case "$session_health_reason" in
          probe_failure|runtime_not_alive) stuck_recovery_reason="unhealthy-session-state-unreadable" ;;
        esac
      fi
      now_ts="$(date '+%s')"
      elapsed=$(( now_ts - last_stuck_poke_ts ))
      if [ -n "$stuck_recovery_reason" ] && [ "$elapsed" -ge "$COOLDOWN_S" ]; then
        log "WARN  orchestrator session '${ORCHESTRATOR_SESSION}' present but unhealthy (${session_health_reason}) with ${stuck_recovery_reason}; restarting orchestrator runtime before reconcile-sweep poke."
        last_stuck_poke_ts="$now_ts"
        last_revive_ts="$now_ts"
        if tmux kill-session -t "$ORCHESTRATOR_SESSION" >> "$LOG_FILE" 2>&1; then
          log "OK    killed stuck orchestrator tmux session '${ORCHESTRATOR_SESSION}' (workers untouched)."
        else
          stuck_kill_exit=$?
          log "WARN  failed to kill stuck orchestrator tmux session '${ORCHESTRATOR_SESSION}' (exit ${stuck_kill_exit}); trying ao start anyway."
        fi
        if ao start "$PROJECT_ID" >> "$LOG_FILE" 2>&1; then
          log "OK    present-stuck guarded restart succeeded for project ${PROJECT_ID}."
        else
          stuck_revive_exit=$?
          log "WARN  present-stuck guarded restart failed (exit ${stuck_revive_exit}); sending reconcile-sweep poke anyway."
        fi
        send_reconcile_poke "present-stuck-orchestrator"
        consecutive_failures=0
        continue
      elif [ -n "$stuck_recovery_reason" ]; then
        remaining=$(( COOLDOWN_S - elapsed ))
        log "INFO  orchestrator present but unhealthy (${session_health_reason}) with ${stuck_recovery_reason}; cooldown active — ${remaining}s remaining before restart."
        consecutive_failures=0
        continue
      fi
    fi
    if canonical_obligations_present; then
      now_ts="$(date '+%s')"
      elapsed=$(( now_ts - last_stuck_poke_ts ))
      # AO-001 nudge-once: a governance_blocked / stranded obligation has NO deterministic executor,
      # so poke it at most ONCE per distinct signature instead of every cooldown (was: 109 inert
      # re-pokes). Real actionable ready/gated work is NOT a stuck blocker and still pokes per cooldown
      # (and clears the nudge epoch so a later blocker earns a fresh nudge).
      if [ "$CANON_CATEGORY" = "actionable" ]; then
        last_nudge_sig=""
      elif { [ "$CANON_CATEGORY" = "governance_blocked" ] || [ "$CANON_CATEGORY" = "stranded" ]; } \
           && [ "$CANON_SIGNATURE" = "$last_nudge_sig" ]; then
        log "INFO  orchestrator present/healthy; ${CANON_CATEGORY} (${CANON_SIGNATURE}) already nudged once — suppressing repeat poke (no deterministic executor; avoids inert loop)."
        consecutive_failures=0
        continue
      fi
      if [ "$elapsed" -ge "$COOLDOWN_S" ]; then
        log "INFO  orchestrator session '${ORCHESTRATOR_SESSION}' present and healthy, but canonical obligations remain (${CANON_CATEGORY}); sending reconcile-sweep poke without restart."
        last_stuck_poke_ts="$now_ts"
        if [ "$CANON_CATEGORY" = "governance_blocked" ] || [ "$CANON_CATEGORY" = "stranded" ]; then
          last_nudge_sig="$CANON_SIGNATURE"
        fi
        send_reconcile_poke "present-healthy-obligations"
        consecutive_failures=0
        continue
      fi
      remaining=$(( COOLDOWN_S - elapsed ))
      log "INFO  orchestrator present and healthy with canonical obligations (${CANON_CATEGORY}); cooldown active — ${remaining}s remaining before reconcile-sweep poke."
      consecutive_failures=0
      continue
    fi
    # orchestrator tmux exists but no nudge-worthy obligation this tick (none / self-inflicted /
    # unavailable) — reset revive failure counter AND clear the nudge epoch (AO-001) so a blocker
    # that reappears after being resolved earns one fresh nudge instead of permanent suppression.
    last_nudge_sig=""
    consecutive_failures=0
    continue
  fi

  # --- orchestrator absent: check for live workers ---
  live_workers="$(tmux ls 2>/dev/null | grep "^${ORCHESTRATOR_SESSION%-orchestrator}-" | grep -v -- "-orchestrator" || true)"
  if [ -z "$live_workers" ]; then
    if canonical_obligations_present; then
      log "WARN  orchestrator absent and no live workers found, but canonical obligations are present — reviving for reconcile."
    else
      log "INFO  orchestrator absent, no live workers, and no canonical ready/gated obligations — skipping revive."
      continue
    fi
  else
    log "WARN  orchestrator session '${ORCHESTRATOR_SESSION}' missing; live workers present:"
    echo "$live_workers" | while IFS= read -r line; do log "        $line"; done
  fi

  # --- cooldown check ---
  now_ts="$(date '+%s')"
  elapsed=$(( now_ts - last_revive_ts ))
  if [ "$elapsed" -lt "$COOLDOWN_S" ]; then
    remaining=$(( COOLDOWN_S - elapsed ))
    log "INFO  cooldown active — ${remaining}s remaining; skipping revive attempt."
    continue
  fi

  # --- supervised backoff (NEVER permanently give up) ---
  # Owner decision 2026-06-05: under launchd supervision the backstop must not stop on its own — a
  # permanent give-up is exactly the multi-hour dead-gap this sidecar exists to prevent. (And an
  # exit 1 here would be silently relaunched by KeepAlive={SuccessfulExit:false} anyway, defeating
  # the guard.) So after MAX_CONSECUTIVE_FAILURES consecutive failed revives, log loudly, pause for
  # BACKOFF_S, reset the counter, and keep trying in-process — a bounded retry, not a hammer.
  if [ "$consecutive_failures" -ge "$MAX_CONSECUTIVE_FAILURES" ]; then
    log "ERROR ${consecutive_failures} consecutive revive failures — AO appears wedged. Backing off ${BACKOFF_S}s, then continuing (supervised retry; will NOT give up)."
    sleep "$BACKOFF_S"
    consecutive_failures=0
  fi

  # --- attempt revive (guarded — failure logged, not fatal) ---
  # `ao start <projectId>` reuse-reattach is LIVE-VERIFIED (see header). It runs the AO runtime
  # as a FOREGROUND daemon and NEVER EXITS on success — so it must be launched DETACHED. The old
  # synchronous call (`if ao start ...; then`) parked the patrol loop inside the first successful
  # revive forever: no further ticks, no further sweep pokes, and the success branch below
  # (including the post-revival poke that un-parks a restored orchestrator from its resume
  # prompt) was DEAD CODE reachable only on failure. Production symptom: one morning revive,
  # then a full day of silence with the orchestrator sitting at a resume menu.
  # Success is therefore judged by the orchestrator session APPEARING within a bounded wait —
  # the signal the loop actually cares about — not by an exit code success never produces.
  log "INFO  attempting revive: ao start ${PROJECT_ID} (detached)"
  last_revive_ts="$(date '+%s')"

  nohup ao start "$PROJECT_ID" >> "$LOG_FILE" 2>&1 &
  revive_pid=$!
  revive_ok=0
  for _revive_tick in $(seq 1 "${REVIVE_WAIT_TICKS:-12}"); do
    if tmux has-session -t "$ORCHESTRATOR_SESSION" 2>/dev/null; then
      revive_ok=1
      break
    fi
    # The detached launcher died before the session appeared — a genuine fast failure
    # (busy port, bad config); stop waiting out the full bound.
    kill -0 "$revive_pid" 2>/dev/null || break
    sleep "${REVIVE_WAIT_INTERVAL_S:-5}"
  done

  if [ "$revive_ok" -eq 1 ]; then
    log "OK    revive succeeded for project ${PROJECT_ID} (runtime detached, launcher pid ${revive_pid})."
    consecutive_failures=0
    # A freshly-booted orchestrator idles until poked AND needs >~9s to come up. Send the
    # post-revival reconcile-sweep poke with a boot delay + bounded retry. `ao send` can exit 0
    # even when it "could not confirm" delivery, so treat that wording as non-delivery and retry.
    send_reconcile_poke "post-liveness-revival"
  else
    consecutive_failures=$(( consecutive_failures + 1 ))
    log "ERROR revive failed (orchestrator session absent after bounded wait); consecutive_failures=${consecutive_failures}/${MAX_CONSECUTIVE_FAILURES}."
  fi

done
