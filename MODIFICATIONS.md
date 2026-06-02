# Modifications

The Hermes Autonomaton Refactor is a derivative work of Hermes Agent
(https://github.com/NousResearch/hermes-agent), forked at tag
v2026.5.16 (v0.14.0).  Repository:
https://github.com/the-grove-ai/hermes-autonomaton-refactor.  This
file enumerates the modifications The Grove Foundation has made since
the fork point, per MIT-license convention.

Upstream code is licensed under the MIT License, Copyright (c) 2025
Nous Research. Modifications are Copyright (c) 2026 The Grove
Foundation, also under the MIT License. See LICENSE and NOTICE.

## Added

- `grove/` — the Grove package: zone classifier, Andon quarantine and
  sovereignty CLI, Cognitive Router, per-turn classifier, cellar
  retrieval substrate, identity composition, telemetry, and the
  `grove/kaizen/` recommender package.
- `config/zones.schema.yaml` — the declarative zone schema.
- `config/routing.config.yaml` — Cognitive Router tier bindings and
  declarative routing rules.
- `config/identity/` — reference identity templates.
- `skills/upstream-sync-register/` — the editorial-register skill for
  the upstream-divergence ledger.
- Grove provenance frontmatter (`created_by`, `zone`, `tier`,
  `register`, `lineage`, `promotion_history`) on bundled and
  agent-authored skills.
- `CHANGELOG.md` and `MODIFICATIONS.md`.

## Changed

- Configuration namespace: data directory `~/.grove` (was `~/.hermes`),
  environment variables `GROVE_*` (were `HERMES_*`), telemetry store
  `telemetry.db` (was `state.db`).
- The CLI binary is `autonomaton`; `hermes` is retained as a
  backward-compatible alias on the same entry points.
- Skill creation routes agent-authored skills to the Andon quarantine;
  promotion is an explicit operator act.
- Model selection runs through the Cognitive Router; each turn is
  classified and routed to a tier.
- HTTP `User-Agent`, `X-Title`, and `X-BILLING-INVOKE-ORIGIN` headers
  identify grove-autonomaton.
- Operator-facing vocabulary aligned to the Grove canon.

## Removed

- Two bundled skills inconsistent with the Pattern's sovereignty
  commitments: `red-teaming/godmode` and `mlops/inference/obliteratus`.

## Preserved

- The MIT license and the upstream copyright notice.
- The upstream substrate: SQLite + FTS5 telemetry, the multi-platform
  gateway, the terminal backends, and the agentskills.io-compatible
  SKILL.md format.
- Module, package, and class names internal to the upstream codebase;
  renamed only where operator-facing.

For the sprint-by-sprint history, see CHANGELOG.md.
