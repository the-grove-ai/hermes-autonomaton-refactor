# GRV-006 The Compositional Context Loop

**Version:** 1.0
**Status:** Draft (Sprint 37)
**Supersedes:** —
**Depends on:** GRV-005, GRV-007

## **I. Preamble**

The autonomaton's system prompt MUST adapt to observed operator behavior. A static prompt is a frozen prior. GRV-006 governs the closed loop: operator interactions write intent records; a prompt section reads them; the model sees its own historical patterns and biases toward observed success. The autonomaton gets smarter with use — Pattern v1.3 Commitment 1 instantiated in the system prompt itself.

## **II. The Compositional Preamble**

A registered section provider MUST render the Compositional Preamble. The provider MUST consume the Sprint 28 intent store and the current turn's classification. The rendered preamble MUST contain three labeled sub-blocks in this order:

* Contextual Anchor — the current turn's intent_class and a pattern_hash fragment.
* Historical State — a bounded list of prior matching intent records with timestamp, message stem, and outcome.
* Outcome Signal — aggregated outcome counts across the matched set. MUST exclude pending; pending is not a signal, it is missing information.

The preamble MUST render as a single section with one label (`Compositional Context`) in the composer's sections dict, not three separate registrations.

## **III. Tuning Parameters**

The preamble's configuration MUST expose three operator-tunable knobs:

* top_k — maximum number of prior intents rendered in Historical State.
* recency_decay — per-position weight applied when sorting matches by timestamp. Range (0, 1]. 1.0 means equal-weight; 0.85 means each next-older slot weighs 85% of the prior.
* outcome_filter — whitelist of outcome states the provider considers. MUST default to {success, correction, drop}; MUST exclude pending.

Operators MAY tighten outcome_filter to {correction} for active-learning installations or to {success} for confidence-priming installations.

## **IV. Source Authority**

The Sprint 28 intent store (`~/.grove/intent_records.jsonl`) is the sole source for the preamble's Historical State and Outcome Signal. The provider MUST NOT read from session memory, the conversation buffer, or any other substrate to construct the preamble — those are separate sections in the composer.

The provider MUST consume the store via the public IntentStore API (`latest_by_turn`, `filter`). It MUST NOT parse the JSONL file directly.

## **V. The Feed-Consumer Pattern**

GRV-007 § VII reserved the feed-consumer registration surface; GRV-006 defines what a feed consumer looks like. A feed consumer is a section provider that:

1. Reads from one named substrate feed (the Sprint 28 intent store is the prototype; future feeds include the Skill Flywheel proposal stream and the Tier Ratchet recommendation stream).
2. Renders a deterministic format given the feed's current state.
3. Returns None when the feed is empty or no matches qualify.
4. Operates within a documented latency budget (50ms for v1).
5. Is governed by a per-feed config block in `prompt.config.yaml`.

Future feed consumers MUST follow the same shape: one provider, one config block, one per-feed Standard if the feed shape is non-trivial.

## **VI. Closing the Loop**

GRV-006 instantiates the closed loop in the substrate:

```
operator interaction
  → intent record (Sprint 28)
  → preamble query (Sprint 37)
  → model sees observed patterns
  → response shaped by prior outcomes
  → new intent record (next turn)
```

The loop MUST be observable. The Compositional Preamble appearing in the composed prompt is the operator-visible signal that the loop is closed. No-preamble in the rendered prompt MUST mean either (a) the store is empty, (b) no matches qualified, or (c) the section is config-disabled — never silent failure of the query path.

## **VII. Conformance Criteria**

An implementation conforms to GRV-006 when:

1. A section provider named `contextual_preamble` is registered in the default composer.
2. The provider reads the Sprint 28 intent store via the public API.
3. The provider returns the three-block format defined in § II.
4. top_k, recency_decay, and outcome_filter are operator-tunable via `prompt.config.yaml`.
5. Empty store and no-match cases return None (no preamble).
6. Query cost on a 100-record store is under 50ms.

## **VIII. Implementation Boundaries**

### 1. Read-side only.

The preamble provider MUST be a pure consumer. It MUST NOT write to the intent store, mutate any record, or trigger any side effect during composition. Intent writes are the Dispatcher's responsibility (Sprint 28 Phase 4 provisional-write pattern).

### 2. No live LLM calls.

The preamble MUST be rendered from the intent store alone. It MUST NOT make a synchronous LLM call during composition to summarize, classify, or rerank matches. Future sprints MAY introduce an asynchronous summarization path that writes a precomputed summary field on records; that summary becomes the new source field, not a per-turn synchronous LLM call.

### 3. Provider purity (per GRV-007 § IX.1).

The provider MUST be a pure function of the `context` dict. The intent store access is an exception explicitly carved by GRV-006 § IV — the store is the provider's named substrate feed. No other I/O is permitted.

### 4. Reentrancy (per GRV-007 § IX.3).

The preamble provider MUST be reentrant. The IntentStore's `latest_by_turn` iterator is read-only and safe to invoke concurrently; the provider holds no module-level state.

## **IX. Architectural Horizons**

Deferred to future Standards:

* Sprint 38 (correction-signal-v1) — the model's response to a Historical State row marked `correction` MUST be measured; if the correction rate for a pattern_hash does not improve after N preamble-equipped turns, the autonomaton MUST escalate to a Kaizen recommendation.
* Asynchronous summarization — replacing the raw message_stem list with an LLM-generated theme summary written ahead of compose-time.
* Cross-operator preambles — installations with multiple operators MAY scope matches by operator_id; v1 scopes by session_id only via the records the operator's own sessions wrote.

## **Normative References**

* GRV-005 — Dispatch Pipeline.
* GRV-007 — Declarative Prompt Composition.

## **Informative References**

* Sprint 28 — Intent capture v1.
* Sprint 35 — Classify-before-construct.
* Sprint 36 — Prompt composer.
