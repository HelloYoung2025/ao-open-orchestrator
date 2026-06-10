# Security Policy

## Supported Versions

Only the current `main` branch is supported during the alpha period.

## Reporting a Vulnerability

Please open a GitHub security advisory or a private issue with:

- affected commit;
- reproduction steps;
- expected fail-closed result;
- observed unsafe result;
- relevant state/proposal snippets with secrets removed.

## Threat Model

The project is designed around local-first orchestration, not remote trust. It
assumes adapters and subscription CLIs are profile-owned and
must prove their outputs through local artifacts.

The state writer rejects:

- self-declared external-review actors without trusted caller identity;
- escalated review receipts that are not bound to package hash, nonce, artifact hash, and
  gate proposal;
- artifact refs outside `artifact:reports/...`;
- unsupported state schema or contract versions;
- unknown action vocabulary;
- non-canonical roots.

`AO_CALLER_TYPE` and `AO_SESSION_ID` are local binding hints, not a general
authentication system. Do not expose `ao-state-writer`, `escalated-review-actuate`, or
adapter commands as network services without an additional authentication,
authorization, audit, and OS-level isolation layer.

Do not publish raw `.omx` state, local transcripts, reviewer conversation ids,
session ids, account-bound logs, or machine-specific paths.
