# 架构

AO Open Orchestrator 把职责拆成三层：

1. Runtime wakeup：AO-like runtime 决定什么时候调用 CLI。
2. Canonical state：`StateWriter.apply()` 是状态转换和 review receipt 的唯一写入口。
3. Profile transport：本地 CLI、浏览器自动化、桌面 adapter 等外部能力只返回受约束的 artifact。

core 不假设任何 transport 永远可用。adapter 失败时必须返回 typed failure，或者通过 state writer 提交 blocker proposal；它不能伪造 pass。

## State

canonical state 默认位于：

```text
.omx/state/ao-state-writer/state.json
.omx/state/ao-state-writer/state-transitions.jsonl
```

JSON state 携带 `schema_version = 1`。缺少 `schema_version` 但具备 `state_revision`、`targets`、`proposal_results` 的 legacy v1 state 可以读取，并会在下次写入时补齐。

## Actions

action 分为三类：

- `AUTO_SPAWN_ACTIONS`：tier-1 action，preflight 通过后可以 dispatch；
- `GATED_ACTIONS`：需要 owner/orchestrator authorization 的 action；
- `NON_EXECUTABLE_ACTIONS`：convergence 状态，不能 dispatch。

未知 current obligation 会返回 `unsupported_current_obligation` 和 repair envelope，不能被静默跳过。
