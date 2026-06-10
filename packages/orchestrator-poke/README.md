# ao-notifier-orchestrator-poke

An AO orchestrator **notifier** plugin (slot: `notifier`). When a worker session emits a
needs-input reaction/event, it pokes the orchestrator's tmux session with a *reconcile-from-state*
instruction so the orchestrator dispatches the next ready proposal — keeping the loop self-propelling
without an external scheduler.

## Behaviour

- Relays only `agent-needs-input` / `report-needs-input` reactions and `session.needs_input` /
  `report.needs_input` events.
- Never pokes the orchestrator session for itself (skips the configured session and any
  `*-orchestrator` session id).
- Deduplicates per worker session within a 10s window.
- Optional per-project allow-list.
- **Never throws**: a notifier failure must not break the orchestrator's lifecycle poll. A failed
  `tmux` call is logged and swallowed.

## Config

```js
import poke from "ao-notifier-orchestrator-poke";

const notifier = poke.create({
  orchestratorSession: "myproj-orchestrator", // or set sessionPrefix -> `${prefix}-orchestrator`
  activeRoot: "/abs/path/to/project",         // project state root, passed to the engine as --root
  stateWriterCommand: "ao-state-writer",      // optional; defaults to the installed console script
  projectIds: ["myproj"],                     // optional allow-list
});
```

If `orchestratorSession` (or `sessionPrefix`) **and** `activeRoot` are not configured, the notifier
loads but does not poke; it warns once. The engine command is decoupled from `activeRoot`: the
project state directory is passed as `--root <activeRoot>` (shell-quoted), while the engine itself is
the installed `ao-state-writer` console script (override via `stateWriterCommand`).

## Licence

Apache-2.0. See `LICENSE`.
