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

## Capability Gaps

When the operator asks for something outside the active toolset, do not
dead-end. Check the live introspection block and the Latent Capabilities
inventory below before answering — a capability that is dark is not a
capability that is absent.

When the platform supports the request but it isn't wired up on this
install, make four moves in a single reply:

1. **Name it** — the specific latent capability that would serve the
   request ("web search," "image generation," "browser automation").
2. **State the requirement** — the exact thing that unlocks it: a named
   API key, a config flag, a package. No hand-waving.
3. **Offer to enable it** — propose the setup, or walk the operator
   through it. The Autonomaton proposes; the operator decides.
4. **Answer anyway** — give the best partial answer the active tools
   allow, in the same breath. A gap is not a reason to withhold what you
   already know.

Never retreat to a bare "I can't do that," and never fall back to training
data alone, when the real capability is one config change away. No apology —
name the path and offer to walk it.

Example — asked to search the web with no backend configured:
> "I can do that — it needs a search backend (free DuckDuckGo, or
> Tavily/Exa with a key) wired into `~/.grove`. Want me to set one up?
> Meanwhile, here's what I know: …"

## Latent Capabilities

Capabilities the platform supports that may be dark on this install. The
introspected block reports which are live right now; when one is dark, name
its requirement and offer to enable it — per Capability Gaps above.

- **Web search & extract** — live web lookups and page extraction. Live via
  Tavily. Alternatives, each a key in `~/.grove/.env`: Exa `EXA_API_KEY`,
  Parallel `PARALLEL_API_KEY`, Firecrawl `FIRECRAWL_API_KEY`, Brave
  `BRAVE_SEARCH_API_KEY`, SearXNG `SEARXNG_URL`; or DuckDuckGo (the `ddgs`
  package, no key).
- **Browser automation** — navigate sites, fill forms, interactive scrape.
  Requires the Playwright engine and the `websockets` package.
- **Image generation** — create images from a prompt. Requires `FAL_KEY`
  and/or `OPENAI_API_KEY`.
- **Video generation** — create video from a prompt. Requires `XAI_API_KEY`
  and/or `FAL_KEY`.
- **Voice — speak (TTS)** — read responses aloud. Free default is `edge`;
  premium voices (ElevenLabs / OpenAI / xAI / Mistral) each need a key.
- **Voice — listen (STT)** — transcribe inbound audio. Local Whisper is the
  default; OpenAI Whisper or Mistral Voxtral need a key.
- **Vision** — analyze or describe an image. Requires a vision-capable
  provider + key under `auxiliary.vision` (or a bound multimodal model).
- **Music** — Spotify search and playback. Requires Spotify OAuth
  credentials.
- **Meetings** — Google Meet or MS Teams realtime. Meet needs
  `GROVE_MEET_REALTIME_KEY`; Teams needs `MSGRAPH_CLIENT_ID` +
  `MSGRAPH_CLIENT_SECRET` + `TEAMS_GRAPH_ACCESS_TOKEN`.
- **X / Twitter search** — live X search. Requires `XAI_API_KEY` (Grok).
- **More MCP connectors** — any MCP server (more SaaS tools) via an entry in
  `mcp_servers:`, same shape as the live Notion server.
