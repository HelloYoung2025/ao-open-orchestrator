# Quickstart: bootstrap a new project

Goal: **install the engine once, run bootstrap once per project, and start a new project by
pointing it at a new directory.** The engine (the "machinery") is installed once; each project
directory holds only config + governance files + canonical state.

## 1. Install the engine (once)

```
pip install ao-open-orchestrator
```

This installs the `ao-state-writer` and `ao-commander` console scripts. The engine command
(MACHINERY) is decoupled from the project state directory (ACTIVE_ROOT): the files bootstrap
renders invoke the engine via `@STATE_WRITER_CMD@` (default: the installed `ao-state-writer`)
with `--root <active_root>`.

## 2. Render a new project

> **Note: bootstrap is a source-checkout-only script — it is not shipped in the wheel.** The engine
> (`ao-state-writer`/`ao-commander`) is `pip install`-ed, but the template renderer runs from a
> `git clone` of this repo (templates live in `examples/` and are intentionally not packaged). Even
> on a machine where the engine is already pip-installed, you still clone this repo to run this step.

Run bootstrap from this repo checkout (templates are read from `examples/`):

```
python3 scripts/bootstrap_project.py \
  --project-id my-proj \
  --orchestrator-session my-proj-orchestrator \
  --repo-owner my-org --repo-name my-repo \
  --agent claude-code \
  --worker-model <worker-model> --orchestrator-model <orchestrator-model> \
  --target-dir ~/projects/my-proj
```

It renders every template into `--target-dir`: `DIRECT_PROJECT_CONTRACT.toml`, `MASTER_PLAN.md`,
`TODO.md`, `SESSION_LOG.md`, `agent-orchestrator.yaml` (the brain), `<label>.plist`,
`orchestrator-liveness.sh`, `.commander/commander.toml`, and **prints** the
`~/.agent-orchestrator/config.yaml` fragment (next step).

Optional inputs have defaults: `--active-root` (default: the target dir), `--session-prefix`
(default: the orchestrator-session with a trailing `-orchestrator` stripped),
`--plan-file/--todo-file/--session-log-file`, `--home`, `--path`, `--python-bin`,
`--state-writer-cmd`, `--label`, `--log-file`, `--sidecar-path`.

Safety: bootstrap renders **only into a new/empty directory**; it refuses symlinks, non-empty
directories, your HOME, `~/.agent-orchestrator`, this engine repo, and any directory inside a
`.omx` state tree; it does **not** create `.omx/state` (the engine owns canonical state on first
run) and never overwrites an existing file.

## 3. Register the project (you paste)

Bootstrap does **not** write the global `~/.agent-orchestrator/config.yaml` (that host config is
operator-owned). Paste the printed `projects:` + `notifiers:` block into it.

## 4. Fill the gated content

- Edit the rendered `MASTER_PLAN.md`: it is the canonical product source of truth and the **only**
  human-owner-gated file; the orchestrator never edits it.
- Fill `codex_worker_thread_id` and `reviewer_target` in `.commander/commander.toml`; the
  Commander's `doctor`/`brief` fail loud while they are empty.

## 5. (Optional) Unattended sidecar

For crash self-healing, install the rendered plist into launchd and place
`orchestrator-liveness.sh` at the `--sidecar-path` location. Loading launchd is an operator/owner
action, outside bootstrap's scope. See [UNATTENDED_LOOP.md](UNATTENDED_LOOP.md).

## 6. Start and bootstrap the first slice

```
ao start my-proj
```

Then externally bootstrap the first slice to the orchestrator; thereafter each finished worker's
accepted proposal creates the next obligation, which the orchestrator dispatches after
reconcile-from-state — the loop self-propels.
