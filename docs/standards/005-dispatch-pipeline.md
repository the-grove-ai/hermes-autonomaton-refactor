# **The Dispatch Pipeline**

*Runtime Physics for Conformant Autonomatons*  
**GRV-005 · v1.1 · May 2026 · CC BY 4.0 International**  
Jim Calhoun · The Grove Foundation

> **v1.1 amendment (Sprint 32 — sovereignty-ux-v1):** § VI replaces the v1.0 two-option Skip/Drop disposition surface with a four-choice Kaizen-register prompt. Implementation details (zone names, regex patterns, rule sources, intent indices) MUST move to the Kaizen Ledger; operator-facing text MUST use plain language.

## **I. Preamble**

GRV-005 specifies the runtime dispatch contract for conformant Autonomatons.  
The goal of this pipeline is to deliver a composed, frictionless user experience where architectural complexity recedes into the background. It achieves this by mapping Toyota Production System principles — **Jidoka** (automation with a human touch), **Andon** (halting for human intervention), and **Kaizen** (continuous improvement) — to the cognitive layer.  
Enforcement is architectural, not procedural. The Standard names objects, the contract that binds them, and the assertions a runtime must satisfy to claim conformance.

* GRV-005 supersedes any prior arrangement that placed model selection, configuration reading, or tool execution within the Agent.  
* Implementation specifics throughout this Standard — exact attribute names, class signatures, async primitives, state-management semantics, and decision-rule policies — are deferred to Section IX.  
* Conformance criteria are enumerated in Section VIII.

Conformance signaling follows GRV-004: "Implementations MUST declare which version of GRV-004 they conform to and which (if any) optional fields they implement." \[GRV-004 §Invariant 5\] A runtime claiming GRV-005 conformance declares its status in the same form.

## **II. The Dispatcher (Authority)**

The Dispatcher owns the pre-agent pipeline and the physical execution substrate.

* The Dispatcher MUST own message-zone classification.  
* The Dispatcher MUST select the capability tier before constructing the Agent.  
* The Dispatcher MUST own all tool execution.  
* The Dispatcher MUST own escalation decisions.  
* The Dispatcher MUST observe post-turn execution for Kaizen.

Dispatcher authority implements GRV-002's governance-at-endpoints: "Governance functions belong at the endpoints — with the human operator — not inside the cognitive layer." \[GRV-002 Part III\] The Dispatcher is the runtime manifestation of the Cognitive Router. \[GRV-001 Part III\]

## **III. The Agent Contract (Intent)**

The Agent is restricted strictly to yielding intents.

* The Agent MUST NOT execute tools.  
* The Agent MUST NOT select its own model or tier.  
* The Agent MUST NOT access the system substrate.  
* The Agent receives tool descriptions, not implementations.

The exclusion of tool execution from the Agent is structural. GRV-003 establishes the precedent: "The code governing Red actions lacks the permissions to execute them. Not *will not*. *Cannot*." \[GRV-003 §4\] GRV-005 generalizes this invariant from Red-zone actions to *all* tool execution.  
Tier-not-model dispatch follows GRV-001 Principle II (Capability Agnosticism). \[GRV-001 Part IV Principle II\]

## **IV. The Intent Protocol (v1) — Bidirectional**

The Agent yields intents; the Dispatcher yields observations.

### **Agent → Dispatcher (four intent types, exhaustive in v1)**

* **ToolIntent** — Request to execute a named tool with named arguments.  
* **EscalationRequest** — Request for expanded capability.  
* **FinalResponse** — The terminal output of the turn. The Dispatcher ceases execution.  
* **ClarificationRequest** — Request for operator input. The turn pauses pending response.

### **Dispatcher → Agent (one observation type, exhaustive in v1)**

* **Observation** — The deterministic result of an executed ToolIntent or the outcome of an EscalationRequest. Carries status, return values, and metadata. The Agent's reasoning resumes using this Observation as new context.

### **Contract Shape**

