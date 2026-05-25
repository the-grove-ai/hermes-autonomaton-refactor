# Affordances

This file declares the capability landscape — what external systems
the Autonomaton can reach, which slash commands are available, how
the Cognitive Router routes work across tiers, and what's indexed in
the operator's cellar. It is graceful-tier: missing is fine, the
Autonomaton runs with a generic capability picture and falls back to
runtime introspection.

The composition layer reads this file at session start and pairs it
with a live introspection block (connected MCPs right now, current
router bindings, current slash command set, cellar index status).
The static text below gives semantic orientation — when to reach for
what. The introspected block gives live state.

This file replaces turn-1 capability rediscovery. Edit it to make the
Autonomaton's capability landscape sharp for your work.

## Connectors

External systems the Autonomaton can reach via MCP servers, native
tools, or operator-provided credentials. Examples:

- **Google Workspace** (gmail, calendar, drive, docs, contacts) —
  for scheduling, correspondence, and document operations.
- **Notion** — for project context, wiki pages, and the
  workstream's planning artifacts.
- **GitHub** — for repository operations and issue tracking.

Customize this list for your install. The introspected block at
session start names the connectors actually active right now.

## Slash Commands

The canonical verb surface for in-session interaction:

- `/andon` — operate the sovereignty gate (Sprint 06a).
- `/index` — manage the cellar FTS index (Sprint 13).
- `/tier` — override the Cognitive Router's tier choice for the
  session.
- `/context` — print the per-section token breakdown for the current
  turn.
- `/register` — switch the active register overlay.
- `/why` — show the routing decision for the most recent turn
  (webui only).
- `/summary` — show the session routing summary (webui only).

The introspected block at session start gives the full live verb
list grouped by category.

## Cognitive Router

Work routes across four tiers based on classified intent and
confidence:

- **T0 Pattern Cache** — deterministic recall from the cellar.
  Instant, free. Grows with use.
- **T1 Cheap Cognition** — a small fast model for triage, simple
  factual lookups, casual exchange.
- **T2 Premium Cognition** — the default. Real reasoning, code,
  drafting, tool-using work.
- **T3 Apex Cognition** — the strongest model. Multi-step planning,
  novel synthesis, architecture-level reasoning.

The router's tier bindings live in `~/.grove/routing.config.yaml`.
The introspected block names the current model bound to each tier.

## Cellar

The cellar (`~/.grove/`) holds the operator's promoted and proposed
skills, identity files, zone and routing config, and memory. The
FTS5 index at `~/.grove/index/cellar.db` enriches each turn with
the most relevant cellar context — answers come from the operator's
actual files, not training data.

Rebuild with `hermes index rebuild`. Status (path, document count)
appears in the introspected block.

## When to Reach For What

Procedural guidance — match the work to the right combination of
register, tier, and surface.

- **Broadcasts and public-facing text** → Standards Register +
  cellar RAG + T2/T3. No villain in plumbing; name design and
  consequence.
- **Ledger entries and divergence registers** → Editorial Register
  + Standards reasoning cap (three sentences for REJECTION
  REASONING). Preserved commitment / canonical citation /
  structural consequence.
- **Direct session work with the operator** → Operator Register +
  T1/T2 routing. Terse, executor mode. Eight-word status sentences.
  One blocking question per turn or none.
- **System-administration commands** → terminal tool + the
  Sovereignty Gate. Red-zone actions surface for operator approval,
  never auto-execute.
- **Skill creation** → propose into `~/.grove/skills/.andon/`.
  Kaizen surfaces patterns the operator promotes via `hermes andon
  promote`. The Autonomaton never promotes its own skills.
