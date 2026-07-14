# The Hermes Autonomaton Refactor

![Status: Pre-Alpha](https://img.shields.io/badge/status-pre--alpha-critical.svg)
![Tests: 3,497 passing](https://img.shields.io/badge/grove_suite-3%2C497_passing-brightgreen.svg)

<p align="center">
  <a href="https://the-grove.ai">The Grove Foundation</a> ·
  <a href="https://the-grove.ai/standards">Published Standards</a> ·
  <a href="https://the-grove.ai/lambda">Λ Watch</a> ·
  <a href="https://the-grove.ai/observations">Observations</a> ·
  <a href="https://the-grove.ai/about">About</a> ·
  <a href="https://the-grove.ai/autonomaton">Run the Pattern ↗</a>
</p>

What started as an internal summer challenge — take [NousResearch's Hermes Agent](https://github.com/NousResearch/hermes-agent), remove the god-object at its center, and replace it with an autonomatonic spine — took on a life of its own. A hundred sprints later, the fork represents a reference implementation of the [Autonomaton Pattern (GRV-001)](https://the-grove.ai/standards/001): a governed agent architecture where the model reasons but never owns the run. Much of what was learned here has been folded back into the standard — clarity improvements, ambiguity removals — in the Q2 update now live on the site.

## The Idea

Most AI agents reset to zero every session. They don't remember what you taught them, don't learn from what worked, and can't propose improvements to their own behavior. The operator carries the full cognitive load, re-teaching the same lessons on repeat. The agentic loop is just that: a treadmill.

This system inverts that. An Autonomaton observes how you work, proposes its own improvements, and — only with your approval — rewrites its own configuration: routing tables, capability records, skills. Approved patterns ratchet down from frontier API calls toward cheaper, faster, more local execution. The system gets smarter, more capable, and less expensive with use as a structural property of the architecture. This repository is where you can check that claim for yourself against a working implementation.

Here's the guarantee, stated plainly: **everything the system learns is stored in your home directory, and a software update cannot touch it.** The code lives in the repo. Your data — patterns, approvals, routing choices, accumulated knowledge — lives in `~/.grove/`. Updating the code replaces the repo and nothing else. There's no migration step to trust, no "we promise not to" policy. The update mechanically cannot reach your data, because your data isn't where updates happen. Check it yourself: `ls ~/.grove/`.

The separation is the same one Unix has used for fifty years: programs in one place, your configuration and data in another. The repo is the program. `~/.grove/` is yours. `git pull` all you want — it can't reach your side.

The entire control surface is three plain-text files and one pipeline. A routing config says which model handles what. A zones schema says which actions need your approval. A telemetry log records everything that happened. The pipeline reads them in order on every turn. There's no hidden policy engine and no behavior you can't trace to a line in one of those files — if you want to know why the system did something, the answer is in a file you can open.

## What Changed From Upstream

Upstream Hermes — like most agent frameworks — hands the loop to a model-powered agent object: the model decides, calls tools, holds state, and polices itself. This refactor performed one surgery and let everything else follow from it:

**The dispatcher owns the turn — not the model.** A deterministic pipeline — Recognition, Compilation, Approval, Execution, with Telemetry as the cross-cutting spine every stage writes into — carries each request from arrival to completion. The model is consulted inside that agentic pipeline as a pure reasoner: it receives composed context, yields intents, and holds nothing. It cannot touch state, disk, or tools directly, because no code path exists for it to do so. Governance stops being a set of instructions the model is trusted to follow and becomes a set of walls the model cannot reach around. Self-evolution doesn't breach these walls; it walks through them. When the system rewrites its own configuration, that write is itself an intent — proposed by the model, gated at Approval, executed by the dispatcher's governed writers. The system changes itself the same way it does everything else: with the operator holding the key.

```
              ┌─────────────────────────────────────────────┐
              │              DISPATCHER — owns the turn      │
              │                                             │
   operator ──► Recognition ─► Compilation ─► Approval ─► Execution ──► effects
              │  (Cognitive     (Agent         (Zone         (governed  │
              │   Router,        reasons,       gate:         tools,    │
              │   T0–T3)         yields         G/Y/R)        walls)    │
              │                  intents)                               │
              │  ═══════════ Telemetry — every stage writes ══════════  │
              └─────────────────────────────────────────────┘
                        │                              ▲
                        ▼                              │
              ~/.grove/  (operator's state)   Flywheel: observe →
              intents · approvals · skills    propose → operator
              knowledge · routing state       approves → evolve
```

## The Lineage

The Autonomaton Pattern is not rocket science. It's a synthesis of established ideas from computer science, industrial engineering, and the PC revolution's deepest architectural insight: that coherent products are expressions of coherent worldviews. The generation that built the personal computer treated architecture as philosophy expressed through constraint. The Macintosh was not a collection of features; it was a coherent set of structural commitments. The Autonomaton Pattern returns to this old-school design axis deliberately — and the coherence pays out in ways that were designed in, not bolted on:

**Model flexibility fell out of a founding constraint.** Capability Agnosticism — dispatch to tiers, never to models — means every tier binding is one line of config across any OpenAI-compatible provider. The pattern assumed a many-model world before the many-model world arrived.

**The telemetry was always a training corpus.** Feed-first design means the labeled data was accumulating from the first sprint — see The Ratchet below.

**The quality loop is Toyota, not novelty.** Jidoka detects abnormality in-flight; Andon stops the line and surfaces it; Kaizen aggregates recurrence into proposals; the operator directs. Production quality control, applied to production cognition — and this repository's own development history is run through the same loop it ships.

## The Ratchet

The compounding isn't a metaphor. It's a pipeline with a direction.

Every turn through the system leaves a complete structured intent record — what was asked, how it was classified, which tier handled it, what tools fired, and what the operator did with the result. This is the **feed-first** design: telemetry isn't logging bolted onto an agent, it's the spine the agent hangs off of. The dispatcher owns the turn, so the record is complete by construction — there is no path through the system that doesn't leave one.

That record stream drives three mechanisms, each shipped and operating:

**Patterns migrate down — and "down" is a cliff, not a slope.** The Cognitive Router dispatches every request to the cheapest tier that can handle it — T3 frontier reasoning down to T0 deterministic rules that need no inference at all. Every tier binding is one line of config. Bind a tier to a frontier API, to a hosted open-weight model through a broker like [OpenRouter](https://openrouter.ai) (300+ models behind one endpoint), or to an open-weight model running on your own hardware via MLX or Ollama. At published per-token prices, hosted open-weight models run as much as 100X cheaper than frontier APIs for the same request — and local inference has no meter at all. When telemetry shows a pattern succeeding consistently at a tier, the Flywheel proposes demotion to a cheaper one; when the operator corrects a result, the pattern demotes back up automatically. Confirmed patterns compile to the T0 Pattern Cache and resolve with no model call whatsoever.

The economics compound in both directions at once: the *routing* gets cheaper as patterns prove out, and the *bindings* get cheaper as open-weight models improve underneath you. You don't wait for a vendor to cut prices. You re-bind a config line. The ratchet turns one way — toward cheaper, faster, more local — unless the operator resets it.

**Evidence accumulates per skill, per model.** Binding telemetry tracks how each capability performs on each model it's tried on, building evidence for exact per-skill model bindings. The system doesn't guess which model is sufficient for a job — it learns from production outcomes, proposes the rebind, and waits for approval.

**Judgment becomes an asset.** Here is the part that matters most and gets built least. The hardest asset in enterprise AI is not compute and not model access — it's labeled, domain-specific training data with a quality signal attached. Companies set up annotation teams to manufacture it. In the Autonomaton's architecture, it's exhaust. Every approval is a label. Every correction is a label. Every accepted, rejected, or revised proposal is a label — attached to the full reasoning trace that produced it, stamped with provenance, stored under `~/.grove/`, owned by the operator. The substrate accumulates exactly the production-labeled corpus required to train local models that replace metered cloud calls. Today that corpus drives routing decisions and pattern promotion. Compiling it into local adapters is the designed next turn of the same crank — the architecture was built for it from the first sprint, which is why the data is already in the right shape.

The standard names the consequence plainly: this converts metered cloud dependencies into permanent institutional assets. Your API bill funds a one-time acquisition, not a subscription. The system's most valuable output isn't its answers — it's the accumulating record of your judgment about its answers, and that record belongs to you.

## Where This Sits

Two open-source projects define the territory this fork lives in, and both deserve their due. [NousResearch's Hermes Agent](https://github.com/NousResearch/hermes-agent) is the battle-tested framework underneath this repository — model-agnostic transport, multi-platform gateway, session persistence, skills. [OpenClaw](https://openclaw.ai) proved, at remarkable scale, that people want personal agents that run on their own hardware with a thriving skill ecosystem. Neither is a rival. They're the prior art this pattern builds beside — and in Hermes's case, directly on top of.

The primary difference is one architectural commitment, applied everywhere:

| | Agent-loop frameworks (upstream Hermes, OpenClaw) | The Autonomaton refactor |
|---|---|---|
| **Who owns the run** | The model steers the loop directly — it decides, calls tools, holds state, polices itself | A deterministic dispatcher owns the turn; the model reasons and yields intents, and is physically unable to touch state |
| **Tool access** | Tools available to the agent, gated by allowlists and prompt discipline | No capability record, no admission — the record set is the sole authority on what can reach the model |
| **Risky actions** | Configuration flags and instructions the model is trusted to respect | Zone governance at a mandatory pipeline stage — writes classified by what they change, fail-closed on non-interactive surfaces |
| **Learning** | The operator adds skills and memory files by hand | The system observes its own telemetry, proposes skills, rebinds, and routing changes — nothing lands without operator approval |
| **The record** | Chat logs and files | Structured, provenance-stamped telemetry of every intent, classification, disposition, and approval — the audit trail is a byproduct, and it's also a training corpus |

The industry has started calling the surrounding practice "loop engineering" — designing systems that prompt agents instead of prompting them yourself. The loop-engineering toolkits describe how to run the loop. They don't describe what makes iteration N+1 smarter than iteration N. Without a compounding substrate, the loop repeats; it doesn't accumulate. That accumulation layer — governed, audited, operator-owned — is what this refactor adds, and it's the part you can't get by writing better prompts.

## Status

This is a working research implementation under active development. The governance surgery is complete, the suite is green, and the self-evolution loop is live in production. Rough edges and breaking changes are guaranteed — see [Limitations](#limitations-honestly).

| Layer | Status | Notes |
|---|---|---|
| Dispatcher pipeline | ✅ Shipped | Five-stage governed turn. The dispatcher owns the run; the agent yields intents and is physically unable to touch state. |
| Cognitive Router | ✅ Shipped | T0–T3 competence envelopes, declarative routing rules, cheapest-sufficient-tier dispatch. All bindings are config. |
| T0 Pattern Cache | ✅ Shipped | Confirmed patterns resolve deterministically — no model call. Correction-driven auto-demotion. |
| Tool admission | ✅ Shipped | Capability records are the sole authority on what reaches the model. No record, no admission — on every surface. |
| Zone governance | ✅ Shipped | Green/Yellow/Red at a mandatory pipeline stage. Scope-defining targets classify Red at the universal write wall; non-interactive surfaces fail closed. |
| Kaizen governance UX | ✅ Shipped | One voice, every surface. Four-choice prompts on CLI and Telegram (inline buttons); on the web, interactive store-and-resume — prompt inline, verdict next message, 300s auto-cancel, never auto-allows. |
| Skill authoring pipeline | ✅ Shipped | Operator-initiated and system-detected. Quarantine → governed trial → promotion prompt, all in-conversation. |
| Flywheel self-evolution | ✅ Shipped | The system proposes routing changes, skill promotions, model rebinds, and memory crystallizations from its own telemetry. Nothing lands without operator approval. |
| Per-skill model binding | ✅ Shipped | Binding telemetry accumulates per-skill × per-model evidence; one sanctioned writer applies approved rebinds with full audit trail. |
| Quality gates | ✅ Shipped | Declarative rubrics score producer output; governed redraft cycles. Gates inform the operator — they never withhold work. |
| Consolidation Ratchet | ✅ Shipped | Two-stage learning: tier ratchet observes, consolidation ratchet proposes policy. Atomic write + hot reload. |
| Memory substrate | ✅ Shipped | Lifecycle-managed observations: crystallize → graduate → deprecate, confidence-scored, operator-inspectable. |
| Living Cellar | ✅ Shipped | Compaction pipeline turns raw material into canonical, searchable institutional knowledge (FTS5, retrieval at turn start). |
| Auto-ingest | ✅ Shipped | `~/.grove/notes/` and `~/.grove/research/` poll-ingested into the knowledge substrate. |
| Ledger retention | ✅ Shipped | Event-aware retention engine on a systemd timer; prunes aged telemetry, archives with provenance. |
| Fleet | ✅ Shipped | Background workers (reference skills included) running under the same governance as the primary agent. Worker definitions bundled; enablement is node-local state. |
| Operator portal | ✅ Shipped | Tailscale-private surface: substrate browsing, proposal review, live tier rebinding through the sanctioned writer. |
| Definition/state boundary | ✅ Shipped | Repo owns definitions; `~/.grove/` owns state; the deploy guard halts on drift. Upgrades cannot cross the line in either direction. |
| GCP deployment | ✅ Shipped | Provision/setup/deploy scripts, IAP-only SSH, systemd services, watchdog. Sizing is operator choice. |
| Model independence | ✅ Shipped | Any tier to any OpenAI-compatible provider — frontier APIs, hosted open-weight via OpenRouter, or local via MLX/Ollama. Config swaps, not code changes. |
| `--strict` mode | 🟡 Partial | Shipping in stages. Strict skill promotion is live and tested — proposals queue instead of applying and require explicit approval. The broader enterprise gate across all proposal classes is a future sprint. |

**Test surface:** 3,497 / 3,497 passing, deterministic — reproduce with `scripts/run_tests.sh tests/grove/`. Zero known governance-behavior regressions, verified by full-suite triage. The inherited upstream suite (~23.9k tests) runs alongside it. Please report anything you find. Contributions welcome.

## Self-Authoring Skills

Teach it something once, in conversation, and it becomes a capability:

The operator describes a procedure — or the system notices a repeated pattern in its own telemetry and proposes one. Either way, the draft lands in a quarantine directory where it holds zero authority. It runs under supervision, its results are scored, and when the evidence supports it, Kaizen surfaces a promotion prompt. Approve it, and the skill joins the capability index with a zone classification and its own governance record. Reject it, and the system learns what not to propose. The authoring loop is itself governed: drafting is free, authority is earned, and promotion is always the operator's call.

## Governance You Can Feel

When the agent wants to take a supervised action, the operator sees this:

```
I'd like to run the weather skill. This one's your call before I go ahead.

  [1] Just this once
  [2] For the rest of this session
  [3] Always (standing grant) — I'll remember it
  [4] Not this time
```

First person, active voice. The system names the action and offers clear dispositions. No zone names, no regex, no jargon. On Telegram, the same prompt renders as inline keyboard buttons — one tap on mobile. Every disposition feeds the telemetry that trains the next routing decision.

**The same governance follows you to every surface — it just changes shape.** On the web chat, a supervised action pauses the turn: the prompt arrives inline as the response, your next message is the verdict, and an unanswered prompt cancels itself after five minutes. It never auto-allows, and it never silently drops your request — the pending action is durably stored and waits for you. On genuinely non-interactive surfaces — background fleet workers, scheduled jobs, the headless API — there is no one to ask, so the answer is built in: supervised and protected actions fail closed, loudly, with the refusal in the record. The governance model isn't a feature of one chat surface. It's a property of the pipeline, and every surface inherits it.

**And some things have no prompt at all.** Actions that would change what the system is *allowed to do* — its zone rules, its routing authority, its capability grants — never get a "just this once" button. There is no disposition menu for the Red zone, on any surface, by construction. What happens instead: the system files a durable proposal, tells you where to review it, and stops. The write executes only if you approve it through an operator-authenticated channel the conversational loop cannot reach. The agent can ask for more authority; it cannot take it, and it cannot talk you into handing it over mid-conversation.

When the agent hits a boundary that belongs entirely to you:

```
The command `sudo rm -rf /var/log` needs privileges that stay with
you — sudo / su / doas, never with me. Run it in your terminal, then
tell me the result so I can keep going.
```

Governance that recedes into competence. Invisible on routine turns, present when it matters, structural when it counts.

## Tiers Are Roles, Not Vendors

The router dispatches to competence envelopes. What fills each envelope is one line of the operator's routing config:

| Tier | Role | Cost character |
|---|---|---|
| **T0 — Pattern Cache** | Confirmed patterns, resolved deterministically | No inference. Free. |
| **T1 — Cheap Cognition** | Classification, telemetry, routine turns | Cents per thousand turns |
| **T2 — Premium Cognition** | The daily-driver reasoning tier | The workhorse line item |
| **T3 — Apex Cognition** | Frontier reasoning, escalations, synthesis | Spent deliberately, on proof of need |

One node's bindings on the day this document was verified — an example, not the architecture: T1–T3 bound to frontier models through OpenRouter, a separate telemetry binding on a fast flash-class model, and a quality-assurance tier on an open-weight model. Tomorrow's node might bind T2 to a local model on a Mac. The pattern doesn't care; that's the point. The node outlives any vendor's roster.

## Conformance

This repository implements the structural commitments of [GRV-001 v2.0](https://the-grove.ai/standards/001): the five-stage pipeline, capability-record tool admission, zone governance with operator sovereignty, the definition/state boundary, and feed-first telemetry. Where the implementation's mechanisms differ from the standard's reference schemas, the difference is documented, argued, and tracked — never silent. [CONFORMANCE.md](./CONFORMANCE.md) carries the per-element map: what's implemented, what's enhanced, what's declared as open work. The standard's own position is that its schemas are illustrative and conformance is structural; this document holds us to the stricter reading anyway.

## The EU AI Act, Since You'll Ask

The architecture maps cleanly onto the obligation classes the EU AI Act imposes — human oversight, traceability, transparency, technical documentation — not because it was built for compliance, but because operator sovereignty, complete audit trails, and human-in-the-loop governance are what the regulation is trying to cause:

| Obligation class | Where the architecture answers it |
|---|---|
| Human oversight | Zone governance: supervised actions prompt, protected actions require operator-authenticated approval, non-interactive surfaces fail closed |
| Traceability & logging | Feed-first telemetry: every intent, classification, disposition, and approval, provenance-stamped |
| Transparency of operation | The operator portal and the ledger — the system's behavior is inspectable, not inferred |
| Technical documentation | The standard, this document, and CONFORMANCE.md |

Compliance posture is runway, not relief: an architecture that produces these properties structurally doesn't scramble when enforcement arrives.

## Running It

### Quick start (local)

```bash
git clone https://github.com/the-grove-ai/hermes-autonomaton-refactor
cd hermes-autonomaton-refactor
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[web,mcp,dev]"
cp .env.example .env   # add at least one provider key
hermes chat            # `autonomaton chat` works too
```

First run seeds `~/.grove/` with your routing config. Zone rules ship with the repo; your overrides live in `~/.grove/zones.autonomaton.yaml` and are never touched by upgrades. Routing changes made through the portal or CLI hot-reload — the next turn uses them; hand-edits take effect on restart.

### Production (GCP)

Provisioning, setup, and deploy scripts ship in `scripts/`. The defaults: an `e2-small` Ubuntu 24.04 VM (published GCP pricing puts that around $12/month; sizing is your call — edit the machine type in the provision script), SSH over IAP only, no public ingress, services under systemd with a watchdog, and the web chat surface (Open WebUI) reachable only over your Tailscale mesh. The deploy script carries a drift guard: if node-local state has leaked into the definition tree, the deploy halts and tells you, rather than silently overwriting either side of the boundary.

## Limitations, Honestly

This is a single-maintainer research fork, maintained by The Grove Foundation as a reference implementation of GRV-001. It is pre-alpha and a research release. What follows is the truthful if already outdated contour of the rough edges.

**The Hermes underbelly is still here.** This fork deliberately diverged from upstream — tracking NousResearch's releases stopped being viable once the dispatcher surgery went deep, and the divergence is now a design fact, not a backlog item. That cuts both ways. The governance architecture is Grove's; large parts of the body are still upstream Hermes, and it shows:

- **Naming is mid-migration.** The CLI answers to `hermes` (with `autonomaton` as an alias), the systemd services carry `hermes-*` names, the repo is `hermes-autonomaton-refactor`, and internal modules mix Grove vocabulary with upstream terminology. A full rename is scoped and queued; until it lands, expect both dialects in logs, paths, and code.
- **Dormant upstream surfaces.** Gateway code for Slack, Discord, WhatsApp, and Matrix exists in the tree but has never been tested against the Grove governance layer. Treat them as untested until a sprint proves them. Contributions and bug reports here are especially welcome. Most testing has been done through the CLI, Telegram, and WebUI surfaces for expediency.
- **Inert upstream tooling.** Some upstream tool modules for platforms this deployment never routes remain in the tree. Under capability-record admission they are structurally unreachable — no record, no admission — but the dead code awaits removal, not governance.
- **Inherited test mass.** The ~23.9k-test upstream suite runs alongside the Grove governance suite; its residual skips are documented environment gates and upstream-divergence markers, not hidden failures.

**Known architectural debt is tracked by name.** The conformance gaps against GRV-001 v2.0 are declared in [CONFORMANCE.md](./CONFORMANCE.md) rather than papered over — including the items still open. A memory-provider tool path that predates capability-record admission is scheduled to be brought under it. Field-level separation of routing authority within the config file is in progress. Universal per-write provenance stamping is partial. Each has a named sprint or debt entry; none is silent.

**Operational honesty.** The web chat surface is Open WebUI, not a Grove-built UI. Cost telemetry is not yet captured, so we publish no per-interaction cost figures — the economics claims in this README rest on published provider pricing and the architecture's tier mechanics, not on measured spend. One operator runs this in production daily; that is the extent of the deployment evidence.

If you're evaluating this repo, the right frame is: **the pattern is the product; this is working proof.** Design from scratch against [the standard](https://the-grove.ai/standards/001), or fork this and inherit both the composable, declarative architecture and the underbelly. Either path is fine. This document tries to make sure you know exactly which parts are which.

## Credit, Standard, License

This work stands on [NousResearch's Hermes Agent](https://github.com/NousResearch/hermes-agent) — the transport, gateway, and session machinery that made the governance surgery possible are theirs, and the respect is genuine. The Autonomaton Pattern is published as [GRV-001](https://the-grove.ai/standards/001) by [The Grove Foundation](https://the-grove.ai). Licensed under the terms in [LICENSE](./LICENSE). Contributions welcome — the dormant surfaces and the naming migration are honest places to start; open an issue before a large PR so the sprint discipline can meet you halfway.

---

*Architectural guarantees beat policy promises every time.*