* The contract MUST be generator-shaped (Section IX).  
* The Agent MUST NOT call into the Dispatcher synchronously.

The generator shape realizes GRV-003's invariant: "Every cognitive operation traverses all five stages, in order, every time." \[GRV-003 §4\] Agent yields represent participation in the pipeline, never a parallel or bypass path.

## **V. Governance Integration (Jidoka, Andon, Kaizen)**

Jidoka, Andon, and Kaizen act as sequential, non-bypassable gates within the Dispatcher pipeline.

* Message-zone classification (Jidoka) MUST fire in the Dispatcher's pre-construction path; the Agent's reasoning loop receives the result, does not produce it.  
* Tool-zone classification MUST fire per ToolIntent at intent-yield, prior to execution.  
* The pipeline MUST halt (Andon) when zone discipline requires operator authorization.  
* The Sovereign Prompt MUST surface to the operator upon an Andon halt.  
* Dispatch MUST NOT proceed without operator disposition.  
* Post-turn telemetry MUST be captured (Kaizen).  
* Kaizen output MUST route to appendix content and MUST NOT inject into mid-stream Agent reasoning.

Zone classification implements GRV-002's sovereignty mechanism: "No API provider can restrict what the operator has classified as Green. No model vendor can require human approval for what the operator has classified as autonomous." \[GRV-002 Part III\]

## **VI. Mid-Execution Andon — Disposition Semantics** *(v1.1)*

**v1.1.** When tool-zone discipline triggers an Andon halt, the Dispatcher hands authority back to the operator. The operator-facing Sovereign Prompt MUST use plain language — Kaizen register. Zone names, matched regex patterns, source identifiers, and rule indices MUST NOT surface in the operator prompt; they MUST be recorded to the Kaizen Ledger.

The Sovereign Prompt MUST present exactly four disposition options:

1. **Allow once.** The Dispatcher executes the action for this invocation only. The same action on a future turn re-prompts.
2. **Allow for this session.** The Dispatcher executes the action and caches a session-scoped allow. Subsequent identical invocations (same tool, same arguments) execute silently within the session.
3. **Always allow.** The Dispatcher executes the action, caches a session allow, and queues a ZonePromotionProposal to the GRV-008 proposal queue (`~/.grove/proposals.jsonl`). The promotion takes effect only after operator approval via `autonomaton flywheel approve`.
4. **Don't allow.** The Dispatcher injects a denial Observation; the Agent may recover, re-reason, or pivot. The denial is cached for the session; subsequent identical invocations auto-deny silently.

The v1.0 `Skip` disposition maps to `Don't allow`. The v1.0 `Drop` disposition is removed — turn-flush as an operator escape hatch is replaced by the cumulative session deny cache + the Section VII red-zone hard-denial after three strikes.

Non-interactive surfaces (batch, gateway) MUST map all four choices to `Allow once` with a Kaizen Ledger telemetry record. Silent surfaces (test fixtures) MUST map to `Allow once` with no record. Gateway surfaces MUST NOT queue promotion proposals from a non-TTY context — the operator has no CLI access to approve from a mobile messaging client.

The Dispatcher MUST maintain two session-scoped caches (deny + allow) keyed by `(tool_name, sha256(canonical_json(arguments)))`. Cache hits MUST auto-apply silently without invoking the operator handler, and MUST emit a `session_cache_hit` event to the Kaizen Ledger with `type=deny` or `type=allow`. Cache lifetime is the Dispatcher instance — a new Dispatcher starts with empty caches.

The Dispatcher MUST maintain a per-turn, per-tool red-zone strike counter. On every red-zone halt the counter MUST increment for the triggering tool. At three strikes the Dispatcher MUST force a hard-denial path WITHOUT invoking the operator handler: the injected denial Observation MUST carry the directive text `"HARD DENIAL: This action is prohibited. Do not attempt this tool with these arguments again."` and a metadata marker `is_hard_denial=true` so the Agent can detect "do not retry" without parsing the text. The counter resets at every `dispatch_turn` entry; cross-turn enforcement remains architectural via the zone rule itself.

