# Compatibility Boundary

The compatibility layer intentionally supports one schema and one contract
version at a time:

- state schema: `schema_version = 1`;
- contract version: `version = 1`.

If a future runtime changes transport, environment variables, state shape, or
action vocabulary, the correct outcome is an explicit compatibility failure,
not a silently bypassed gate.

Current caller identity variables:

- `AO_CALLER_TYPE`;
- `AO_SESSION_ID`.

Profiles may shim future runtime variables into these names, but the shim must
be explicit and covered by tests.
