# Contributing

Keep changes small, testable, and reversible.

Before opening a pull request:

```bash
python -m pytest -q
python scripts/public_safety_scan.py
git diff --check
```

Contribution rules:

- Do not add a second canonical state writer.
- Do not introduce a scheduler, daemon, or queue into core state logic.
- Put local transports in profiles or adapters.
- Add a regression test for every new action token, state schema change, or
  external receipt rule.
- Treat public/private hygiene as part of the test surface.
