# GRV-008 Flywheel Proposal Governance & Safety Subsystems

**Version:** 1.0
**Status:** Draft (Sprint 46)
**Supersedes:** —
**Depends on:** GRV-005, GRV-006, GRV-007

## **Abstract**

This standard defines the mechanisms by which the Autonomaton safely proposes, evaluates, and applies self-improvement modifications (e.g., tier ratcheting, pattern compilation). To guarantee baseline determinism and preserve absolute operator authority, all modifications must pass a strict regression gate and adhere to a layered configuration hierarchy.

## **I. The Safety Subsystem**

The Autonomaton must not surface a proposal to the operator unless it has cryptographically proven its safety against known deterministic baselines.

- **The Evaluation Harness:** Any sub-system generating a proposal (e.g., TierRatchet, IntentPatternDetector) must pipe its proposed state through the local runner (`grove.eval.hero_runner`) prior to queuing.
- **The Hero Prompts:** The runner must execute the curated Armory Hero Prompts against the proposed state and assert that:
  - (a) Classified intents perfectly match the expected baseline.
  - (b) Selected tiers match or optimize the expected baseline.
  - (c) Tool composition sequences remain unbroken.
  - (d) No Andon halts are triggered on golden-path execution.
- **Failure Protocol:** If the proposed state fails any assertion within the hero suite, the proposal is silently dropped. It never reaches the operator review surface.

## **II. The Proposal Queue Contract**

Proposals that successfully clear the regression gate are appended to the pending queue.

- **Location:** `~/.grove/proposals.jsonl`
- **Schema Invariants:** Each entry must be a valid JSON object containing:
  - `proposal_id`: Unique cryptographic hash of the proposed change.
  - `type`: The class of proposal (e.g., `routing_update`, `skill_candidate`).
  - `payload`: The structured diff or skill scaffolding parameters.
  - `evidence`: An array of prior IntentRecords that triggered the proposal (including correction signals).
  - `eval_hash`: The signature confirming a successful pass through the hero_runner gate.

## **III. Source-of-Truth Hierarchy**

To eliminate concurrency deadlocks, file-locking, and merge conflicts, the Autonomaton is strictly forbidden from mutating operator-authored configurations. The system will utilize a layered configuration model mirroring the `/usr/lib` vs. `/etc` paradigm.

- **`routing.config.yaml`** (The Operator Root): This file is strictly human-authored. It represents the absolute, overriding truth of the system.
- **`routing.autonomaton.yaml`** (The Machine Root): This file is exclusively managed by the Dispatcher's write-hooks.
- **Resolution Precedence:** At runtime, the Dispatcher deeply merges the two files. In the event of a key collision, the value in `routing.config.yaml` strictly overrides `routing.autonomaton.yaml`.

## **IV. Approval Semantics & The Write Pattern**

The operator interacts with the flywheel via an Andon-aligned review surface (CLI or WebUI) that reads directly from `~/.grove/proposals.jsonl`.

- **Diff Presentation:** The operator is presented with the proposed modification strictly as a diff against the current `routing.autonomaton.yaml` state.
- **The Write Execution:** Upon operator approval, the Dispatcher applies a versioned diff exclusively to `routing.autonomaton.yaml`.
- **Conflict Immunity:** Because the machine only writes to its designated file, and the operator maintains precedence via `routing.config.yaml`, human intent is perpetually insulated from automated overwrites.

## **Normative References**

* GRV-005 — Dispatch Pipeline.
* GRV-006 — The Compositional Context Loop.
* GRV-007 — Declarative Prompt Composition.

## **Informative References**

* Sprint 12 — Haiku telemetry normalization.
* Sprint 28 — Intent capture v1.
* Sprint 30 — Escalation signal v1.
* Sprint 37 — Contextual preamble v1.
* Sprint 38 — Correction signal v1.
* Sprint 46 — Hero-prompts regression gate.
