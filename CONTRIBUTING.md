# Contributing to The Hermes Autonomaton Refactor

This is a Grove Foundation reference implementation. The architectural
commitments are non-negotiable; the implementation is open to refinement.

## Before you contribute

Read GRV-001: The Autonomaton Pattern — https://the-grove.ai/standards/001
The architectural commitments below derive from it.

## The Foundation Loop

This project develops through the Grove Foundation Loop: one sprint, one
purpose, one set of writes, gated by discovery → design lock → execution.
Sprint-structured PRs follow it. Read the full methodology — the three-
artifact contract, the gate sequence, the Andon halt discipline, and
copy-paste templates — before submitting:

docs/contributing/FOUNDATION_LOOP.md

## Vocabulary

The Grove canon governs operator-facing strings, documentation, and
module docstrings. See website/docs/style/vocabulary.md (added in
`vocab-retrofit-docs-v1`).

Internal identifiers and external-spec property names (e.g., schema.org
`publisher`) follow their own namespaces and are preserved as-is.

## Code style and process

For Python style, test conventions, PR cadence, this fork inherits
upstream Hermes' practices. See
https://github.com/NousResearch/hermes-agent/blob/main/CONTRIBUTING.md
for conventions we have not deliberately diverged from.

## Sovereignty discipline

No PR is merged that:
- Bypasses Stage 4 (Approval) for any zone-classified operation.
- Allows agent-authored skills to enter the active skill set without
  passing through ~/.grove/skills/.andon/ and an explicit human
  promotion act.
- Hardcodes a zone boundary that should be declarative.
- Embeds a specific model identifier in engine code outside the
  Cognitive Router's tier configuration.

These are not guidelines. They are how this codebase remains the
reference implementation.

## License

MIT. By contributing you agree your contributions are MIT-licensed.
No CLA. Per Grove's published standards, the architectural patterns
themselves are CC BY 4.0 and may be re-implemented without permission.
