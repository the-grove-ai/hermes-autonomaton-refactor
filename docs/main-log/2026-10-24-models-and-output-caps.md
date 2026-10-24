# Main log — 2026-10-24: new models, clean rollouts, and output caps that follow the model

**For:** anyone working on a branch off `main`. `main` stays the demo branch. This note says what changed on it today, what changed on the live node that the repo does not carry, and what to expect when you merge or rebase.

**Bottom line:** Mylo now behaves more like a production agent harness. A model's answer finishes in one call instead of being stitched together, every model call leaves a trace, the provider's real charge is recorded, and coding routes by complexity like everything else. Everything is deployed (`7cce77a8c`). It was QA'd against the live invoice harness, and the month 3 demo smoke on 2026-10-24 ran clean.

## What landed on main

Each commit message states its behavior in one sentence, oldest first.

| Commit | What it does |
|---|---|
| `b53b90fc1` | Adds DeepSeek V4.1 Flash 1017 to the fast-tier menu. The plain V4.1 Flash is deprecated. |
| `50886f798` | Claude Haiku 5.5 replaces Haiku 4.5 on the menu for Telemetry, T1 and T2. |
| `9e5e42ece` | DeepSeek 1017 ships `active`, not `candidate`. One rule for every vendor: a new snapshot of an active model ships active, and the old one is deprecated with `superseded_by`. |
| `14cbf7ff7` | Portal model menus show name and price only. The card states the bound model's standing once, on its flag line. The ad hoc "(candidate)", "(deprecated)" and "(not in catalog)" suffixes are gone. |
| `a1ab4a336` | Kimi K3.1 Pro replaces Kimi K3 on the menu for T2 and T3. |
| `5dea40b7c` | Coding routes by complexity like all other knowledge work. `premium_coding` and `apex_coding` are removed; `premium` (T2) takes simple and moderate, `apex` (T3) takes complex and novel. |
| `1d812e28c` | Every model call leaves a trace. A call that hits the output cap now gets its own "API call #" line, counted tokens and cost, and a WARNING. The turn record carries `call_reasons` beside `call_ms`. |
| `7cb090e9c` | A provider charge of $0 is not taken at face value. On a bring-your-own-key route the charge is `cost + cost_details.upstream_inference_cost`. Any other $0 for a call that used tokens is treated as unknown and priced from declared `model_facts`, with a WARNING. |
| `53bb242f5` | Every catalog model's `model_facts` entry declares `max_output_tokens`, read from OpenRouter. |
| `abdbf5b08` | The router resolves each model tier's output cap at load. It uses the tier's own cap if set, else the model's limit, held to the model's limit and to `output_ceiling_tokens` (128000). |
| `b89bd74d7` | A length continuation keeps the resolved cap. The old 2x/3x boost up to 32768 is removed. |
| `8ceeac123` | Tiers follow their models' output limits. The seed's 4096 / 8192 / 16384 tier caps are removed. |
| `7cce77a8c` | Final-review fixes: a cap resolved for one model is never carried onto another (fleet retry, model pins, untiered operator models). |

## Why: the three defects we found

**1. Output caps were left over from May.** T1 was 4096, T2 8192 and T3 16384, set on 2026-05-21 for Haiku 4.5, Sonnet 4.6 and Opus 4.6. Today's models allow 128K or more. OpenRouter enables medium reasoning by default, and reasoning tokens count against the cap. Result: a long Haiku answer hit the cap twice and was stitched from three calls over 204 s.

The fix follows how production harnesses work:
- the cap follows the bound model, so a tier swap needs no further edit;
- one declared ceiling bounds a runaway answer;
- a tier can still set a lower cap on purpose.

**2. A truncated call vanished.** The length branch in `run_agent.py` exited before the per-call usage block. A truncated call therefore had no log line, no counted tokens and no counted cost, and its only notice went to stdout. That broke fail-loud and made turn totals wrong.

**3. Haiku cost read $0.** An Anthropic key is attached to the OpenRouter account. For Anthropic models OpenRouter reports `is_byok: true` and `cost: 0`, which is its own fee. The real charge, billed by Anthropic, is in `upstream_inference_cost`. The audit trusted the 0.

