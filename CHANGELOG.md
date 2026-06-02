# Changelog

All notable changes to The Hermes Autonomaton Refactor. This fork
derives from Hermes Agent v0.14.0 (upstream tag v2026.5.16); the
entries below record the v0.1 retrofit to the Autonomaton Pattern
(GRV-001).

## v0.1 — Grove Autonomaton Pattern retrofit

Sprint by sprint:

- **fork-bootstrap** — Established the fork with attribution files
  (NOTICE, LICENSE, README, CONTRIBUTING) and the Grove editorial register.
- **config-paths-retrofit** — Data directory `~/.hermes` → `~/.grove`,
  `HERMES_*` environment variables → `GROVE_*`, telemetry store
  `state.db` → `telemetry.db`.
- **zones-schema-design** — Added the declarative zone schema,
  `config/zones.schema.yaml`.
- **zones-schema-implementation** — Added the `grove/` package and the
  `ZoneClassifier` (loader, matcher, atomic reload).
- **andon-design** — Specified the Andon quarantine, sovereignty CLI,
  and telemetry contracts.
- **jidoka-andon-implementation** — Agent-authored skills route to the
  `~/.grove/skills/.andon/` quarantine and execute only after an
  explicit operator promotion; `sovereignty_decision` telemetry records
  every promote / reject / revoke.
- **kaizen-foundation** — Established the `grove/kaizen/` package.
- **persona-soul-retrofit** — Added `config/identity/` templates and
  `grove/identity.py`, composing operator identity into the system prompt.
- **vocab-retrofit-core** — Aligned operator-facing strings to the
  Grove canon.
- **cognitive-router-naming** — Added `config/routing.config.yaml` and
  the `CognitiveRouter` config loader.
- **cognitive-router-tiering** — Added `route()` and the four-tier
  dispatch (T0–T3) wired into CLI model selection.
- **haiku-telemetry-normalization** — Added per-turn classification
  (`grove/classify.py`) feeding routing and the telemetry log.
- **rag-substrate** — Added the cellar retrieval substrate
  (`grove/cellar.py`, FTS5 index) with per-turn context retrieval.
- **soul-kaizen-wiring** — Made Kaizen identity-aware; skill proposals
  carry soul-alignment metadata.
- **cognitive-router-functional** — Per-turn routing: every turn is
  classified and routed to its tier; declarative `routing_rules`.
- **skill-frontmatter-extension** — Added `tier`, `register`, `lineage`,
  and `promotion_history` to skill frontmatter.
- **cli-rename** — The CLI binary is `autonomaton`; `hermes` is retained
  as a backward-compatible alias.
- **reference-skills-curation** — Grove provenance frontmatter on every
  bundled skill; two skills inconsistent with the Pattern's sovereignty
  commitments removed; the upstream-sync-register skill installed.
- **attribution-and-license** — Attribution audit: NOTICE updated to the
  v0.1 modification set; HTTP User-Agent and attribution headers identify
  grove-autonomaton; CHANGELOG and MODIFICATIONS.md added.
