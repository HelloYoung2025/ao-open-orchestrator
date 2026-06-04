# Unattended Loop Principle

"Unattended" means automatic progress to the next safe boundary while the
runtime is alive and being ticked. It does not mean bypassing unknown business
logic, owner-only gates, host crashes, system sleep, or external service
failures.

Every tick should land in one of three buckets:

1. continue a supported action;
2. enter typed repair or convergence;
3. stop at an explicit gate.

Most gates are owner-proxy/orchestrator gates. Current human-owner intervention
is reserved for explicitly human-only actions such as master-plan edits or
destructive operations.

Silent stalls are treated as design failures. Fail-closed results are typed JSON
blockers. Some blockers include `blocked_action`, `forbidden_actions`, and
`allowed_repair_actions`; authorization waits currently return a smaller
envelope. Do not assume every blocker carries `repair_obligation` or
`evidence_anchor` unless that field is emitted by the CLI path you are using.
