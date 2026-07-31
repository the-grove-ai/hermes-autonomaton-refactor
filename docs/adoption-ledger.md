# Adoption Ledger — hermes-severance-v1

Explicit keeps carry written reasons; debts carry names. Silent keeps do not
exist in this tree (GRV-001 §V: adopted-with-reason).

Source anchors are at HEAD `20a99abf4` (T4 tip) plus the T5 changes on top.

## Adopted

- **sync-operator.sh** — `scripts/sync-operator.sh`. Live operator-artifact sync
  (Mac→VM governance artifacts); referenced by kept core
  (`agent/curator.py:502`, `grove/kaizen/rendering.py:745`);
  operator-performed-write class.
- **scripts/rubric_tool.py** — operator authoring aid for the rubric surface;
  git+deploy is its conformant write path today; revisit when rubric mutation
  joins the proposal types.
- **scripts/mlx-harness/** — local-T2 measurement harness; Sprint 71→77 verdict
  provenance; XML→tool_calls binding gate pending.
- **Dockerfile (+ docker-publish-class workflows)** — `Dockerfile`,
  `.github/workflows/docker-publish.yml`. Deployment container image; release
  infrastructure (kept by function per R-T5.2).
- **scripts/release.py** — release infrastructure; the pypi path
  (`.github/workflows/upload_to_pypi.yml`) is the distribution goal.
- **skills/ + optional-skills/** — provenance source for capability records;
  instance-deploy origin.
- **Provider layer** — `providers/base.py`, `agent/*_client.py`. Model
  independence is a published conformance property, not theater. The
  copilot-acp adapter (`agent/copilot_acp_client.py`) was the sole exception,
  **DELETED in full (T5)**. The severance covers BOTH roles the `copilot-acp`
  string carried: (a) the dead selectable-**provider registration** — the ~8
  hermes_cli declarations (ProviderConfig / ProviderEntry / catalog / overlay /
  labels / aliases / CLI choices / `_model_flow_copilot_acp`), the
  `external_process` auth helpers (`get_external_process_provider_status` /
  `resolve_external_process_provider_credentials`, dead once copilot-acp was
  their only subject), and the `auxiliary_client` provider-resolve branch —
  unreachable per the auto-resolve trace (external_process filtered at
  `models.py` auto-inject; explicit string absent from both machines' configs);
  and (b) the live ACP **transport** — the `run_agent` ACP client constructor
  (`_create_openai_client`), the `acp_command`/`acp_args` attribute threading
  (run_agent, cli.py, cron), the `delegate_task` ACP params + schema, and the
  `tips.py` advertisement — removed as an **unsanctioned second delegation
  path**, NOT by the auto-resolve trace. Reason: a raw ACP-subprocess spawn is
  an ungoverned, unaudited execution surface (competes-with-substrate class;
  execute-code-containment debt class); sanctioned agent composition is the
  governed fleet (rubric-gated workers) and GRV-004 StreamableHTTP MCP nodes
  (grove-browser precedent). A Mylo→Claude-Code path, if it returns, returns
  governed through GRV-004, not an inherited spawn. `delegate_task` itself
  survives with every non-ACP mode intact (single / batch / orchestrator role,
  provider / base_url / api_mode overrides, credential-pool + fallback
  inheritance). The `external_process` auth_type *enum token* is retained as
  inert type vocabulary (generic auto-inject skip-set + type comment; no
  provider uses it) — the retired-Platform-enum pattern.
- **run_agent module** — `run_agent.py`. AIAgent's home (9 dispatcher-core
  import sites); console entry point removed; extraction filed as future work.
  The copilot-acp ACP client constructor and the `acp_command`/`acp_args`
  attribute threading were severed with the ACP transport (see Provider layer).
- **Retired Platform enum members (19)** — `tests/_retired_platforms.py:21`
  (`RETIRED`). Vocabulary retained for the kept-code references; the retirement
  pin proves no adapter exists; dead-guard sweep owns their eventual removal.
- **tavily / edge-TTS / local-STT** — `plugins/web/tavily/provider.py`,
  `tools/tts_tool.py` (edge-tts), the `voice` extra (faster-whisper). The
  serving backends on the named machines.
- **Managed passthroughs (web/tts/browser)** — `tools/web_tools.py:74`
  (`managed_nous_tools_enabled`). Functional behind `managed_nous_tools_enabled`;
  modal feature severed; full surface adjudication deferred to the
  provider-prune horizon.
- **`plugins` subcommand** — `hermes_cli/main.py:10696` (`plugins_parser`). The
  operator surface for a kept mechanism; removing it would force hand-edited
  config (banned pattern).
- **Windows/Nix CI** — REJECTED with reason: not deployment targets (Linux prod,
  macOS dev); model independence is a provider-swap property, not an OS matrix.
  Deleted in T5: `.github/workflows/nix.yml`, `nix/`, `flake.{nix,lock}`,
  `.github/actions/nix-setup`, and the Windows-runner test coverage.

## Named debt

- **hooks mechanism** — `gateway/run.py:3701` (SECURITY-DEBT header) →
  `:3718` (`register_from_config`). LIVE-DORMANT ungoverned execution surface
  (loads at gateway boot from the instance hooks dir); SECURITY DEBT; removal
  rides the dead-guard sweep.
- **Platform-enum dead-guard sweep** — `tests/_retired_platforms.py:21`. The
  retired-member references, teams_pipeline wiring, file_tools env-type
  branches, backend config keys; sequenced with run.py decomposition.
- **Hollow skills** — kept skill files (`skills/`, `optional-skills/`) invoking
  deleted tools; each curates to kept tools or retires.
- **Fixture-authoring modernization** — `tests/grove/routing_fixtures.py`. Six
  tests author v1-shaped fixtures through the relocated translator; author v2
  directly, delete the helper.
- **Surviving red tests** — the R-T5.1 named-debt list: the baseline-red files
  that pin KEPT behavior and stay honestly red. **T5-close full suite
  (`-n auto`): 175 failed / 22,766 passed / 47 xfailed / 23,186 collected.**
  The 175 is baseline-180 (`d35b0b51`) minus reds that lived in T1–T5 deleted
  files; **zero net-new deterministic failures from the copilot-acp severance**
  — every severance-edited test file passes, and the only edited-file failures
  (`test_run_agent_codex_responses` ×9: codex/xai/copilot 401-refresh +
  reasoning-response) reproduce in `-n0` isolation and are logically
  pre-existing (the removed Responses-rewrap conjuncts are tautologically true
  for non-ACP providers, so the boolean is unchanged for codex/xai/copilot).
  Exact final clusters: **AIAgent core** (`tests/run_agent/*` — test_run_agent
  ×33, codex_responses ×9, file_mutation_verifier ×7, openai_client_lifecycle
  ×4, memory_sync_interrupted ×4, commit_memory_session_context_engine ×4,
  860_dedup ×4, background_review/background_review_toolset_restriction/
  background_review_cache_parity ×8, 413_compression/compression_boundary_hook/
  compression_persistence ×6, token_persistence_non_cli ×2,
  anthropic_error_handling ×8, interrupt_propagation, tool_call_guardrail_runtime,
  dict_tool_call_args); **gateway** (`test_api_server`, `test_approve_deny_commands`
  ×6, `test_dm_topics` ×2, `test_telegram_mention_boundaries`); **fleet**
  (`test_fleet_*` — state_card_emission ×4, artifact_proposal ×3, promote_route
  ×2, emit_contract ×2, tool_floor, offering_override); **cron** (`test_scheduler`,
  `test_codex_execution_paths` ×2); **tool guards** (`test_command_guards` ×7,
  `test_file_operations` ×4, `test_file_read_guards` ×6, `test_file_staleness`
  ×3, `test_file_state_registry` ×2, `test_skill_improvements` ×5,
  `test_modal_sandbox_fixes`, `test_web_providers` ×2); **grove**
  (`test_producer_recurrence`, `test_credential_pool`, `test_display` ×8,
  `test_prompt_builder`, `test_skills_config` ×2, `test_skin_engine`,
  `test_startup_plugin_gating`, `test_tools_config`, `test_kanban_cli`,
  `test_binding_opacity_guard` ×2, `test_wiki_watcher`); plus `test_cli_approval_ui`,
  `test_live_system_guard_self_test` ×4, `test_lint_config`, `test_plugin_discovery`.
  `test_percentage_clamp` now PASSES (dropped from the red set).
- **test_website_policy** — `tests/tools/test_website_policy.py`. firecrawl
  extract/crawl policy coverage lost with the backend.
- **docs/standards/** — absent, pending an operator-sourced snapshot of
  GRV-001..004 + the Ratchet from the live site.
- **Windows POSIX-subset coverage** — RESOLVED (FLAG 2 clean lift, not
  whole-delete): the POSIX-invariant `_pid_exists` / process-registry OSError
  widening was migrated verbatim into `tests/tools/test_process_registry.py`
  (`TestProcessRegistryOSErrorWidening`) and `tests/gateway/test_status.py`
  (`TestPidExistsOSErrorWidening`); no coverage lost. Debt line VOID.
- **openclaw-migration hardening orphan** — RESOLVED inline (T5 close). The
  staged openclaw deletion (`optional-skills/migration/openclaw-migration/` +
  `tests/skills/test_openclaw_migration.py`) missed the sibling
  `tests/skills/test_openclaw_migration_hardening.py`, which loads the deleted
  `openclaw_to_hermes.py` via `spec_from_file_location` inside each test body —
  26 deterministic `FileNotFoundError`s, surfaced by the first T5-close
  full-suite run. `git rm`'d as a mechanical orphan fix (subject fully deleted,
  cross-sprint-correction precedent). Debt line VOID.
