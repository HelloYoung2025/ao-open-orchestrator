# External Review Receipt Contract

External review is advisory evidence. It becomes canonical only when accepted
by `StateWriter.apply()`.

## Verdict Terms

`pass-family` means one of `pass`, `pass_with_nits`, or `advisory`. These
verdicts may advance toward a closure candidate when all receipt proof checks
also pass.

## Codex cc

An accepted pass-family Codex cc receipt must include:

- `review_scope = "codex_cc"`;
- `actor_role = "codex_cc"`;
- trusted caller identity `AO_CALLER_TYPE=codex_cc`;
- expected model and reasoning effort;
- `codex_cc_transcript_sha256`;
- `codex_cc_transcript_artifact_ref` pointing to the repo-local transcript under
  `reports/codex-cc-receipts/` (its sha256 must match `codex_cc_transcript_sha256`);
- evidence refs.

## escalated review

An accepted escalated review receipt must include:

- `review_scope = "escalated_review"`;
- `actor_role = "escalated_review"`;
- trusted caller identity `AO_CALLER_TYPE=escalated_review_actuator`;
- `package_sha256` matching the authorized pending package;
- `external_review_submission_nonce`;
- `external_review_receipt_sha256`;
- `external_review_artifact_ref`;
- `external_review_gate_proposal_id`.

The artifact ref must be `artifact:reports/<relative-file>`. Absolute paths,
parent traversal, and files outside the repository are rejected.

Two blocker-only exceptions exist:

- watchdog timeout blockers: they do not claim to be an external receipt, but
  they still must be submitted by `AO_CALLER_TYPE=watchdog`;
- actuator failure blockers: they do not claim a harvested external receipt,
  but they still require sanctioned caller identity plus gate/package binding.

Both exception types must still be accepted through the state writer before
they affect continuation.
