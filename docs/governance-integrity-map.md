# Governance Integrity Map

**Last forensic audit:** Sprint W3.0a (`governance-integrity-audit-v1`), 2026-05-24.

**Lodestar:** Architecture is the guarantee; policy is the promise. The pipeline
is immutable — it runs on every turn, no exceptions.

This document is the canonical inventory of the production paths from operator
input to a model API call, and the seven invariants every path must satisfy.
It is living: when a new mode is added, this map gets a new row. When an
invariant is added, this map gets a new column. The invariant tests in
`tests/test_w3_0a_governance_invariants.py` are the enforcing complement —
they break when someone wires a shortcut.

## The seven invariants

| ID | Invariant | Why it matters |
|---|---|---|
| **I1** | No ungoverned path to a model API. | Every user input passes through `grove.providers.route_for_agent`. A path that skips routing is not governed by the autonomaton, regardless of what telemetry it emits. |
| **I2** | Routing decision governs actual selection. | The model string in the API call **matches** `RoutingDecision.tier_config.model`. A `RoutingDecision` that does not bind the next API call is advisory, not governance. |
| **I3** | Classification result emitted. | Every routing decision emits a `routing_decision` telemetry event enriched with the classification fields. Verified at the **telemetry emission boundary** (`grove.providers.log_routing_decision`), not at the provider API boundary. |
| **I4** | Zone checks unsuppressible. | No `try/except`, no fallback, no silent-degradation path wraps zone classification on tool actions. If the zone check errors, the action does not proceed. This is fail-closed by architecture, not policy. |
| **I5** | Sovereignty gate non-bypassable. | When a Red-zone action fires, execution halts. Downstream code does not run the action regardless. The gate is binding, not advisory. |
| **I6** | Exactly-once classification per turn. | Each turn produces exactly one T-telemetry classifier call. Not zero (skipped). Not two (double-fire from CLI pre-route + AIAgent self-route). |
| **I7** | Operator preference feeds the router, never bypasses it. | A non-empty model, `--model`, or `operator_model` is an **input** to `route()`. It is resolved to a tier *within* the pipeline (`operator_model_preference` / `operator_model_untiered` reasons). It never causes the pipeline to be skipped. |

## The 13 production paths

| # | Path | Entry call site | Pre-routes? | Passes `already_routed=True`? | Self-routes via AIAgent (W3.0)? |
|---|---|---|---|---|---|
| **P1** | CLI interactive chat | `cli.py:11290` | yes (`cli.py:4394` via `_resolve_turn_agent_config`) | yes (`cli.py:11300`) | n/a — caller pre-routed |
| **P2** | CLI batch / eval | `cli.py:14484` | yes (same pattern as P1) | yes (`cli.py:14490`) | n/a |
| **P3** | CLI background agent (`bg_agent`) | `cli.py:8493` | parent pre-routes on the `/bg` command; bg prompt is a fresh turn | no | yes — on bg prompt |
| **P4** | CLI oneshot | `oneshot.py:430` (`agent.chat`) | yes (`oneshot.py:359`) | yes (`oneshot.py:433` via `chat()`) | n/a |
| **P5** | Webui | `api/streaming.py` / `api/routes.py` (in webui repo) | no | no | yes |
| **P6** | ACP adapter | `acp_adapter/server.py:1248` | no | no | yes |
| **P7** | Gateway core | `gateway/run.py:10674`, `15718` | no | no | yes |
| **P8** | Gateway platforms (Feishu comment, API server) | `gateway/platforms/feishu_comment.py:1089`, `gateway/platforms/api_server.py:2739`, `2993` | no | no | yes |
| **P9** | TUI gateway | `tui_gateway/server.py:3251`, `3672` | no | no | yes |
| **P10** | Cron scheduler | `cron/scheduler.py:1497` (via thread executor) | no | no | yes |
| **P11** | Batch runner | `batch_runner.py:349` | no | no | yes |
| **P12** | Delegate tool / sub-agents | `tools/delegate_tool.py:1502` | no | no | yes |
| **P13** | Curator review fork (Kaizen / agent) | `grove/kaizen/curator.py:1842`, `agent/curator.py:1720` | no | no | yes |

The convergence point on the agent layer is
`AIAgent._maybe_route_for_turn` (`run_agent.py`), called at the top of
`run_conversation` unless the caller has set `already_routed=True`. Both
paths — CLI's external `_resolve_turn_agent_config` route and AIAgent's
internal self-route — call `grove.providers.route_for_agent`. Single
function, no parallel implementation.

## Integrity map — invariants × paths

All cells verified by the invariant tests in
`tests/test_w3_0a_governance_invariants.py` (which use boundary
instrumentation rather than mocking the checkpoints themselves) and by
file-line reading.

