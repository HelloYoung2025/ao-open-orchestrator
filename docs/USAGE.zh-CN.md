# 使用说明：快速上手与日常操作

[English](USAGE.md)

定位：[QUICKSTART.zh-CN.md](QUICKSTART.zh-CN.md) 负责"从零建一个项目"；本篇负责**建好之后怎么用**——
五分钟心智模型、日常命令手册、怎么正确踢醒 orchestrator、常见卡点排障。全部内容来自真实生产部署的实战经验。

## 0. 五分钟心智模型

- 一个项目的全部规范状态住在它的 ACTIVE_ROOT（`.omx/state/ao-state-writer/`），**单一 writer**。
  引擎没有 daemon、没有队列：每次调用都是一条命令进、一个 JSON 信封出。
- 标准闭环：worker 完成工作 → `apply` 提案（如 `evidence_pending`）→ 向 runtime 报告 needs-input →
  runtime 的 reaction poke orchestrator → orchestrator 对账（`list-ready` + `list-gated`）→
  `dispatch` 下一动作 → 新 worker 拉起。每个已接受的提案生成下一个 obligation，loop 自走。
- 两级评审：小章节走 codex review（tier-1，orchestrator 常规 policy 自动授权）；
  大章节走 escalated review（tier-2，需要 orchestrator 先 `authorize` 再 `dispatch`）。
- 一切 fail-closed：不认识的输入、陈旧的 receipt、越权的 caller、对不上的 root——都变成
  **typed blocker**（机器可读的明确拒绝），绝不静默停住。看到拒绝信封先读 `reasons`，再决定动作。

## 1. 日常命令手册

所有子命令都要 `--root <ACTIVE_ROOT>`。装好 wheel 后用 console script `ao-state-writer`，
或等价的 `python -m ao_state_writer.cli`。

**只读（随时可跑，不动状态）：**

```bash
ao-state-writer list-ready --root <ROOT>        # 哪些提案已就绪可派发
ao-state-writer list-gated --root <ROOT>        # 哪些在等 tier-2 授权
ao-state-writer reconcile-once --root <ROOT>    # 全景对账摘要（含历史派发记录）
ao-state-writer dispatch-stall-check --root <ROOT> \
  --last-seen-revision <N> --no-advance-count <K>   # 停滞检测；水位线由调用方自管
ao-state-writer watchdog --root <ROOT>          # 评审超时观测（dry-run 取证）
```

**会写状态（有 orchestrator/operator 身份门控）：**

```bash
ao-state-writer dispatch --root <ROOT> --proposal-id <pid>     # 派发一个 ready 提案
ao-state-writer authorize --root <ROOT> --proposal-id <pid> \
  --evidence <ref>                                             # tier-2 授权（随后同一轮内 dispatch）
ao-state-writer pause --root <ROOT>                            # 状态可见的全局暂停
ao-state-writer resume --root <ROOT>                           # 恢复
ao-state-writer reconcile-leases --root <ROOT> [--apply]       # 孤儿 lease 清理（默认 dry-run）
ao-state-writer retire-dead-sessions --root <ROOT> [--apply]   # 僵尸会话 GC（默认 dry-run）
```

**退出码约定**：`0` 正常；`2` 输入畸形；`3` fail-closed 门（blocked、需授权等——信封里有
`reasons` 与 `allowed_repair_actions`）；`4` 歧义（如多个 ready 时跑了裸 `continue`——
改用 `dispatch --proposal-id` 逐个派）。**`3` 不是崩溃**，是引擎在按设计拒绝。

## 2. 怎么正确踢醒 orchestrator（重要实战经验）

orchestrator 是事件驱动的：没 poke 就不动。踢醒它的**唯一可靠通道**是 runtime 的官方注入：

```bash
ao send <orchestrator-session> "<消息>"
```

实测教训：

- 裸 `tmux send-keys -t <session> ... Enter`（或 `C-m`）对 agent TUI **不生效**——文字会进输入框，
  但回车不会触发提交。别浪费时间调它，直接 `ao send`。
- `ao send` 会把会话输入框里**遗留的未发送文字一并带出提交**。通常无害（消息会拼在一起），
  但发送前知道这一点。
- 何时需要手动踢：ready 队列挂着几小时没人派发（见 §3 第一条）；机器睡醒之后；
  手工修复状态之后；恢复出来的 orchestrator 还抱着过时信念时（poke 里显式作废旧指示）。

**poke 消息模板**（事实 + 请求，别只说"醒醒"）：

> 事实：worker X 已于 <时间> 提交 <状态>（rev N），canonical 现状 = …，list-ready 当前 = …。
> 请求：从 canonical state 对账，按你的常规 policy 派发。

## 3. 常见卡点排障

| 症状 | 根因 | 处置 |
| --- | --- | --- |
| ready 提案挂数小时无派发 | 唤醒链断了：sidecar 巡逻死/needs-input poke 没送达 | 查 per-project sidecar 日志（`orchestrator-liveness.<project_id>.log`）→ `ao send` 手动 poke → 长期解法用 v0.3.0 的 detached-revive sidecar |
| `blocked: unrecognized_governance_dirty:<file>` | worker 留下未提交的治理文件 | orchestrator 派发 governance-dirty review 修复；或 operator 核实后自行提交 |
| `requires_orchestrator_authorization`（exit 3） | tier-2 动作未授权 | orchestrator：`authorize` → 同一轮 sweep 内 `dispatch` |
| `ambiguous_proposal_id`（exit 4） | 多个 ready 时跑裸 `continue` | 改 `dispatch --proposal-id <pid>` 逐个派 |
| `stale_review_receipt_for_inactive_round` / `stale_escalated_review_gate` | 迟到或陈旧的评审 receipt 被拒 | **无需处置**——这是防错设计在工作，当前轮次的真 receipt 不受影响 |
| `escalated_review_actuator_busy`（exit 0） | 同机另一项目正占用外审通道 | 稍后重试；本项目 lease 未被消耗 |
| 评审 worker 超时 | 评审超过 staged 时限 | 不补跑、不造假 verdict：worker 停手报 needs-input，watchdog 记 typed blocker 走 bounded repair |
| sidecar 拉起 runtime 后巡逻再无动静 | 旧版同步 revive（`ao start` 前台守护永不返回，卡死巡逻循环） | 换 `examples/orchestrator-liveness.sh` 的 detached-revive 版（v0.3.0） |
| 注入消息没反应 | 用了裸 tmux send-keys | 见 §2，走 `ao send` |

## 4. 多项目并行

一台机器一个 runtime daemon 多路复用全部项目；外审通道有机器级互斥。
per-project 三件套、第 N+1 个项目的上线流程见 [MULTI_PROJECT.zh-CN.md](MULTI_PROJECT.zh-CN.md)。

## 5. 安全边界备忘

- 项目的 master plan 文件是**唯一人类 Owner 门**：orchestrator 与一切自动化永不编辑它。
- 引擎不删自动化、不删自己；删除类操作永远是 operator 亲手动作。
- 所有带 `--apply` 的命令默认 dry-run——先看清单再下手。
- 公开发布、merge、生产副作用属于 operator/orchestrator 明确授权动作，引擎不会替你做。
