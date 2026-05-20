# grove-autonomaton

The reference implementation of the
[Grove Autonomaton Pattern](https://the-grove.ai/standards/001).

This repository is a fork of
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent),
retrofitted to The Grove Foundation's canonical vocabulary and architectural
commitments. It is the codebase cited as the reference implementation in
the Grove Autonomaton Pattern publication.

## What this is

An Autonomaton is a self-authoring engine: it converts metered cloud
dependencies into permanent, zero-marginal-cost institutional assets that
get smarter, cheaper, and more private with every human interaction.

This fork inherits Hermes Agent's mature substrate — SQLite + FTS5 telemetry,
multi-platform gateway, seven terminal backends, the SKILL.md procedural
memory format compatible with the agentskills.io open standard — and adds
the architectural commitments that make it Autonomaton-conformant per
GRV-001:

- The five-stage invariant pipeline (Telemetry → Recognition → Compilation
  → Approval → Execution).
- Declarative Sovereignty Guardrails (~/.grove/zones.schema.yaml).
- Pattern-Based Approval at the Skill Flywheel boundary: agent-authored
  skills land in ~/.grove/skills/.andon/ and never execute until the
  operator runs `autonomaton sovereignty promote`.
- The Cognitive Router with Tier 0/1/2/3 dispatch.
- Feed-First telemetry with Grove-compliant audit trails.

## Upstream relationship

Hermes Agent is upstream. We rebase against tagged Hermes releases on a
deliberate cadence (see CONTRIBUTING.md). We do not push retrofit changes
upstream; the architectural divergence is the point of the fork.

## License

MIT. See LICENSE (Nous Research, 2025) and NOTICE (modifications,
The Grove Foundation, 2026).

## Status

PRIVATE during v0.1 development. Goes public when the Sovereignty Gate
is working, the Grove-register documentation is complete, and Jim has
run the Weekend MVP demo from GRV-001 §VIII end-to-end.

## Built by

The Grove Foundation · Indianapolis · jim@the-grove.ai
