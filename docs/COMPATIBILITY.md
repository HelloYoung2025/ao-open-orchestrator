# Compatibility Boundary

This release intentionally supports one schema and one contract version:

- state schema: `schema_version = 1`;
- contract version: `version = 1`.

Future releases may add explicit migrations or shims. Until such support is
implemented and tested, if a runtime changes transport, environment variables,
state shape, or action vocabulary, the correct outcome is an explicit
compatibility failure, not a silently bypassed gate.

Compatibility/preflight currently covers root identity, contract
version/action vocabulary, state schema, and unsupported live obligations.
Caller identity is enforced later on receipt submission and authorization paths.

Current caller identity variables:

- `AO_CALLER_TYPE`;
- `AO_SESSION_ID`.

Profiles may shim future runtime variables into these names, but the shim must
be explicit and covered by tests.

These environment variables are local binding hints, not a general
authentication system. Do not expose `ao-state-writer` or adapter commands as
network services without an additional authentication and authorization layer.