## Merge notes for branches

The hotspots, by likelihood of conflict:

- **`run_agent.py`.**
  - **Moved block:** the per-call usage block (the `if hasattr(response, 'usage') and response.usage:` block that logs "API call #") moved *above* the `if finish_reason == "length":` branch. If your branch edits either block, expect a conflict, and keep the new order.
  - **Continuation boost removed:** at `restart_with_length_continuation` the boost is gone, and the WARNING text changed.
  - **New reason tracking:** `self._next_call_reason` is set at retry sites, and `_turn_call_reasons` is appended beside `_turn_call_ms`.
  - **Stream stand-in:** the stand-in choice now carries `native_finish_reason`.
- **`grove/router.py`.**
  - **New:** `_OUTPUT_CEILING_DEFAULT`, `resolve_output_cap()`, `CognitiveRouter.output_ceiling()`, `CognitiveRouter.retarget()`, and a WARNING when bound facts declare no `max_output_tokens`.
  - **Changed:** `TierConfig.max_tokens` is now always an int for a model tier, and stays None only for handler tiers like T0. Code that treated `None` as "inherit the agent's cap" no longer sees `None` from the router.
- **`grove/dispatcher.py`:** resets `agent._turn_call_reasons` / `_next_call_reason` per turn, and writes `call_reasons` into the execution stage of the intent record.
- **`grove/fleet/worker_entry.py`:** a model pin goes through `router.retarget()`. The raised-cap retry is held to the model's limit via `_raised_output_cap()`, and is skipped (with a WARNING) when the cap is already at the limit.
- **`agent/usage_pricing.py`:** `_reported_cost()` handles bring-your-own-key and rejects an implausible $0.
- **`grove/api/fragments.py`:** `_model_options_html()` emits no status suffixes; the new `_model_flag()` owns the card's status line.
- **`config/routing.operational.yaml` (seed):**
  - tier `max_tokens` keys removed;
  - top-level `output_ceiling_tokens: 128000` added;
  - `max_output_tokens` added to every catalog model's facts;
  - coding folded into `premium` / `apex`.
- **`config/model-catalog.yaml`:** new entries for DeepSeek V4.1 Flash 1017, Claude Haiku 5.5 and Kimi K3.1 Pro. Their predecessors are deprecated with `superseded_by`.
- **Tests that changed expectations:**
  - `tests/grove/test_router.py`: coding at complex goes to T3, the reason is `premium`, plus the new cap tests;
  - `tests/grove/test_model_catalog_status.py`: no parentheses in menu options;
  - `tests/run_agent/test_run_agent.py`: continuation and accounting;
  - `tests/test_fleet_emit_contract.py` and `tests/grove/test_audit.py`.

If your branch adds a model to the catalog, give its `model_facts` entry a `max_output_tokens`. The pin `test_repo_model_facts_declare_output_limits` fails without it.

## The live node: what the repo does not carry

Deploy never writes `~/.grove`. These were applied on the VM through the governed routing writer (`RoutingConfigWriter.apply_mutation`) or the portal Swap action:

