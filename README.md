# The Hermes Autonomaton Refactor

A fork of [NousResearch's Hermes Agent](https://github.com/NousResearch/hermes-agent) (~176K★), restructured around the [Autonomaton Pattern](https://the-grove.ai/standards/001). Released by [The Grove Foundation](https://the-grove.ai).

The EU AI Act's high-risk obligations — human oversight, record-keeping, transparency, robustness — require architectural answers, not bolt-on compliance layers. This fork demonstrates that those answers can be applied to an existing, capable agent framework without replacing it. We took NousResearch's Hermes Agent, moved state, memory, and tool execution out of the agent, and handed them to a deterministic dispatcher. The language model still does what it does well — reasoning, judgment, creativity. It just no longer owns the run.

The result is a governed agent that satisfies the structural requirements emerging from the EU AI Act and the broader regulatory landscape, while preserving the full capability of the upstream framework. We continue developing and testing this demo release to bring forth the full Autonomaton benefits — model independence (already supporting Gemma 4 via MLX, Ollama, and any OpenAI-compatible provider alongside Anthropic), self-improving routing, and operator-declared governance that domain experts can read and revise without touching code.

---

## The Architecture the Industry Is Converging On

The three most consequential players in agent infrastructure are all moving toward the dispatcher-first design. LangChain rebuilt itself on LangGraph's state-machine runtime. Microsoft is migrating AutoGen to typed graph workflows. Temporal's durable-execution model treats the LLM as an ephemeral activity, never the owner. Independent teams, who had never seen the Autonomaton Pattern spec, arrived at the same five-stage loop — telemetry, recognition, compilation, approval, execution.

But the frameworks stopped at the dispatcher.

The [Autonomaton Pattern](https://the-grove.ai/standards/001) goes further: a model-independent architecture that gets cheaper, more local, and more sovereign with use — as confirmed patterns ratchet from frontier API calls to smaller local models to deterministic rules that need no inference at all. A declarative governance layer where domain experts, not just developers, control the system through configuration they can read and revise. Self-evolution that proposes routing changes through the same approval gate it enforces on everything else. And a [sovereign node declaration protocol](https://the-grove.ai/standards/004) that makes the whole pattern composable and portable across independent nodes. The independent frameworks built the circuit breaker. The Autonomaton specifies the operating layer above it.

---

## Preview Status

This is a working fork under active development. The core governance architecture is implemented and tested. Daily-driver stability is improving sprint over sprint. Some rough edges remain.

| Layer | Status | Notes |
|---|---|---|
| Dispatcher pipeline | ✅ Shipped | Five-stage pipeline. Generator-shaped agent loop. Bidirectional Intent Protocol. |
| Cognitive Router | ✅ Shipped | T0–T3 tier routing. 15-intent taxonomy. T1 default floor. |
| Zone-based sovereignty | ✅ Shipped | Green/Yellow/Red zones. Hierarchical rules. Regex fail-hard. |
| Kaizen four-choice UX | ✅ Shipped | Plain-language operator prompts. Session caching. Zone promotion via proposal queue. |
| Flywheel proposal queue | ✅ Shipped | CLI: `autonomaton flywheel list/show/approve/reject`. |
| Declarative prompt composition | ✅ Shipped | 18 sections as declarative providers. Sub-50ms compose. |
| Feed-first context loop | ✅ Shipped | Intent store queries. Contextual preamble. |
| Agent purity | ✅ Shipped | Four axes substrate-free: construction, session, memory, classification. |
| Model independence | ✅ Shipped | Anthropic, OpenAI, Ollama, oMLX (Gemma 4, etc.). Config swaps, not code changes. |
| Tool registry | ✅ Shipped | Dispatcher-owned. Module-level singleton deleted. |
| Skill authoring pipeline | 📋 Scaffolded | Quarantine → sandbox → promote. Try before you buy. |
| `--strict` mode for enterprise | 📋 Planned | Promotion gated on review + test coverage. |

**Test surface:** 24,060 passing / 257 failing / 214 skipped across 24,531 tests. Governance suite: 1,153/1,153. Live CLI integration suite: 13/13.

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
The agent wants to run a skill (google-workspace).
This requires a decision before it can continue.

[1] Allow this once
[2] Allow for this session
[3] Always allow this — I'll save the preference
[4] Don't allow this
```

No zone names. No regex patterns. No jargon. The operator decides; the system remembers.

"Always allow" queues a promotion proposal. The operator approves it via `autonomaton flywheel approve` when ready. The governance is structural. The UX is invisible.

---

## Tier Routing and Model Independence

The Cognitive Router sends each request to the cheapest tier that can handle it:

| Tier | Default Binding | Role |
|---|---|---|
| T0 — Pattern Cache | Deterministic | Confirmed patterns. No model call. |
| T1 — Cheap Cognition | Haiku-class | Routine, well-understood requests. The daily driver. |
| T2 — Premium Cognition | Sonnet-class | Novel or moderately complex work. |
| T3 — Apex Cognition | Opus-class | High-stakes, ambiguous, or creative. |

T1 is the default floor. Most daily-driver queries — memory, retrieval, conversation, factual lookup, translation, summarization — run here at ~$0.002 per interaction. T2 and T3 are escalation tiers for knowledge work and architectural reasoning.

All bindings are config swaps in `routing.config.yaml`. Local models (Gemma 4 via MLX, Ollama, any OpenAI-compatible endpoint) work at any tier. Model independence is structural, not cosmetic — swap the binding, keep the governance.

As the system learns, confirmed patterns ratchet downward: from frontier API calls, to smaller local models, to deterministic rules that need no inference at all. Inference cost falls as the system learns. Autonomy compounds at the node.

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

On first invocation, `~/.grove/` is seeded automatically with operator
copies of `routing.config.yaml` (tier bindings) and `zones.schema.yaml`
(sovereignty rules). Edit those files to retune routing or governance —
no restart required for most changes.

The CLI binary is `hermes` (upstream compat) with `autonomaton` as an
alias. Both work.

---

## Upstream Credit

This is a derivative work. [NousResearch](https://nousresearch.com) built the Hermes Agent under the MIT license. Grove applied an architecture to it. The upstream `LICENSE` and copyright notice travel with the code. Where this document describes what "the fork" does, it describes Grove's modifications; the underlying framework remains the work of its original authors.

---

## The Standard

This fork implements the [Autonomaton Pattern (GRV-001)](https://the-grove.ai/standards/001), published under CC BY 4.0 at [the-grove.ai](https://the-grove.ai). The standard describes the architecture in the abstract. This fork makes it executable.

<!-- TODO: publish essay and verify URL -->
For the full argument behind the design, read [The Hermes Autonomaton Refactor](https://the-grove.ai/hermes-autonomaton).

---

## License

- **Code:** MIT. Upstream (NousResearch) + Grove modifications. Both copyright notices in LICENSE.
- **The Autonomaton Pattern (GRV-001):** CC BY 4.0. Spec and code carry different licenses by design.

---

## Contributing

This fork follows the Foundation Loop sprint methodology. One sprint, one purpose, one set of writes. Contributions that align with GRV-001 and the sprint discipline are welcome.

For questions, architecture discussion, or collaboration: questions@the-grove.ai

---

*Architecture is the guarantee; policy is the promise. Model independence is not theater.*
