# GRV-007 Declarative Prompt Composition

**Version:** 1.0
**Status:** Draft (Sprint 36)
**Supersedes:** —
**Depends on:** GRV-005 (Dispatch Pipeline)

## **I. Preamble**

The system prompt is the autonomaton's compositional context. It is not a single string — it is a declarative composition of named sections, each rendered from a substrate source and assembled by a Dispatcher-owned composer. GRV-007 governs that composer's contract.

Pre-GRV-007, prompt assembly lived inside the Agent (`AIAgent._build_system_prompt_parts`). Hardcoded ordering, inline conditional gating, scattered constants. Adding a section required editing the Agent. GRV-007 inverts that: section providers register declaratively; the Dispatcher composes; the Agent receives.

## **II. The PromptComposer (Authority)**

The Dispatcher MUST own prompt composition. The Agent MUST NOT construct its own system prompt. The Agent MUST receive the composed prompt at construction or at turn entry; the Agent's reasoning loop reads from it, does not produce it.

A `PromptComposer` instance MUST exist as a Dispatcher-owned object. The composer MUST expose:

* `register_section(name, provider, order, tier)` — registration API.
* `compose(**context) -> ComposedPrompt` — composition entry point.

## **III. The Provider Contract**

A section provider is a callable: `Callable[[Dict[str, Any]], Optional[SectionResult]]`.

* It MUST accept a single `context` dict carrying turn-specific and per-Agent state.
* It MUST return `SectionResult(label, text)` when the section is included.
* It MUST return `None` to skip (runtime gating).
* It MUST NOT mutate the context dict.
* It MUST NOT reach back into the Agent instance — all state flows through `context`.

## **IV. Ordering Authority**

Section ordering MUST be config-driven, not code-driven. The composer reads order from `runtime_ctx.config["prompt"]["sections"][name]["order"]`. Lower order renders earlier within a tier. Tier order is fixed: `stable → context → volatile`.

A vanilla install (no prompt config) MUST use the in-code default ordering. Adding a new section MUST be a one-line registration call plus a config entry — never an edit to existing rendering code.

## **V. Gating Semantics**

Sections are gated at two layers:

1. **Config layer:** `enabled: false` in `prompt.config.yaml` disables a section globally. The provider is never called.
2. **Provider layer:** the provider returns `None` for runtime gating (e.g., a tool-guidance provider returns None when its gated tool isn't loaded).

Both layers MUST be supported. The config layer is operator-facing; the provider layer is for state-dependent rendering decisions.

## **VI. Caching Policy**

The composer MUST NOT cache across turns. Section assembly is fast enough (sub-50ms cold, sub-5ms warm) that per-turn composition is acceptable. Caching is the responsibility of the LLM provider's prompt-prefix cache (content-based), not the composer.

Per-Agent caching of identity-file reads or substrate sources is permitted at the **substrate layer** (e.g., `load_identity()`'s file cache) but MUST NOT introduce composer-state caching that requires invalidation hooks.

## **VII. Feed-Consumer Registration**

Sprint 37's contextual preamble is the first feed-consumer registration: a section provider that consumes the Sprint 28 intent feed and the Sprint 35 classification result to render a per-turn contextual preamble. GRV-007 establishes the surface; Sprint 37 registers against it.

Future feed consumers (Skill Flywheel proposal injection, Tier Ratchet recommendations, etc.) register the same way: one provider, one registration call, one config entry.

## **VIII. Conformance Criteria**

An implementation conforms to GRV-007 when:

1. No prompt assembly code exists outside the composer.
2. The Dispatcher composes the prompt; the Agent receives it.
3. Section ordering is config-driven.
4. Gating is dual-layer (config + provider).
5. Adding a new section is a one-line registration + one config entry.
6. Removing a section is a one-line config flip (`enabled: false`) without code changes.

## **IX. Implementation Boundaries**

### 1. Provider purity.

Providers MUST be pure functions of the `context` dict. They MUST NOT read from disk, network, or substrate beyond what `context` carries. Substrate reads (identity files, memory, session) are the Dispatcher's responsibility; the Dispatcher passes the hydrated content into `context`.

### 2. Per-section token budgets.

Optional `max_tokens` per section is reserved for future use. Sprint 36 establishes the surface; budget enforcement is a future sprint.

### 3. Composer reentrancy.

`compose()` MUST be reentrant. Multiple concurrent calls (concurrent gateway sessions sharing a Dispatcher) MUST not interfere.

## **X. Architectural Horizons**

Deferred to future Standards:

* **Per-section token budgets** with enforcement (truncate, summarize, drop).
* **A/B testing surface** for prompt section variants.
* **Operator-facing section editor** (UI for non-technical operators).

## **Normative References**

* GRV-005 — Dispatch Pipeline (§ II Dispatcher authority; § III Agent contract).

## **Informative References**

* Sprint 24a — Context Instrumentation (per-section token measurement).
* Sprint 26 — Substrate authority extraction.
* Sprint 33 — Construction inversion.
* Sprint 34 — RuntimeContext mandatory.
* Sprint 35 — Classify-before-construct.