## **VII. The Escalation Contract**

Escalation is a structured request routed strictly from the Agent to the Dispatcher.

* The Agent MUST escalate only via EscalationRequest.  
* EscalationRequest payloads MUST be structured data.  
* The Dispatcher's escalation decision MUST be observable.  
* Decisions MAY be handled automatically per predefined operator policy.  
* Borderline-case decisions MUST surface to Kaizen appendix content per policy.  
* The Agent MUST NOT simulate or fabricate capability it was not explicitly granted.

Escalation decisions enforce the Stage 4 gate: "Stage 4 is always human." \[GRV-003 §4\] Every decision carries strict attribution per GRV-001 Principle III (Provenance as Infrastructure).

## **VIII. Conformance Criteria**

GRV-005 conformance is highly testable. A runtime conforms when it successfully demonstrates that:

1. Tool execution authority resides exclusively in the Dispatcher.  
2. The Agent is limited to yielding intents.  
3. Classification fires at both message ingestion and at tool-intent yield.  
4. Tier selection strictly precedes agent construction.  
5. Escalation utilizes structured EscalationRequest data, bypassing retry loops or self-modification.  
6. Mid-execution Andon surfaces operator disposition options rather than automated state-management choices.  
7. The Agent yields entirely rather than blocking on tool execution, resuming only upon receiving an Observation.

A reference conformance test suite for these criteria is a future deliverable.

## **IX. Implementation Boundaries**

Implementation specifics named below are deferred or strictly bounded as defined.

### **1\. Attribute names and class signatures.**

* Exact symbol names for the Dispatcher, the Agent, the four intent types, and the Observation type.  
* Module paths and import structure.  
* Session-state data shape.  
* Error envelope shape for tool exceptions.

### **2\. Async primitives.**

