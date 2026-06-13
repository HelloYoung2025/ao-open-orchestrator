# 快速开始：用 bootstrap 开展一个新项目

目标：**一次安装引擎 + 每个项目跑一次 bootstrap → 指定一个新目录即可开展新项目。**
引擎（机械臂）只装一份；每个项目目录只放 config + 治理文件 + 规范状态。

## 1. 安装引擎（一次）

```
pip install ao-open-orchestrator
```

这会装上 `ao-state-writer` 与 `ao-commander` 两个 console script。引擎命令（MACHINERY）
与项目状态目录（ACTIVE_ROOT）是解耦的：bootstrap 渲染出的文件用
`@STATE_WRITER_CMD@`（默认就是已安装的 `ao-state-writer`）配合 `--root <active_root>` 调用引擎。

## 一键替代：启动页

第 2-6 步也可以用本地网页表单一键完成：

在 Finder 里双击仓库根目录的 `launch-ui.command`（macOS），或在终端运行：

```bash
python3 scripts/launch_ui.py
```

两种方式都会自动在默认浏览器打开表单（127.0.0.1，随机端口）。

页面有两种模式：**从零新建**（向全新目录渲染完整脚手架）和**接管已有项目**（项目里已经
有自己的计划/TODO，文件名随意）。接管严格执行"缺什么补什么"：只补缺失的 AO 工件
（brain yaml、sidecar、plist——仅当 `DIRECT_PROJECT_CONTRACT.toml` 不存在时才补 AO 契约），
绝不覆盖任何已有文件；你的计划和 TODO 必须已存在，且永远不会被写入。

填好项目信息、产品目标和第一个切片后点启动。每一步都 fail-closed：第一处失败立即停下，
并显示已发生的步骤和回滚锚。两条刻意的边界：启动页只在 `~/.agent-orchestrator/config.yaml`
不存在或为空时**自动创建**——已有任何内容都会退回第 3 步的手工粘贴（见
[MULTI_PROJECT.zh-CN.md](MULTI_PROJECT.zh-CN.md)）；表单是 `MASTER_PLAN.md` 的一次性
撰写入口——启动之后该文件只归你改（第 4 步的人类门照旧生效）。

## 2. 渲染一个新项目

> **注意：bootstrap 是源码 checkout 专用脚本，不随 wheel 安装。** 引擎（`ao-state-writer`/
> `ao-commander`）走 `pip install`，但模板渲染器要从本仓 `git clone` 的 checkout 里运行
> （模板读自 `examples/`，刻意不打进 wheel）。在已 pip 安装引擎的机器上仍需 clone 本仓来跑这一步。

从本仓 checkout 运行 bootstrap（模板读自 `examples/`）：

```
python3 scripts/bootstrap_project.py \
  --project-id my-proj \
  --orchestrator-session my-proj-orchestrator \
  --repo-owner my-org --repo-name my-repo \
  --agent claude-code \
  --worker-model <worker-model> --orchestrator-model <orchestrator-model> \
  --target-dir ~/projects/my-proj
```

它会把全部模板渲染进 `--target-dir`：`DIRECT_PROJECT_CONTRACT.toml`、`MASTER_PLAN.md`、
`TODO.md`、`SESSION_LOG.md`、`agent-orchestrator.yaml`（大脑）、`<label>.plist`、
`orchestrator-liveness.sh`、`.commander/commander.toml`，并**打印** `~/.agent-orchestrator/config.yaml`
的片段（见下一步）。

可选项均有默认值：`--active-root`（默认=目标目录）、`--session-prefix`（默认=去掉
`-orchestrator` 后缀）、`--plan-file/--todo-file/--session-log-file`、`--home`、`--path`、
`--python-bin`、`--state-writer-cmd`、`--label`、`--log-file`、`--sidecar-path`。

安全边界：bootstrap **只渲染到一个新的/空目录**；它拒绝符号链接、非空目录、你的 HOME、
`~/.agent-orchestrator`、本引擎仓、以及任何带 `.omx` 状态树的目录；它**不**创建 `.omx/state`
（引擎在首次运行时拥有规范状态），也**不**覆盖已存在文件。

## 3. 注册项目（你来粘贴）

bootstrap **不会**替你写全局 `~/.agent-orchestrator/config.yaml`（那是 operator 自己的主机配置）。
把它打印出来的 `projects:` + `notifiers:` 块粘进 `~/.agent-orchestrator/config.yaml`。

## 4. 填好门控内容

- 编辑渲染出的 `MASTER_PLAN.md`：这是产品的规范事实源，也是**唯一**的人类 Owner 门控文件，
  orchestrator 永不编辑它。
- 填 `.commander/commander.toml` 的 `codex_worker_thread_id` 与 `reviewer_target`；二者为空时
  Commander 的 `doctor`/`brief` 会响亮失败。

## 5.（可选）无人值守 sidecar

要让 loop 在崩溃后自愈，把渲染出的 plist 装进 launchd、并把
`orchestrator-liveness.sh` 放到 `--sidecar-path` 指向的位置。装载 launchd 是 operator/owner 动作，
不在 bootstrap 范围内。详见 [UNATTENDED_LOOP.zh-CN.md](UNATTENDED_LOOP.zh-CN.md)。

## 6. 启动并引导第一切片

```
ao start my-proj
```

然后由外部把第一切片引导给 orchestrator；此后每个完成的 worker 的已接受 proposal 会生成下一个
obligation，orchestrator reconcile-from-state 后自动派发，loop 自走。
