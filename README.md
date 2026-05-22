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
  operator runs `autonomaton andon promote`.
- The Cognitive Router with Tier 0/1/2/3 dispatch.
- Feed-First telemetry with Grove-compliant audit trails.

## Upstream relationship

Hermes Agent is upstream. grove-autonomaton derives from upstream tag
v2026.5.16 (Hermes Agent v0.14.0) and rebases against tagged Hermes
releases on a deliberate cadence (see CONTRIBUTING.md). Retrofit changes
are not pushed upstream; the architectural divergence is the point of
the fork.

### Modifications

The v0.1 retrofit adapts the upstream codebase to the Grove Autonomaton
Pattern: the Cognitive Router, the Andon quarantine and Pattern-Based
Approval, declarative Sovereignty Guardrails, the `~/.grove`
configuration namespace, identity composition, and the `autonomaton`
CLI. The upstream substrate — SQLite + FTS5 telemetry, the multi-platform
gateway, the terminal backends, the agentskills.io SKILL.md format — is
preserved; the fork changes what the Pattern requires and no more.
MODIFICATIONS.md enumerates the divergence from the fork point;
CHANGELOG.md records it sprint by sprint.

## License

MIT. See LICENSE (Nous Research, 2025) and NOTICE (modifications,
The Grove Foundation, 2026).

## Status

PRIVATE during v0.1 development. Goes public when the Sovereignty Gate
is working, the Grove-register documentation is complete, and Jim has
run the Weekend MVP demo from GRV-001 §VIII end-to-end.

## Built by

The Grove Foundation · Indianapolis · jim@the-grove.ai
