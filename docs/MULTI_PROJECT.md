# Multi-project: N projects on one machine

Goal: **install the engine once, register N projects, and run them at the same time** — without
any project's state, sessions, deployment artifacts, or external-review actuation bleeding into
another's. The historical failure mode this design retires is a machinery root and an active
project root forced into one directory; here MACHINERY is installed once and every project is
just a directory plus a config registration.

## Topology: one daemon, N projects

The host runs **one** AO daemon. It multiplexes every project registered under `projects:` in
`~/.agent-orchestrator/config.yaml`; notifiers filter by `projectIds`, and tmux sessions are
namespaced by each project's `sessionPrefix`. You do not run one daemon per project — you run
`ao start <project-id>` per project against the same daemon installation.

Per-project state is fully isolated by construction: each project's engine invocations carry
`--root <active_root>`, and the canonical state tree (`state.json`, the decision ledger, dispatch
ledger) lives under that root. Nothing in the engine keys state by anything global, so two
projects' orchestrators advancing concurrently cannot touch each other's targets, proposals, or
revision chains. This is pinned by `tests/test_multi_project_smoke.py`.

## The per-project triad

Bringing up a project means exactly three per-project things (see
[QUICKSTART.md](QUICKSTART.md) for the single-project walkthrough):

1. **Project directory** (rendered by `scripts/bootstrap_project.py` into an empty
   `--target-dir`): `DIRECT_PROJECT_CONTRACT.toml`, governance files (`MASTER_PLAN.md`,
   `TODO.md`, `SESSION_LOG.md`), the brain (`agent-orchestrator.yaml`),
   `.commander/commander.toml`, and the sidecar pair (`orchestrator-liveness.sh` +
   `<label>.plist`). Canonical state appears under this directory on first engine write.
2. **Host config fragment** (printed by bootstrap, pasted by the operator): one
   `projects.<project-id>` block plus one `notifiers.orchestrator-poke` block whose
   `projectIds` lists that project. The global `~/.agent-orchestrator/config.yaml` is
   operator-owned; bootstrap never writes it.
3. **Per-project launchd plist** (optional, for the unattended sidecar — see
   [UNATTENDED_LOOP.md](UNATTENDED_LOOP.md)): the label defaults to
   `ao-orchestrator-liveness.<project-id>` and the liveness log defaults to
   `~/.agent-orchestrator/<project-id>-orchestrator-liveness.log`, so N projects' plists,
   labels, and logs never collide in one `LaunchAgents` directory or one home. The sidecar's
   singleton-guard pidfile is per-project too
   (`~/.agent-orchestrator/orchestrator-liveness.<project-id>.pid`) — the guard stops a second
   instance of the *same* project's sidecar, never a sibling project's.

## Machine-global resources (the deliberately shared surfaces)

Everything above is per-project. The table below is the complete list of surfaces that are
**machine-global by design**, and how each one stays safe under N concurrent projects:

| Surface | Why it is machine-global | Concurrency story |
| --- | --- | --- |
| Escalated-review actuator | One external review surface per machine (one browser profile, one reviewer account) — two projects driving it at once would interleave submissions | Non-blocking machine-global `flock` taken **after** the per-project dispatch claim and held across the whole bridge run. Default lock: `~/.agent-orchestrator/locks/escalated-review-actuator.lock`; override the **directory** with `AO_ESCALATED_REVIEW_ACTUATOR_LOCK_DIR`. On contention the losing project gets `escalated_review_actuator_busy` (exit 0) and its dispatch lease is **released, not consumed** — a later actuate simply retries. A SIGKILLed holder cannot wedge the machine: the kernel drops the flock with the process. Pinned by `tests/test_actuator_machine_lock.py`. |
| AO daemon + `~/.agent-orchestrator/config.yaml` | One daemon multiplexes all projects; the host config is the single registry | Per-project `projects.<id>` blocks; notifier delivery filtered by `projectIds`; tmux namespaced by `sessionPrefix`. Keep every project's `project-id`, `orchestrator-session`, and `sessionPrefix` distinct — bootstrap derives them per project, and the smoke test asserts the rendered trees carry zero cross-project identity. |
| Bridge-command scratch space | An operator-supplied `--bridge-command` may write its own temp/cache files under the system temp directory | The engine itself writes no machine-global temp files. If your bridge command caches artifacts, key them by package sha + a per-run nonce (collision probability ≈ 0), or point each project's bridge at its own scratch directory via environment — no engine change is needed or provided for this. |

Everything not in this table — state tree, ledgers, locks under the project root, governance
files, tmux sessions, sidecar logs — is per-project and needs no coordination.

## Onboarding project N+1

With N projects already live, adding another touches nothing that is running:

1. Render: `python3 scripts/bootstrap_project.py --project-id new-proj … --target-dir <empty-dir>`
   (bootstrap refuses non-empty directories, symlinks, your home, `~/.agent-orchestrator`, the
   engine repo, and `.omx` state trees — it cannot collide with a live project).
2. Register: paste the printed `projects:` + `notifiers:` fragment into
   `~/.agent-orchestrator/config.yaml`, appending the new id to the poke notifier's
   `projectIds` (or adding a second notifier block if you route projects differently).
3. (Optional) Install the rendered plist + sidecar for unattended self-healing.
4. Start: `ao start new-proj`, then externally bootstrap its first slice. Existing projects'
   loops are unaffected; the only cross-project interaction the new project can ever have is
   waiting its turn on the escalated-review actuator lock.
