# desktop_bridge — GPT Pro Review Adapter

本目录提供一个可选的 browser/CDP adapter 示例，用于把 `review-job`
产生的 GPT Pro 外审任务提交到已登录的 ChatGPT web session，并把 transcript
写回本地 artifact。它是 profile transport 示例，不是 core trust root。

English summary: this directory contains an optional browser/CDP transport
adapter. The canonical trust decision still happens in `ao-state-writer`.

## Contract

`gpt_pro_desktop_bridge.py` 调用脚本：

```bash
chatgpt_browser_review.sh run <package.zip> <prompt.md> <raw_out> <timeout_s>
```

脚本成功时必须把捕获到的最后一条 assistant response 文本写入 `<raw_out>`，并且该文本中必须出现可解析的最终行：

```text
VERDICT: <one of: pass|pass_with_nits|advisory|blocker>
```

之后由 bridge/CLI 负责：

- 校验 package sha；
- 绑定 nonce；
- 复制 artifact 到 `reports/`；
- 用 `AO_CALLER_TYPE=gpt_pro_review_actuator` 通过 state writer 记录 receipt。

orchestrator 或 worker 不应自声明 `gpt_pro_review_actuator`。

## Runtime Prerequisites

此 adapter 依赖用户自己的 profile：

- 一个已登录、可被 Chrome DevTools Protocol 连接的浏览器 session；
- 可运行 Playwright 的 Python；
- 可用的 ChatGPT Pro 订阅或等价 Pro 账号权限；
- 本地环境变量中声明的模型标签、CDP URL 和超时参数。

模型校验是 UI/transport 层证据：adapter 尝试选择与 `AO_GPT_PRO_MODEL_LABELS`
匹配的 Pro 标签，无法匹配就拒绝执行。这不是后端模型身份的密码学证明。

English: model verification is UI/transport-level evidence. The adapter selects
a label configured by `AO_GPT_PRO_MODEL_LABELS` and refuses to proceed if it
cannot. It is not cryptographic proof of backend model identity.

默认环境变量：

- `AO_GPT_PRO_CDP_URL`：CDP endpoint，默认 `http://127.0.0.1:9222`；
- `AO_GPT_PRO_BROWSER_PYTHON`：带 Playwright 的 Python；
- `AO_GPT_PRO_BROWSER_MAX_S`：完成轮询上限；
- `AO_GPT_PRO_MODEL_LABELS`：profile 接受的 Pro 模型标签；
- `AO_GPT_PRO_VERDICT_CACHE_DIR` / `AO_GPT_PRO_VERDICT_CACHE_MAX_AGE_S`：同一 gate/nonce 的 assistant response 缓存。

## Completion Rule

完成条件不是“页面看起来停止生成”，而是：

1. 存在可解析的 `VERDICT` 行；
2. 页面不再 generating；
3. 最后一条 assistant response 连续两次轮询稳定。

这样可以避免把模型开场白、短暂停顿或中间草稿误当成最终 verdict。

## Safety Notes

- 不要把浏览器 cookie、CDP profile、raw transcript 或 runtime cache 提交到公开仓库。
- 如果 adapter 失败，应返回 typed failure 或让 state writer 记录 blocker，不能伪造 pass。
- 真实项目可以替换 `AO_GPT_PRO_DESKTOP_BRIDGE_SCRIPT`，但替换 adapter 必须输出同样的 JSON/artifact contract。
