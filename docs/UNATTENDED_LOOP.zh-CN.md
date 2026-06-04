# 无人值守 Loop 原则

“无人值守”指的是在 runtime 存活并持续收到 tick 时，自动推进到下一个安全边界。它不是绕过未知业务逻辑、owner-only gate、宿主崩溃、系统休眠或外部服务故障。

每次 tick 必须落入三类之一：

1. 继续执行受支持的 action；
2. 进入 typed repair 或 convergence；
3. 停在明确 gate。

大多数 gate 是 owner-proxy/orchestrator gate。只有 master plan 修改、破坏性操作等明确 human-only action 才需要当前 human owner。

静默停住是设计失败。fail-closed 结果应是 typed JSON blocker。有些 blocker 会包含 `blocked_action`、`forbidden_actions`、`allowed_repair_actions`；authorization wait 当前返回更小的 envelope。不要假设所有 blocker 都带 `repair_obligation` 或 `evidence_anchor`，除非你使用的 CLI 路径确实会发出这些字段。
