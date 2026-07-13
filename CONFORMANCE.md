# CONFORMANCE

**Standard:** [GRV-001 — The Autonomaton Pattern, v2.0 (2026-06-22)](https://the-grove.ai/standards/001)
**Implementation verified at:** repo/VM HEAD `37342aa08`, 2026-07-13
**Grove suite at verification:** 3,497 / 3,497 passing

## How to read this document

GRV-001 distinguishes its **structural commitments** — the pipeline shape, telemetry format, zone semantics, and substrate ownership an implementation must hold — from its **reference schemas**, which the standard itself declares illustrative rather than exhaustive. Conformance, by the standard's own definition, is structural.

This document holds the implementation to the stricter reading anyway: every element of the v2.0 reference architecture is mapped below as **Implemented**, **Implemented (enhanced mechanism)**, **Partial (declared)**, or **Open (declared)**. Where our mechanism differs from the standard's reference mechanism, the difference is argued, not hidden. Where work is unfinished, it is named. Nothing on this page is silent.

## Element map

### (a) Five-stage pipeline as invariant

**Status: Implemented, one declared exception.**

Every agent intent traverses Recognition → Compilation → Approval → Execution, with Telemetry as the cross-cutting spine each stage writes into. No agent-reachable path skips a stage; approval verdicts are produced by the dispatcher, never the model.

Declared exception: the memory subsystem's tool schemas are injected at agent construction through a provider path that predates capability-record admission. The tools' *invocations* still traverse the full pipeline (classify, zone, execute); the exception is at the admission surface, not the execution path. Bringing this path under capability-record admission is a scheduled, named sprint.

### (b) Zone classification keyed on operator scope

**Status: Implemented (enhanced mechanism).**

The v2.0 prohibition on category-keyed zoning exists to protect one surface: mutation. This implementation keys **every write** by what it changes — write targets are resolved and classified by scope (workspace-confined, governed, scope-defining) before any tool-name heuristic applies, on both the shell and file-tool paths. Category keying survives only for non-mutating dispatch, where scope is undefined by construction.

### (c) Routing authority separation

**Status: Implemented (enhanced mechanism); field-level separation open (declared).**

The standard's reference mechanism is a file split: `routing.operational` / `routing.authority`. This implementation runs a single routing config and delivers the separation the split exists to guarantee at the **write wall** instead: any agent write targeting the routing config — or any other scope-defining authority file, including the zones overlay, routing profiles, and fleet enablement — classifies **Red on every surface**, is re-verified at execution time against its approved effect signature (defeating classification-to-execution swaps), and can execute only through operator-approved re-dispatch. Mutations by the sanctioned operator writer are channel-authenticated and self-audited to the ledger with `surface_class: scope_defining`.

This is verified two ways: deterministically (the wall's test set, including `test_write_file_to_scope_defining_is_red` and the execution-guard suite in `test_scope_wall_execution`) and live — production probes writing to the routing config landed as durable Red proposals with the config byte-identical afterward.

Open work, declared: the routing file remains one undifferentiated surface to the classifier. Field-level separation of tier bindings from governance policy within it is tracked as the `routing-authority-separation` debt item.

### (d) Grant tokens

**Status: Partial (declared).**

Grants carry scope, write class, disposition (once / session / standing), and a revocation handle; standing grants are reviewable and revocable at any time. Authentication of consent is by **channel origin** — the standard's own clause scopes a grant's strength to the strength of the consent's authentication, and operator-authenticated channels are that mechanism here. Signing-key authentication of grant issuance is declared open.

### (e) Provenance stamps on agent writes

**Status: Partial (declared).**

Provenance stamping is implemented where authority moves: Red-grant executions (stamped with actor, target, and `surface_class`), skill promotion proposals, and every mutation by the sanctioned routing and binding writers (each files a self-audited ledger event identifying its target and surface). Universal per-write stamping with the standard's full field set, including `source_chain`, is declared partial and tracked.

### (f) Confused-deputy read-closure check

**Status: Open (declared).**

The v2.0 read-path declaration and Approval-stage disjointness test are not implemented. One vector class in this family is already mitigated structurally — dormant routing profiles, a pre-poisoning target, are inside the Red write wall — but the general mechanism is future work and is named as such. This is the largest declared gap on this page.

### (g) Tier semantics

**Status: Conformant via enhancement.**

The standard's reference table binds T1 to a local model. This implementation treats all tiers as **competence envelopes** whose bindings are operator configuration — one config line per tier, any OpenAI-compatible provider, local or hosted. The local-T1 binding the standard depicts is one available choice (MLX and Ollama paths are supported), not a structural requirement; the structural commitment — dispatch to tiers, never to models — is held everywhere, including per-skill model binding under governed rebind. Tier naming here (Pattern Cache / Cheap / Premium / Apex) differs from the standard's (Local / Cloud-Fast / Cloud-Frontier); the semantics map one-to-one.

## Summary

| Element | Status |
|---|---|
| (a) Five-stage pipeline invariant | Implemented — one declared admission-surface exception, remediation scheduled |
| (b) Scope-keyed zoning | Implemented (enhanced) — all writes scope-keyed; category only for non-mutating dispatch |
| (c) Routing authority separation | Implemented (enhanced) — Red write wall + execution re-verification; field-level split open |
| (d) Grant tokens | Partial — channel-origin authentication; signing keys open |
| (e) Provenance stamps | Partial — stamped where authority moves; universal stamping open |
| (f) Confused-deputy read-closure | Open — declared; one vector class structurally mitigated |
| (g) Tier semantics | Conformant via enhancement — tiers as envelopes, bindings as operator config |

## Challenging this document

If you believe any status above overstates the implementation, open an issue citing the element letter. Claims here are held to the same discipline as the README: anchored to the tree and to test evidence at the stated HEAD, and corrected loudly when wrong.
