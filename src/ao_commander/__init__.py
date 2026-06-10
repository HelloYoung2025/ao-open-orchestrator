"""Gate-aware commander helper: review-package builder + worker/reviewer orchestration.

Brand-neutral companion to the ``ao_state_writer`` engine. It builds verifiable
review packages (manifest + SHA256SUMS + secret scan + zip integrity), classifies
the current canonical-file gate into an advisory Brief, and drives a pluggable
worker (default: a Codex CLI session) and a pluggable reviewer (default: a
fail-loud manual/staged submitter; optional operator-configured submit command).

This package is standalone: it does NOT import ``ao_state_writer`` and does NOT
require a project contract. Per-project wiring lives in ``.commander/commander.toml``.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