* Generator vs coroutine pattern (Section IV's contract shape resolves to one of these).  
* Event-loop selection.  
* Concurrency model for tool execution (sequential vs pooled).

### **3\. State-management semantics.**

* **The Andon Pause:** Upon triggering an Andon halt, the Dispatcher MUST suspend the Agent's generator and hold the current turn's context array in volatile memory. No partial turns may be written to the persistent session store.  
* **Disposition: Skip:** The Dispatcher MUST resume the generator, injecting an Observation containing a deterministic denial code. The turn proceeds; the Agent is responsible for degradation.  
* **Disposition: Drop:** The Dispatcher MUST forcefully terminate the generator. The volatile context array MUST be flushed. The persistent state MUST remain identical to the millisecond before the operator initiated the turn.  
* **UX Primitives:** The Sovereign Prompt MUST decouple the decision payload from standard conversational text, presenting the intent, arguments, and disposition options in a structured, deterministic interface.

### **4\. Decision-rule policies & The Kaizen Loop.**

* **The Foreground/Background Split:** Upon receipt of a FinalResponse, the Dispatcher MUST decouple the conversational payload from the operational telemetry. Only the conversational payload may be written to the active context window.  
* **Kaizen Telemetry Routing:** All generator traces, intent metadata, tool latencies, Andon triggers, and operator disposition outcomes MUST be routed out-of-band to an isolated Kaizen Ledger (the Appendix).  
* **No Mid-Stream Injection:** Kaizen telemetry MUST NOT be injected into the Agent's active reasoning loop. The Agent evaluates the present; the Dispatcher observes the past.  
* **The Skill Flywheel Interface:** The Kaizen Ledger MUST be structured to allow asynchronous querying and pattern recognition, enabling offline processes to propose policy optimizations (e.g., auto-granting frequently approved EscalationRequests) without degrading runtime performance.  
* **Tier Override:** Escaping local limits for edge-case reasoning requirements.

### **5\. Substrate access enumeration.**

The Agent contract (Section III) excludes substrate access. The specific interfaces falling under that exclusion — subprocess, HTTP client, MCP client, file-system writes outside Dispatcher-mediated paths — are implementation detail.

### **6\. Migration plan from current arrangements to GRV-005 conformance.**

## **X. Architectural Horizons**

Future protocol types are documented to establish architectural trajectory; v1 implementation explicitly excludes them.

### **Agent → Dispatcher (Deferred Intent Types)**

* **MemoryWriteIntent** — A structured request to alter the sovereign, persistent state of the system across sessions.  
  * The Agent MUST NOT possess direct write access to persistent memory stores (e.g., vector databases, graph nodes, or core instruction files).  
  * MemoryWriteIntent routes proposed state changes through the Dispatcher to ensure consistency with Kaizen-mediated promotion and operator sovereignty.  
  * The Dispatcher MUST apply zone-classification to MemoryWriteIntent. If memory modification requires authorization, the Dispatcher MUST trigger an Andon halt and surface the proposed state change to the operator for disposition.  
* **SubAgentIntent** — A structured request to provision and delegate a bounded task to a lateral cognitive worker.  
  * The primary Agent MUST NOT possess the primitive to instantiate, orchestrate, or directly communicate with other agents.  
  * The primary Agent yields a task definition, requested capability tier, and context boundary.  
  * The Dispatcher MUST own the complete lifecycle of the sub-agent.  
  * The sub-agent MUST be bound by the identical Jidoka, Andon, and Kaizen pipeline constraints as the primary Agent. Operator prompts (Andon halts) triggered by a sub-agent MUST surface directly to the human endpoint, circumventing the primary Agent entirely.  
  * The Dispatcher returns the sub-agent's terminal output to the primary Agent as an Observation.

### **Dispatcher → Agent (Deferred Observation Types)**

* **OperatorDispositionObservation** — The reciprocal payload for resumed-after-Andon flows.  
  * v1 handles this as a specific metadata state within a standard Observation. Future Standards may split this into a distinct type to carry deeper semantic meaning regarding the operator's rationale for a skip or denial, allowing the Agent to map its recovery strategy directly to the human's intent.

### **Catch-all**

* Any future intent or observation types subsequent Standards introduce MUST inherit the structural discipline of Dispatcher authority and Agent intent.

## **Normative References**

| Source | Verbatim quote | URL   |
| :---- | :---- | :---- |
| GRV-004 §Invariant 5 | "Implementations MUST declare which version of GRV-004 they conform to and which (if any) optional fields they implement." | https://the-grove.ai/standards/004 |
| GRV-002 Part III | "Governance functions belong at the endpoints — with the human operator — not inside the cognitive layer." | https://the-grove.ai/standards/002 |
| GRV-003 §4 | "The code governing Red actions lacks the permissions to execute them. Not *will not*. *Cannot*." | https://the-grove.ai/standards/003 |
| GRV-003 §4 | "Every cognitive operation traverses all five stages, in order, every time." | https://the-grove.ai/standards/003 |
| GRV-002 Part III | "No API provider can restrict what the operator has classified as Green. No model vendor can require human approval for what the operator has classified as autonomous." | https://the-grove.ai/standards/002 |
| GRV-003 §4 | "Stage 4 is always human." | https://the-grove.ai/standards/003 |

## **Informative References**

* **GRV-001** — *The Autonomaton Pattern: Toward Self-Authoring Software Systems* — https://the-grove.ai/standards/001  
  * Part III (The Pattern — Cognitive Router and five-stage pipeline)  
  * Part IV Principle II (Capability Agnosticism)  
  * Part IV Principle III (Provenance as Infrastructure)  
  * Part V (The Zone Model)  
  * Part VI (The Flywheel)  
* **GRV-002** — *TCP/IP for the Cognitive Layer* — https://the-grove.ai/standards/002  
* **GRV-003** — *The Learner Autonomaton: A Lifelong Cognitive Router in a Composable University* — https://the-grove.ai/standards/003  
* **GRV-004** — *The Autonomaton Protocol: Sovereign Declaration for the Polarity-Compliant Internet* — https://the-grove.ai/standards/004