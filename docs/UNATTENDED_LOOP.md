# Unattended Loop Principle

"Unattended" means automatic progress to the next safe boundary. It does not
mean bypassing unknown business logic or owner-only gates.

Every tick should land in one of three buckets:

1. continue a supported action;
2. enter typed repair or convergence;
3. stop at an explicit owner-only gate.

Silent stalls are treated as design failures. A fail-closed state is valid only
when it names the blocked action, forbidden actions, allowed repair actions,
repair obligation, and evidence anchor.
