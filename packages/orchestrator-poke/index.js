import { execFile } from "node:child_process";

export const manifest = {
  name: "orchestrator-poke",
  slot: "notifier",
  description: "Poke orchestrator tmux session on worker needs-input",
  version: "0.1.0",
};

const RELAYED_REACTION_KEYS = new Set(["agent-needs-input", "report-needs-input"]);
const RELAYED_EVENT_TYPES   = new Set(["session.needs_input", "report.needs_input"]); // defensive
const DEDUP_WINDOW_MS = 10_000;

// Once-per-PROCESS guard for the missing-config warning (module-scoped, not per create() instance),
// so a misconfigured notifier warns exactly once even if the plugin is instantiated more than once.
let warnedMissingConfig = false;

// Shell-quote a DATA value for safe interpolation into the reconcile instruction the orchestrator
// pastes into a shell. activeRoot is data (it may contain spaces); wrap in single quotes and escape
// any embedded single quote as '\''. The stateWriterCommand is a configured shell FRAGMENT (the
// operator owns its quoting) and is interpolated as-is.
function shellQuote(value) {
  return `'${String(value).replace(/'/g, "'\\''")}'`;
}

function tmuxSend(args) {
  return new Promise((resolve) => {
    execFile("tmux", args, { timeout: 8000 }, (err) => {
      if (err) console.warn(`[orchestrator-poke] tmux ${args.join(" ")} failed: ${err.message}`);
      resolve(); // never throw — a notifier failure must not break the lifecycle poll
    });
  });
}
const delay = (ms) => new Promise((r) => setTimeout(r, ms));

export function create(config) {
  const cfg = config ?? {};
  const sessionPrefix = cfg.sessionPrefix || "";
  const orchestratorSession = cfg.orchestratorSession || (sessionPrefix ? `${sessionPrefix}-orchestrator` : "");
  const activeRoot = cfg.activeRoot || "";
  // MACHINERY/ACTIVE split: the engine command (machinery) defaults to the installed console script
  // and is independent of activeRoot (the project state dir, passed below as --root). Operators can
  // override stateWriterCommand for non-PATH installs.
  const stateWriterCommand = cfg.stateWriterCommand || "ao-state-writer";
  const projectAllowList = Array.isArray(cfg.projectIds) ? new Set(cfg.projectIds) : null;
  const recentPokes = new Map();

  return {
    name: "orchestrator-poke",
    async notify(event) {
      try {
        if (!orchestratorSession || !activeRoot) {
          if (!warnedMissingConfig) {
            warnedMissingConfig = true;
            console.warn(
              "[orchestrator-poke] missing config: set orchestratorSession (or sessionPrefix) AND "
              + "activeRoot; the notifier WILL NOT poke until both are configured."
            );
          }
          return;
        }
        const data = event?.data ?? {};
        const key = data?.reaction?.key;
        const ok = (event?.type === "reaction.triggered" && RELAYED_REACTION_KEYS.has(key))
                 || RELAYED_EVENT_TYPES.has(event?.type);
        if (!ok) return;
        const sessionId = event?.sessionId, projectId = event?.projectId;
        if (!sessionId) return;
        if (projectAllowList && !projectAllowList.has(projectId)) return;
        if (sessionId === orchestratorSession || sessionId.endsWith("-orchestrator")) return; // never poke for the orchestrator itself
        const now = Date.now();
        for (const [sid, ts] of recentPokes) { if (now - ts >= DEDUP_WINDOW_MS) recentPokes.delete(sid); } // bound the dedup map (TTL evict)
        if (now - (recentPokes.get(sessionId) ?? 0) < DEDUP_WINDOW_MS) return;
        recentPokes.set(sessionId, now);
        const rootArg = shellQuote(activeRoot);
        const msg = `AO_DISPATCH_POKE worker_session=${sessionId} project=${projectId}: `
          + `reconcile-from-state — use canonical active_root ${activeRoot}; run \`${stateWriterCommand} list-ready --root ${rootArg}\` `
          + `and \`${stateWriterCommand} list-gated --root ${rootArg}\`, then dispatch EACH ready pid via \`${stateWriterCommand} dispatch --root ${rootArg} --proposal-id <pid>\` `
          + `(NEVER bare \`continue\`: it returns ambiguous_proposal_id and dispatches nothing on >=2 ready; idempotent; never use the worker worktree as --root). `
          + `If tier-2 gated: run \`${stateWriterCommand} authorize --root ${rootArg} --proposal-id <pid> --evidence <ref>\`, then immediately run \`${stateWriterCommand} dispatch --root ${rootArg} --proposal-id <pid>\` for the same pid in this sweep.`;
        await tmuxSend(["send-keys", "-t", orchestratorSession, "Escape"]);
        await delay(100);
        await tmuxSend(["send-keys", "-t", orchestratorSession, "-l", msg]);
        await tmuxSend(["send-keys", "-t", orchestratorSession, "Enter"]);
      } catch (e) { console.warn(`[orchestrator-poke] swallowed: ${e?.message ?? e}`); }
    },
  };
}
export default { manifest, create };
