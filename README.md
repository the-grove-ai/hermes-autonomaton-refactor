# The Hermes Autonomaton Refactor

![Status: Pre-Alpha](https://img.shields.io/badge/status-pre--alpha-critical.svg)

A fork of [NousResearch's Hermes Agent](https://github.com/NousResearch/hermes-agent) (~176K★), restructured around the [Autonomaton Pattern](https://the-grove.ai/standards/001). Released by [The Grove Foundation](https://the-grove.ai).

The EU AI Act's high-risk obligations — human oversight, record-keeping, transparency, robustness — require architectural answers, not bolt-on compliance layers. This fork demonstrates that those answers can be applied to an existing, capable agent framework without replacing it. We took NousResearch's Hermes Agent, moved state, memory, and tool execution out of the agent, and handed them to a deterministic dispatcher. The language model still does what it does well — reasoning, judgment, creativity. It just no longer owns the run.

The result is a governed agent that satisfies the structural requirements emerging from the EU AI Act and the broader regulatory landscape, while preserving the full capability of the upstream framework. The fork runs on GCP Compute Engine for 24/7 availability, supports model swaps via configuration (Anthropic, Gemma 4 via MLX/Ollama, DeepSeek, any OpenAI-compatible provider), and gets smarter with use — observing patterns, proposing skills, and ratcheting confirmed work to cheaper tiers, all under operator authority.

> [!WARNING]
> **Pre-Alpha / Active Research Implementation**
> This fork is heavily under active development. The core agent "god object" has been successfully replaced with a deterministic dispatcher, backed by a green suite of 24,000+ tests. However, this is a **pre-alpha** reference implementation. Expect bugs, breaking changes, and shifting APIs as the architecture matures toward production. This is a proof-of-concept for the Autonomaton Pattern, not yet a production-ready framework. Bug reports and suggestions are welcome — see Contributing below.

---

## The Architecture the Industry Is Converging On

The three most consequential players in agent infrastructure are all moving toward the dispatcher-first design. LangChain rebuilt itself on LangGraph's state-machine runtime. Microsoft is migrating AutoGen to typed graph workflows. Temporal's durable-execution model treats the LLM as an ephemeral activity, never the owner. Independent teams, who had never seen the Autonomaton Pattern spec, arrived at the same five-stage loop — telemetry, recognition, compilation, approval, execution.

But the frameworks stopped at the dispatcher.

The [Autonomaton Pattern](https://the-grove.ai/standards/001) goes further: a model-independent architecture that gets cheaper, more local, and more sovereign with use — as confirmed patterns ratchet from frontier API calls to smaller local models to deterministic rules that need no inference at all. A declarative governance layer where domain experts, not just developers, control the system through configuration they can read and revise. Self-evolution that proposes routing changes through the same approval gate it enforces on everything else. And a [sovereign node declaration protocol](https://the-grove.ai/standards/004) that makes the whole pattern composable and portable across independent nodes. The independent frameworks built the circuit breaker. The Autonomaton specifies the operating layer above it.

---

## Pre-Alpha Status

This is a working, exploratory fork under active development. The core governance surgery is complete and the test suite is holding the line, but this is a pre-alpha reference implementation. We are actively proving out the architecture. Rough edges, unimplemented features, and bugs are guaranteed.

| Layer | Status | Notes |
|---|---|---|
| Dispatcher pipeline | ✅ Shipped | Five-stage pipeline. Generator-shaped agent loop. Bidirectional Intent Protocol. |
| Cognitive Router | ✅ Shipped | T0–T3 tier routing. 15-intent taxonomy. T1 default floor. |
| T0 Pattern Cache | ✅ Shipped | Scanner, compiler, promotion, deterministic execution path. Correction-driven auto-demotion. A confirmed pattern resolves with no model call. |
| Zone-based sovereignty | ✅ Shipped | Green/Yellow/Red zones. Hierarchical rules. Regex fail-hard. |
| Kaizen concierge UX | ✅ Shipped | First-person, active-voice governance prompts. "I'd like to run the weather skill — your call before I continue." Butler register, not security guard. |
| Telegram governance | ✅ Shipped | Inline keyboard buttons for the four-choice prompt and post-execution promotion. Tappable on mobile. |
| Flywheel proposal queue | ✅ Shipped | CLI: `autonomaton flywheel list/show/approve/reject`. Async pattern synthesis stages proposals for conversational surfacing. |
| Declarative prompt composition | ✅ Shipped | 18 sections as declarative providers. Sub-50ms compose. |
| Feed-first context loop | ✅ Shipped | Intent store queries. Contextual preamble. |
| Agent purity | ✅ Shipped | Four axes substrate-free: construction, session, memory, classification. |
| Model independence | ✅ Shipped | Anthropic, OpenAI, Ollama, oMLX (Gemma 4, DeepSeek, etc.). Config swaps, not code changes. |
| Tool registry | ✅ Shipped | Dispatcher-owned. Module-level singleton deleted. |
| Skill authoring pipeline | ✅ Shipped | Operator-initiated ("build me a skill for X") and system-detected (pattern synthesis). Quarantine → try-once → promote, all in-conversation. `invoke_skill` governance hook. |
| GCP hosting | ✅ Shipped | Compute Engine deployment scripts. Provision, setup, deploy, watchdog. IAP-only SSH. Tailscale mesh for dashboard access. |
| Web dashboard | ✅ Shipped | Served via Tailscale private mesh. No public exposure. Config, keys, sessions, logs. |
| Daily-driver tools | ✅ Shipped | `ddgs` (DuckDuckGo) web search — keyless default; Tavily / Firecrawl / SearXNG / Exa available via config. Turn-0 affordances preamble. Capability-aware butler (names latent tools and offers to enable them). |
| Process lifecycle | ✅ Shipped | `hermes doctor --reap`/`--restart`. atexit MCP cleanup. macOS process-tree hardening. systemd on GCP. |
| `--strict` mode | 🔲 Planned | Enterprise gate preserving the upstream Hermes PR-driven review model: skills queue silently as proposals, operator reviews the diff via `hermes andon diff`, approves explicitly via `hermes andon promote`. Full audit trail. The default daily-driver mode promotes in-conversation (the Autonomaton spec); `--strict` adds ceremony for environments that require it. CLI review surface exists; full enforcement gating is a future sprint. |

**Test surface:** Governance suite: 1,284/1,284, green. Full upstream suite: ~24,700 collected, 24,290+ passing — the residual is documented environment-gated skips (discord.py 2.x mocks, macOS Keychain, Linux systemd, PTY) and upstream-divergence skips, plus a pre-existing behavioral backlog under continuing audit. Live CLI integration: T1–T31. Telegram gateway: 510 tests.

**What's battle-tested vs. what's deployed.** The CLI and Telegram are the primary proof points — the governance surgery, Kaizen UX, skill authoring pipeline, and Flywheel have been tested end-to-end on both surfaces. The web dashboard is deployed and accessible (via Tailscale) but has known issues: the skills page reads an incorrect path, the models page is unaware of the Cognitive Router, and the UI still carries upstream branding. Other upstream gateway surfaces (Slack, Discord, WhatsApp, Matrix) exist in the codebase but have not been tested against the Grove governance layer. These surfaces are in line for exploration, testing, and bug fixes — contributions and bug reports are especially welcome here.

---

## What Changed From Upstream

Hermes is a capable, battle-tested framework: model-agnostic transport, multi-platform gateway, session persistence, skills, memory. NousResearch built genuinely good software. None of that is Grove's work.

Grove's contribution is narrow. We relocated ownership of the run. Where the framework — like nearly every agent framework — centers the lifecycle on the agent object, the fork puts a deterministic dispatcher in front of it. The model is demoted from owner to advisor. State, routing, approval, and execution become properties of the dispatcher — inspectable, testable, and governable.

The agent class is physically unable to touch state. That constraint is the whole intervention.

**What the dispatcher owns:**

- **Telemetry** — every intent, every classification, every disposition, every tool result
- **Recognition** — the Cognitive Router classifies and routes to the cheapest sufficient tier
- **Compilation** — turns recognized intents into declared actions via `routing.config.yaml`
- **Approval** — zone-based sovereignty with Kaizen-mediated operator prompts
- **Execution** — tools fire through the dispatcher, never through the agent

**Three files and a loop.** `routing.config.yaml`, `zones.schema.yaml`, and structured telemetry. The fork makes these files the authority the agent answers to.

---

## Architecture

```
Operator query
│
▼
┌─────────────┐    classifies     ┌──────────────────┐
│  Dispatcher │ ───────────────►  │ Cognitive Router │
│  (owns the  │ ◄───────────────  │  (T0–T3 tiers)   │
│   pipeline) │    tier + model   └──────────────────┘
│             │
│             │    constructs     ┌──────────────────┐
│             │ ───────────────►  │ Agent (stateless)│
│             │ ◄───────────────  │  yields intents  │
│             │   ToolBatchYield  └──────────────────┘
│             │
│             │    zone check     ┌──────────────────┐
│             │ ───────────────►  │  Zones + Kaizen  │
│             │ ◄───────────────  │  (approve/deny)  │
│             │    disposition    └──────────────────┘
│             │
│             │    executes       ┌──────────────────┐
│             │ ───────────────►  │   Tool Executor  │
│             │ ◄───────────────  │ (P3: zero Agent  │
│             │      results      │   state access)  │
└─────────────┘                   └──────────────────┘
```

The agent yields intents. The dispatcher decides what happens. The operator holds authority over every protected action.

---

## Self-Authoring Skills

The Autonomaton builds its own tools — under operator authority. Two paths in, one pipeline out.

**Operator-initiated:** Say "build me a skill that researches influencers and logs them to Notion." The system collaborates on the design, scaffolds the SKILL.md into quarantine, runs a governed trial, and offers in-conversation promotion. One click to try, one click to keep.

**System-detected:** The Flywheel observes recurring tool sequences in the intent store. When a pattern appears consistently — same tools, same order, across multiple sessions — it synthesizes a SKILL.md proposal via T3 and surfaces it conversationally: "I noticed you regularly check GitHub repos before meetings. I drafted a skill to speed this up — want to try it?"

Both paths end at the same gate: quarantine → governed trial run (Yellow zone) → post-execution promotion prompt (Promote / Not yet / Never). The system drafts. The operator commits. The architecture guarantees this — it is not a policy the model can ignore.

Every skill the system compiles ratchets work downward: what started as a multi-tool frontier interaction becomes a reusable, governed, locally-executable capability. The cost curve bends with use.

---

## EU AI Act Alignment

The EU AI Act's high-risk obligations map directly onto the dispatcher architecture:

| EU AI Act Requirement | Autonomaton Implementation |
|---|---|
| **Human oversight** — understand, intervene, stop | Approval gate. Four-choice Kaizen prompt. Zone-based sovereignty. |
| **Record-keeping** — reconstruct decisions | Structured telemetry. Every action is a declared, replayable record. |
| **Transparency** — intelligible behavior | Model returns inspectable intents, not opaque actions. Governance lives in config files a human can read. |
| **Robustness** — predictable, reliable operation | Deterministic stages surround a single, constrained model call. Fail-hard on malformed governance. |

The alignment is structural, not cosmetic. Both the Autonomaton Pattern and the Act descend from the same older idea: autonomy is acceptable only when a human stays in the loop and the record stays intact. Architecture does not discharge compliance on its own — conformity assessment and process still apply — but it supplies the technical backbone those obligations rest on.

The high-risk obligations timeline was deferred to December 2027 (via the Digital Omnibus provisional agreement of May 2026), but the substance did not soften. The deferral is runway, not relief. This fork is the head start.

---

## The Kaizen UX

When the agent wants to perform a protected action, the operator sees this:

```
I'd like to run the weather skill — your call before I go ahead.

  [1] Just this once
  [2] For the rest of this session
  [3] Always — I'll remember it
  [4] Not this time
```

First person, active voice. The butler names the action, explains why governance is intervening, and offers clear options. No zone names, no regex patterns, no jargon.

On Telegram, the same prompt renders as inline keyboard buttons — 🟢 Always / 🟡 This session / 🟠 Just once / 🔴 Not now — and the post-execution promotion as 🟢 Promote it / 🟡 Not yet / 🔴 Never. Same governance, same dispositions, one tap on mobile.

When the agent hits a hard boundary:

```
The command `sudo rm -rf /var/log` needs privileges I deliberately
don't hold: sudo / su / doas stay with you, never me. Run it yourself
in a terminal that has your credentials, then paste back anything I
need to keep going.
```

Governance that recedes into competence. Invisible on normal turns, present when it matters, always offering options.

---

## Tier Routing and Model Independence

The Cognitive Router sends each request to the cheapest tier that can handle it:

| Tier | Default Binding | Role |
|---|---|---|
| T0 — Pattern Cache | Deterministic | Confirmed patterns. No model call. Scanner identifies stable T1 patterns; the operator approves via flywheel. Correction-driven auto-demotion. |
| T1 — Cheap Cognition | Haiku-class | Routine, well-understood requests. The daily driver. |
| T2 — Premium Cognition | Sonnet-class | Novel or moderately complex work. |
| T3 — Apex Cognition | Opus-class | High-stakes, ambiguous, or creative. |

T1 is the default floor. Most daily-driver queries — memory, retrieval, conversation, factual lookup, translation, summarization — run here at ~$0.002 per interaction. T2 and T3 are escalation tiers for knowledge work and architectural reasoning.

All bindings are config swaps in `routing.config.yaml`. Local models (Gemma 4 via MLX, Ollama, DeepSeek, any OpenAI-compatible endpoint) work at any tier. Model independence is structural, not cosmetic — swap the binding, keep the governance.

As the system learns, confirmed patterns ratchet downward: from frontier API calls, to smaller local models, to deterministic rules that need no inference at all. Inference cost falls as the system learns. Autonomy compounds at the node.

---

## GCP Deployment

The fork ships with production deployment scripts for Google Cloud Platform:

```bash
# Provision the VM (e2-small, Ubuntu 24.04, IAP-only SSH)
bash scripts/provision-vm.sh

# SSH in and set up the environment
bash scripts/setup-vm.sh

# Deploy code updates from your Mac
bash scripts/deploy.sh
```

The VM runs the Telegram gateway and web dashboard as systemd services with automatic restart, a watchdog cron, and persistent state on a dedicated disk. SSH is IAP-only — no public ports exposed. The dashboard is accessible via Tailscale private mesh.

Swapping models on the VM is a config edit: change the tier binding in `~/.grove/routing.config.yaml`, restart the service. Gemma 4, DeepSeek, or any Ollama-served model slots in without code changes.

Cost: ~$15/month for the base VM. The Reverse Tax bends this further as patterns compile to T0.

---

## Quick Start

Requires Python 3.11 or newer.

```bash
# Clone the fork
git clone https://github.com/the-grove-ai/hermes-autonomaton-refactor.git
cd hermes-autonomaton-refactor

# Set up the environment
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Add your API keys
cp .env.example .env
# Edit .env — at minimum, set one provider key (Anthropic, OpenAI,
# OpenRouter, or a local Ollama endpoint).

# Run
hermes chat
```

On first invocation, `~/.grove/` is seeded automatically with operator copies of `routing.config.yaml` (tier bindings) and `zones.schema.yaml` (sovereignty rules). Edit those files to retune routing or governance — no restart required for most changes.

The CLI binary is `hermes` (upstream compat) with `autonomaton` as an alias. Both work.

---

## Upstream Credit

This is a derivative work. [NousResearch](https://nousresearch.com) built the Hermes Agent under the MIT license. Grove applied an architecture to it. The upstream `LICENSE` and copyright notice travel with the code. Where this document describes what "the fork" does, it describes Grove's modifications; the underlying framework remains the work of its original authors.

---

## The Standard

This fork implements the [Autonomaton Pattern (GRV-001)](https://the-grove.ai/standards/001), published under CC BY 4.0 at [the-grove.ai](https://the-grove.ai). The standard describes the architecture in the abstract. This fork makes it executable.

For the full argument behind the design, read [The Autonomaton Pattern: A Brief for Technical Review](https://the-grove.ai/hermes-autonomaton).

---

## License

- **Code:** MIT. Upstream (NousResearch) + Grove modifications. Both copyright notices in LICENSE.
- **The Autonomaton Pattern (GRV-001):** CC BY 4.0. Spec and code carry different licenses by design.

---

## Contributing

This fork follows the Foundation Loop sprint methodology. One sprint, one purpose, one set of writes. Contributions that align with GRV-001 and the sprint discipline are welcome.

Found a bug? Have a suggestion? Open an issue on GitHub or reach out directly. The architecture is under active development — feedback from engineers who've built similar systems is especially valuable.

For questions, architecture discussion, or collaboration: questions@the-grove.ai

---

*Architecture is the guarantee; policy is the promise. Model independence is not theater.*
