# 多项目：一台机器跑 N 个项目

目标：**引擎安装一次，注册 N 个项目，并让它们同时运行**——任何项目的状态、会话、部署产物、
外部评审驱动都不会渗入另一个项目。本设计淘汰的历史病根是：机械装置根目录与活动项目根目录被
强行合在一个目录里；现在 MACHINERY 只装一次，每个项目只是"一个目录 + 一条配置注册"。

## 拓扑：单 daemon，N 项目

主机上只跑**一个** AO daemon。它对 `~/.agent-orchestrator/config.yaml` 中 `projects:` 下注册的
全部项目做多路复用；通知器按 `projectIds` 过滤，tmux 会话按各项目的 `sessionPrefix` 命名空间
隔离。不是每个项目跑一个 daemon——而是对同一套安装按项目执行 `ao start <project-id>`。

每个项目的状态在构造层面即完全隔离：项目的每次引擎调用都携带 `--root <active_root>`，规范
状态树（`state.json`、决策台账、派发台账）都在该根目录之下。引擎中没有任何状态以全局键存储，
所以两个项目的 orchestrator 并发推进时不可能触碰对方的 target、proposal 或 revision 链。
该性质由 `tests/test_multi_project_smoke.py` 钉死。

## 每项目三件套

上线一个项目恰好需要三件每项目的东西（单项目完整流程见 [QUICKSTART.zh-CN.md](QUICKSTART.zh-CN.md)）：

1. **项目目录**（由 `scripts/bootstrap_project.py` 渲染进空的 `--target-dir`）：
   `DIRECT_PROJECT_CONTRACT.toml`、治理文件（`MASTER_PLAN.md`、`TODO.md`、`SESSION_LOG.md`）、
   大脑（`agent-orchestrator.yaml`）、`.commander/commander.toml`，以及 sidecar 一对
   （`orchestrator-liveness.sh` + `<label>.plist`）。规范状态在引擎首次写入时出现在该目录下。
2. **主机配置片段**（bootstrap 打印、操作员粘贴）：一个 `projects.<project-id>` 块加一个
   `notifiers.orchestrator-poke` 块（其 `projectIds` 列出该项目）。全局
   `~/.agent-orchestrator/config.yaml` 归操作员所有；bootstrap 绝不代写。
3. **每项目 launchd plist**（可选，用于无人值守 sidecar——见
   [UNATTENDED_LOOP.zh-CN.md](UNATTENDED_LOOP.zh-CN.md)）：label 默认
   `ao-orchestrator-liveness.<project-id>`，liveness 日志默认
   `~/.agent-orchestrator/<project-id>-orchestrator-liveness.log`——N 个项目的 plist、label、
   日志在同一个 `LaunchAgents` 目录、同一个 home 下永不碰撞。sidecar 的单例守卫 pidfile 同样
   按项目隔离（`~/.agent-orchestrator/orchestrator-liveness.<project-id>.pid`）——守卫只拦
   *同一*项目 sidecar 的第二个实例，绝不拦兄弟项目的。

## 机器全局资源（刻意共享的表面）

以上一切都是每项目的。下表是**设计上机器全局**的表面的完整清单，以及各自在 N 个项目并发下的
安全机制：

| 表面 | 为什么是机器全局 | 并发机制 |
| --- | --- | --- |
| escalated-review actuator | 一台机器只有一个外部评审表面（一个浏览器 profile、一个评审账号）——两个项目同时驱动会交错提交 | 非阻塞机器全局 `flock`，在每项目派发 claim 成功**之后**获取，并跨整个 bridge 运行持有。默认锁文件：`~/.agent-orchestrator/locks/escalated-review-actuator.lock`；用 `AO_ESCALATED_REVIEW_ACTUATOR_LOCK_DIR` 覆盖其**目录**。争用时落败项目得到 `escalated_review_actuator_busy`（exit 0），其派发 lease **被释放而非消费**——稍后 actuate 直接重试。持有者被 SIGKILL 也卡不死机器：内核随进程消亡自动释放 flock。由 `tests/test_actuator_machine_lock.py` 钉死。 |
| AO daemon + `~/.agent-orchestrator/config.yaml` | 单 daemon 复用全部项目；主机配置是唯一注册表 | 每项目一个 `projects.<id>` 块；通知投递按 `projectIds` 过滤；tmux 按 `sessionPrefix` 隔离。保持各项目的 `project-id`、`orchestrator-session`、`sessionPrefix` 互异——bootstrap 按项目派生，smoke 测试断言渲染树零跨项目身份。 |
| bridge 命令的临时空间 | 操作员提供的 `--bridge-command` 可能在系统临时目录写自己的 temp/cache 文件 | 引擎自身不写任何机器全局临时文件。如果你的 bridge 命令缓存产物，请以 package sha + 每次运行的 nonce 作 key（碰撞概率 ≈ 0），或通过环境变量把各项目的 bridge 指向各自的 scratch 目录——这不需要也不提供引擎改动。 |

不在此表中的一切——状态树、台账、项目根下的锁、治理文件、tmux 会话、sidecar 日志——都是每
项目的，不需要任何协调。

## 上线第 N+1 个项目

已有 N 个项目在跑时，再加一个不会触碰任何正在运行的东西：

1. 渲染：`python3 scripts/bootstrap_project.py --project-id new-proj … --target-dir <空目录>`
   （bootstrap 拒绝非空目录、符号链接、home、`~/.agent-orchestrator`、引擎仓库本身和 `.omx`
   状态树——它不可能与在跑项目相撞）。
2. 注册：把打印出的 `projects:` + `notifiers:` 片段粘进 `~/.agent-orchestrator/config.yaml`，
   把新 id 追加进 poke 通知器的 `projectIds`（若路由方式不同则另加一个通知器块）。
3. （可选）安装渲染出的 plist + sidecar 实现无人值守自愈。
4. 启动：`ao start new-proj`，然后从外部 bootstrap 它的第一个切片。既有项目的循环不受影响；
   新项目与其他项目唯一可能的交互，是在 escalated-review actuator 锁上排队等自己的轮次。