| Path | I1 | I2 | I3 | I4 | I5 | I6 | I7 |
|---|---|---|---|---|---|---|---|
| P1 CLI interactive | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| P2 CLI batch | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| P3 CLI bg_agent | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| P4 CLI oneshot | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| P5 Webui | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| P6 ACP | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| P7 Gateway core | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| P8 Gateway platforms | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| P9 TUI gateway | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| P10 Cron | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| P11 Batch runner | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| P12 Delegate / sub-agents | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| P13 Curator review | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

Zero NO cells. All invariants hold across all paths.

## Bypass history

### I4 bypass (`tools/approval.py:1178-1182`) — closed by W3.0a

**Pattern:** `try/except` around zone classification fell through to the legacy
approval flow on any classifier error, with the comment *"the existing approval
flow IS the safety net."*

**Why it was a bypass:** zone classification errors did not block the action.
The legacy approval flow (`tirith`, dangerous-pattern detection, smart-approval)
could let an action through that the zone classifier would have blocked. That
is silent degradation to a permissive state.

**Fix:** the `except` now returns `{"approved": False, "classifier_failed": True, …}`
with an actionable diagnostic. The action does not proceed when the classifier
errors. Fail-closed by architecture.

**Visible to operators:** transient classifier failures (network, schema, config)
now block tool calls instead of silently permitting them. The block message
names the failure mode and points at `config/zones.schema.yaml`, `grove/dispatch.py`,
and `grove/zones.py` for diagnosis.

### I4 preserved through S22 zone-parameter evolution

Sprint 22 evolved `tool_zones` from a flat mapping (`terminal: yellow`)
to optional hierarchical entries with argument-level rules
(`terminal: {default_zone: yellow, rules: [...]}`). The new
`grove.zones.ZoneClassifier.classify_command_string` method and the
new `grove.zone_rules.synthesize_pattern` / `save_zone_rule` write
path enlarged the surface, but the I4 fail-closed envelope at
`tools/approval.py:1182-1211` is unchanged — any exception inside
the classifier (legacy `classify(action)` path OR the new
hierarchical path) still produces `approved=False, classifier_failed=True`.

Additional Sprint 22 protections that reinforce I4 without modifying
its definition:

- **Per-rule load-time rejection** of ReDoS-vulnerable patterns
  (`(a+)+`, `(.*)*`), universe-matchers (`.*`), excessive
  alternation, over-long patterns, and syntactically invalid regex.
  Bad rules are dropped with a loud log; the rest of the schema
  still loads. A vulnerable pattern cannot reach the matcher in
  production.
- **Synthesis denylists** — `synthesize_pattern` refuses to produce
  permanent allowlist patterns for privilege-escalation verbs
  (`sudo` / `su` / `doas` / `pkexec`), root-level catastrophic
  shapes (`rm -rf /`, `chmod 777 /`, `dd of=/dev/sda`), and
  sensitive system directories (`/etc`, `/bin`, `/usr`, `/var`,
  `/sys`, …). These shapes never make it into a written `save_zone_rule`
  call.
- **Last-known-good snapshot on reload** (existing Sprint 04
  behaviour, now snapshotting the richer `_tool_zones_rich` map too)
  so a botched operator edit doesn't wipe the in-memory zone
  configuration.

Test coverage: the existing W3.0a invariant tests
(`tests/test_w3_0a_governance_invariants.py`) cover I4 at the
classifier-error level. S22-specific verification lives in
`tests/grove/test_s22_zone_evolution.py::TestI4Preserved` (3 tests:
last-known-good restore on bad reload, unknown-tool yellow default,
classifier-exception fail-closed). See
`docs/design/zone-model-s22.md` for the full S22 reference.

## Boundary instrumentation reference

For future invariant tests:

- **Telemetry emission boundary:** patch `grove.providers.log_routing_decision`
  (the module-imported reference). Patching `grove.telemetry.log_routing_decision`
  does not intercept calls because `grove/providers.py:30` imports it directly.
- **Provider boundary for I2:** check `agent.model` after `_maybe_route_for_turn`
  fires; it MUST equal `RoutingDecision.tier_config.model`.
- **Classifier-invocation boundary for I6:** patch `grove.classify.classify_for_routing`
  with a counter — `route_for_agent` calls it via local import each invocation,
  so the source-module patch works.
- **`already_routed` gate for I6:** the gate is structural — `run_conversation`
  checks `if not already_routed:` before calling `_maybe_route_for_turn`. Tests
  mimic that flow rather than patching the gate itself.

## Maintenance discipline

- New mode added: add a row to the path table; add an integration test that
  exercises the path through `run_conversation` (W3.0 self-route closes it
  automatically, but explicit verification is worth doing).
- New invariant added: add a column; add a test class in
  `tests/test_w3_0a_governance_invariants.py` instrumented at the right
  boundary.
- Bypass discovered: log it in the **Bypass history** section above when fixed.
  Do not delete old bypass entries — they are the institutional memory of
  what was almost permanent.
