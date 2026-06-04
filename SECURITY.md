# 安全策略

[English](SECURITY.en.md)

## 支持版本

alpha 阶段只支持当前 `main` 分支。

## 漏洞报告

请通过 GitHub security advisory 或私密 issue 提交，并包含：

- 受影响 commit；
- 复现步骤；
- 预期的 fail-closed 结果；
- 实际观察到的不安全结果；
- 已脱敏的 state/proposal 片段。

## 威胁模型

本项目围绕 local-first orchestration 设计，不把远端服务或外部 adapter 的自声明当成信任根。adapter、桌面桥、本地订阅 CLI 都属于 profile 层；它们的输出必须通过本地 artifact 和 state writer 校验后才进入 canonical state。

state writer 会拒绝：

- 没有可信 caller identity 的自声明外部审查 actor；
- 没有绑定 package hash、nonce、artifact hash、gate proposal 的 GPT Pro receipt；
- `artifact:reports/...` 之外的 artifact ref；
- 不支持的 state schema 或 contract version；
- 未知 action vocabulary；
- non-canonical root。

`AO_CALLER_TYPE` 和 `AO_SESSION_ID` 是本地绑定提示，不是通用认证系统。不要把 `ao-state-writer`、`gpt-pro-actuate` 或 adapter 命令直接暴露成网络服务；如果必须远程调用，请先增加独立认证、授权、审计和 OS-level 隔离。

请不要公开 raw `.omx` state、本地 transcript、桌面对话 id、session id、账号绑定日志、机器专属路径或任何 secret。
