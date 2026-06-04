# 外部审查 Receipt Contract

外部审查只是 advisory evidence。只有被 `StateWriter.apply()` 接受后，它才进入 canonical state。

## Codex cc

pass-family Codex cc receipt 必须包含：

- `review_scope = "codex_cc"`；
- `actor_role = "codex_cc"`；
- 可信 caller identity：`AO_CALLER_TYPE=codex_cc`；
- 预期 model 和 reasoning effort；
- `codex_cc_transcript_sha256`；
- evidence refs。

## GPT Pro

真实 GPT Pro receipt 必须包含：

- `review_scope = "gpt_pro"`；
- `actor_role = "gpt_pro"`；
- 可信 caller identity：`AO_CALLER_TYPE=gpt_pro_review_actuator`；
- 与已授权 pending package 匹配的 `package_sha256`；
- `external_review_submission_nonce`；
- `external_review_receipt_sha256`；
- `external_review_artifact_ref`；
- `external_review_gate_proposal_id`。

artifact ref 必须是 `artifact:reports/<relative-file>`。绝对路径、父目录穿越和仓库外文件都会被拒绝。

watchdog timeout blocker 是例外：它不声称自己是真实外部 receipt，但仍必须由 `AO_CALLER_TYPE=watchdog` 提交，并通过 state writer 接受后才影响 continuation。
