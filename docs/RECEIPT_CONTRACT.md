# External Review Receipt Contract

External review is advisory evidence. It becomes canonical only when accepted
by `StateWriter.apply()`.

## Codex cc

A pass-family Codex cc receipt must include:

- `review_scope = "codex_cc"`;
- `actor_role = "codex_cc"`;
- trusted caller identity `AO_CALLER_TYPE=codex_cc`;
- expected model and reasoning effort;
- `codex_cc_transcript_sha256`;
- evidence refs.

## GPT Pro

A real GPT Pro receipt must include:

- `review_scope = "gpt_pro"`;
- `actor_role = "gpt_pro"`;
- trusted caller identity `AO_CALLER_TYPE=gpt_pro_review_actuator`;
- `package_sha256` matching the authorized pending package;
- `external_review_submission_nonce`;
- `external_review_receipt_sha256`;
- `external_review_artifact_ref`;
- `external_review_gate_proposal_id`.

The artifact ref must be `artifact:reports/<relative-file>`. Absolute paths,
parent traversal, and files outside the repository are rejected.

Watchdog timeout blockers are the exception: they do not claim to be an
external receipt, but they still must be submitted by `AO_CALLER_TYPE=watchdog`
and accepted through the state writer.