- **Tier bindings:**
  - T1 and Telemetry: `openai/gpt-6-luna`;
  - T2: `z-ai/glm-5.3` (Jim's portal swap; Haiku 5.5 before it);
  - T3 and T-QA: `moonshotai/kimi-k3.1-pro`;
  - T-GA: `z-ai/glm-5.3`.
- **Caps:** `output_ceiling_tokens: 128000`, no tier `max_tokens`. Every model tier resolves to 128000.
- **Model facts:** live `model_facts` carry `max_output_tokens`.
- **Routing:** coding folded into `premium` / `apex` (live rules differ slightly from the seed; `premium` there has no `research`).
- **Vision:** `auxiliary.vision.model` is `anthropic/claude-haiku-5.5`.

## How to roll out a model, now that it is settled

1. **Catalog first.** Add the entry to `config/model-catalog.yaml`, with its `model_facts`, including `max_output_tokens`. Deploy.
2. **Bind second.** Swap the tier with the portal's Swap action. It validates against the catalog, records `previous_model` and writes the ledger. Never hand-edit `tier_preferences`.
3. **Verify.** Check the next turn's `model_used`, `call_reasons` and `cost_reported` in `intent_records.jsonl`.

## How it was QA'd

Checkpoints before each stage (all on the VM):
- **Git tags:** `demo-good-2026-10-24`, `output-caps-stage1`, `output-caps-stage2`.
- **Node checkpoint:** `demo-good-2026-10-24`.
- **Config snapshots:** `~/config-checkpoints/`.
- **GCP disk snapshot:** `grove-data-demo-good-20261024`.

At each stage the QA run went like this:
1. Save a scratch node checkpoint.
2. Run one month of the invoice harness (`scripts/harness_run.py --policy ~/harness/policy.json --reset --seed 31 --months 1`) and fixed prompts across tiers.
3. Restore the scratch checkpoint, so no QA record stays in the live chain.

| Measure | Old caps | Caps follow the model |
|---|---|---|
| Invoice harness, one month | 385 s, 65 calls, $0.029 | 334–350 s, 53–55 calls, $0.016–0.035 |
| 6,000-word T2 answer | **2 calls** (cap hit), 70.8 s | **1 call**, 53–64 s |
| Truncation warnings | 1 | 0 |
| T3 coding (Kimi) | 273 s, $0.13 | 202–422 s, $0.10–0.19 (varies run to run) |

The tool-approval gate held throughout. Two QA turns stopped at Yellow (`operator_decision_required`) because the model asked to run a tool and nobody answers approvals on the API surface.

## Proposed fixes, not yet done

| Category | Proposal | Zone |
|---|---|---|
| Kaizen | **Hardcoded caps on structured calls** ignore tier config: classifier 280 (`grove/classify.py`), quality judge 4096, goal adjudicator 4096, Kaizen 1024–2048, wiki 1024–4096, vision 2000–4000. Declare them in config. Watch the classifier's 280 on reasoning models. | Yellow |
| Jidoka | **Validate malformed limits.** A negative or string `max_output_tokens`, or a non-int, 0 or bool tier `max_tokens`, should fail config load naming the key. Today a string raises a bare error and a negative is read as "undeclared." | Yellow |
| Jidoka | **Label low-cap continuations.** A continuation at a tier cap the operator deliberately set low should say so in its log line. | Green |
| Jidoka | **Overflow retry cap.** After a context-overflow retry, a later continuation returns to the full cap and may overflow again. Only costs bounded retries. | Green |
| Kaizen | **Restore the T1/T2 prompt contents.** `routing.authority.yaml` `tier_budgets` still strips `claude_contract` and `skills_index` from T1 and T2 prompts, a leftover from running T2 on a local model. Changes Mylo's behavior on most turns, so decide after the demo, with its own QA. | Yellow |
| Jidoka | **Approval log label.** Tool approvals on Telegram log as "Telegram Kaizen button: kaizen_id=…". Label them as tool approvals. The record itself is correct. | Green |
| Jidoka | **Gateway stdout buffering.** Without `PYTHONUNBUFFERED=1` in the gateway unit, stdout lines reach journald late or not at all. | Green |
| Jidoka | **Unclean stop.** The gateway exits status=1 on every stop (seen 2026-10-22 to 24). Diagnose. | Green |
| Flywheel | **Long-answer routing.** The classifier rates long-answer prompts `complex`, so they land on T3. Watch whether that matches operator intent. | — |
| Ops | **Missing limits for MiMo.** Two operator-catalog models (`xiaomi/mimo-v2.6-flash`, `-pro`) have no `max_output_tokens`. The router warns if one is bound. Add their limits. | Green |
| Ops | **Fleet file-size cap.** Fleet workers log `SQLITE_IOERR_WRITE` once `state.db` passes the 64 MB `fsize_mb` limit in `config/fleet_workers.yaml`. | Yellow |
