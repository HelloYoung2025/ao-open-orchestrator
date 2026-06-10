# 外部审查 Receipt Contract

外部审查只是 advisory evidence。只有被 `StateWriter.apply()` 接受后，它才进入 canonical state。

## Verdict 术语

`pass-family` 指 `pass`、`pass_with_nits` 或 `advisory`。这些 verdict 只有在 receipt proof 全部通过时，才可以继续推进到 closure candidate。

## Codex cc

已接受的 pass-family Codex cc receipt 必须包含：

- `review_scope = "codex_cc"`；
- `actor_role = "codex_cc"`；
- 可信 caller identity：`AO_CALLER_TYPE=codex_cc`；
- 预期 model 和 reasoning effort；
- `codex_cc_transcript_sha256`；
- `codex_cc_transcript_artifact_ref`，指向 `reports/codex-cc-receipts/` 下的 repo 内 transcript
  文件（其 sha256 必须与 `codex_cc_transcript_sha256` 匹配）；
- evidence refs。

## escalated review

已接受的 escalated review receipt 必须包含：

- `review_scope = "escalated_review"`；
- `actor_role = "escalated_review"`；
- 可信 caller identity：`AO_CALLER_TYPE=escalated_review_actuator`；
- 与已授权 pending package 匹配的 `package_sha256`；
- `external_review_submission_nonce`；
- `external_review_receipt_sha256`；
- `external_review_artifact_ref`；
- `external_review_gate_proposal_id`。

artifact ref 必须是 `artifact:reports/<relative-file>`。绝对路径、父目录穿越和仓库外文件都会被拒绝。

存在两个只用于 blocker 的例外：

- watchdog timeout blocker：它不声称自己是外部 receipt，但仍必须由 `AO_CALLER_TYPE=watchdog` 提交；
- actuator failure blocker：它不声称已经 harvest 到外部 receipt，但仍必须具备 sanctioned caller identity 以及 gate/package 绑定。

这两类例外也必须先被 state writer 接受，才能影响 continuation。
