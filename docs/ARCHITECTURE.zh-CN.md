# 架构

AO Open Orchestrator 把职责拆成三层：

1. Runtime wakeup：AO-like runtime 决定什么时候调用 CLI。
2. Canonical state：`StateWriter.apply()` 是状态转换和 review receipt 的唯一写入口。
3. Profile transport：本地 CLI 等外部能力只返回受约束的 artifact。本仓库只附一个无品牌 reference actuator，它 shell 出 profile 自有的 reviewer 命令；不打包任何具体产品 bridge，真实部署时的 transport 选择仍应放在私有 profile/contract 中。

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

## Runtime 边界

core 本身不负责让进程常驻。如果宿主进程退出、电脑休眠或外部服务消失，需要本包之外的 supervisor 重新唤醒 loop。core 的保证只覆盖它实际收到的 tick。

## Gate 语义

不是所有 gate 都是 human-only。escalated review 使用 orchestrator/owner-proxy authorization 路径。只有项目契约明确标记为 human-only 的 action，例如 master plan 修改或破坏性操作，才需要当前 human owner。
