# Main log — 2026-11-03: compiler-shapes-v1 (branch, deployed; not yet on main)

**For:** Divyansh, and anyone merging into or out of `main`. This one is different from the earlier entries: the work is on branch `compiler-shapes-v1` (11 commits on `main` at `3c98c0f2c`, head `98d0d02e9`), and **the gateway is running that branch**, not `main`, until Jim merges. `main` is unchanged.

**Bottom line:** the GL-coding loop now reads as a compiler pipeline on disk. A vanilla skill is a keg; the helper keg Kaizen compiles is a git repository with its replay beside it; the signature is a second commit that extends the Kaizen answer's hash chain across ledger files; `pattern_cache.db` is a derived build cache; and every keg decision names the rule that fired. Re-run live on 2026-11-03 (invoices 061–100): audit intact, V4→V5 is a two-hunk `git diff`.

## What landed, by commit

| Commit | What it does |
|---|---|
| `510f66cdb` | `scripts/deploy.sh --ref <branch>` puts a named branch on the gateway. Default stays `main`. |
| `088941afa` | `grove/keg_store.py` (new): the cellar at `$GROVE_HOME/cellar/`, a plain directory; each keg under `cellar/kegs/<slug>/` is its own git repo, versions as commits, one `manifest.yaml` shape for both kinds, `verify()` refuses a hand-edited keg. |
| `f3ce7a7c0` | `skills/gl-invoice-coding/SKILL.md` (new): the procedure as a plain Hermes skill. `python -m grove.keg_store seed-skill <slug>` commits it into the cellar as v1; a later edit re-seeds as v2. |
| `cc9e9cce4` | An isolated goal turn loads the goal's `unlocked_skills` from their kegs into a new composer section `goal_skill` (not tier-gated). The turn record and the turn's provenance carry `skills_loaded: [{keg_id, version, content_hash}]`, empty at T0. `DecisionWork.check_turn` refuses a model turn of a goal that unlocks a skill when none was loaded (`no_skill_loaded`). The `gl_coding` tool keeps the data contract; the procedure text moved to the skill. |
| `6497fe481` | `propose_keg` commits `keg.yaml`, `replay/v<N>.jsonl` and a manifest with build provenance (`compiled_by`, `compiled_under` the skill keg, evidence decision ids, `proposal_id`, `replaces`). The version id's hex is the content hash of `keg.yaml`. `_sign_keg` commits the manifest again as `active`. The `kaizen_proposal` event carries `not_covered`. |
| `68cb7613d` | `pattern_cache.db` keg rows are derived: `sync_cache()` at gateway start imports pre-cellar rows once and rebuilds keg rows from the cellar. Status changes outside a signature (halt, demotion, rejection, feedback) are committed to `transitions.jsonl` through a hook in `PatternCacheStore`. `cellar/kegs` joins the checkpoint state. |
| `352ee57ef` | `keg.rule_id(condition)`; keg decisions carry `rule: {rule_id, if}`, singly and in batches. |
| `505edf2b9` | A batch bumps `hit_count` per item; the turn that empties the queue finalizes its own record. |
| `93a6ef9f8` | The signing `kaizen_disposition` carries `prev_hash` of the `kaizen_answer` it closes; `verify_ledger_chain` and the audit verify the link across files (`cross_links`). |
| `8399050cf` | A non-string isolation value (a mocked session's meta) unlocks no skill. |
| `98d0d02e9` | Review fixes: a restore of a checkpoint that never knew a store archives the live copy; signing checks the keg's HEAD before the cache flips; an imported draft can be signed and rebuilt; `KegRepo.head()` raises on a git error; the escalation hot-swap carries `_goal_skills`. |

The sprint record (spec, plan, ledger with every ruling, DEVLOG) is local in `docs/sprints/compiler-shapes-v1/` (gitignored). Ask Jim or me for it.

## Merge hotspots

By likelihood of conflict with a branch off `main`:

- **`grove/dispatcher.py`** (again). `dispatch_turn` gains a goal-skill load right after `_turn_only_tools`; `turn_provenance` gains `skills_loaded`; `_write_intent_record` passes it; `compose_system_prompt` passes `goal_skills=`; `_maybe_recompose_for_tier` recomposes on a skill-set change; `_session_intercept` self-finalizes when nothing is armed; `_grant_escalation_hot_swap` copies `_goal_skills`. Two new module-level helpers above the class: `_session_work_is_done`, `_skills_loaded`.
- **`grove/pattern_cache.py`:** `set_status` and `set_promotion_evidence` now call `_note_transition` after the UPDATE. If your branch adds a status transition for a keg row, it is recorded in the keg's `transitions.jsonl` automatically; if it is part of a signature, wrap it in `keg_store.signing()`.
- **`grove/flywheel_cli.py`:** `_approve_pattern_promotion` is split: it checks `check_signable` first, then activates and signs inside `signing()`; the second half is `_activate`. `_sign_keg` calls `sign_helper` and adds `keg_commit` to `new_standard_work`. `_record_kaizen_disposition` looks up the `kaizen_answer` by `artifact` and seeds `first_prev_hash`.
- **`grove/eval/pattern_compiler.py`:** `propose_keg` takes `evidence_decisions=`, computes the id from `keg_store.helper_document_hash(spec, lineage)` (not `rules_digest`), and commits to the cellar after the queue append. `backtest_keg` cases carry `decided_id`.
- **`grove/kaizen_ledger.py`:** `record(first_prev_hash=...)`; `verify_ledger_chain(lines, archived, external=...)` returns `cross_links` and a new problem string, "the event this one follows is in no ledger"; new `find_event(ledger_dir, **match)`.
- **`grove/audit.py`:** `_ledger_check` is two-pass (index verified hashes, then verify each file against the rest); the `ledger` block of the report gains `cross_links`. If you pin the report dict exactly, add the key.
- **`grove/intent_store.py`:** `IntentRecord.skills_loaded: Tuple[Dict[str, str], ...]`, hashed; `_parse_line`/`append` coerce it.
- **`grove/decision_work.py`:** `DecisionWorkConfig.unlocked_skills`; `check_turn`'s skill check; `record(rule=...)`; `rule_that_fired`; `history()` cases carry `proposed_id`/`decided_id`; `batch_pass` records hits.
- **`grove/checkpoints.py`:** `STATE` gains `cellar/kegs`; `restore_now` treats a store the checkpoint never declared as absent.
- **`tools/gl_coding_tool.py`:** `_next` no longer returns `instructions`; the schema description is the data contract. Two old test pins moved to the SKILL.md text.
- **Tests that changed expectations:** `tests/grove/test_audit.py` (ledger dict), `tests/grove/test_andon_invariants.py` (per-file verify now passes `external`), `tests/tools/test_gl_coding_tool.py` (schema pins).

If your branch adds a keg proposal path, pass `evidence_decisions` and expect a cellar commit. If it adds a Dock goal with `unlocked_skills`, seed the skill on the node first or every model turn of that goal stops loud.

## What the live node carries that the repo does not

- Branch `compiler-shapes-v1` at `98d0d02e9` deployed (`deploy.sh --ref`).
- `~/.grove/cellar/kegs/gl-invoice-coding-skill/` (1 commit, seeded from the repo) and `~/.grove/cellar/kegs/invoice-gl-coding/` (127 commits: 125 imported pre-cellar versions, then V5's proposal and signature).
- `~/.grove/dock/dock.yaml`: the `gl-invoice-coding` goal has `unlocked_skills: [gl-invoice-coding]` (backup `dock.yaml.before-skill-20261103T210104Z`).
- Checkpoints: `demo-nov3-run43` (today's first run, saved before the restore), `demo-before-month-3` (the demo start, restored today), plus the October ones.
- Tier bindings, caps and the vision model as in the 2026-10-24 entry.

## Two follow-ups Jim flagged for the next branch

1. **`check_turn` verifies that a skill loaded, not that the governed skill loaded.** It refuses an empty `skills_loaded`; it does not compare the loaded `keg_id` against the goal's declared slug (via `keg_store.skill_repo_path`) or its current content hash. Production wants "the governed skill loaded": compare `skills_loaded` to `skill_kegs_for_goal(goal)` by id and hash and refuse on a mismatch. Zone: Green (a stricter refusal on an existing Jidoka path). Test: a turn whose provenance names a different skill id is refused.
2. **Asset hash = spec only.** Today the keg id's hex covers `keg.yaml`, which is the spec plus `lineage` (the run it was earned in), so identity changes with provenance. Two docks that compile identical rules get different ids, and the same asset cannot be recognized as the same asset. The fix: hash the spec alone; move `lineage` to the manifest beside `evidence`; keep "same rules re-earned in a later run" as a manifest fact, not a new id. Zone: Yellow (keg identity changes; ids on record need a mapping, and `propose_keg`'s "already drafted" check must then read lineage from the manifest). The sprint DEVLOG carries the same note.

## Deferred, from the final review

No file lock around `KegRepo.commit()` (a killed process can leave `index.lock`); `_reject_pattern_promotion` swallows exceptions, which now includes a git failure; `sync_cache` does not reconcile rows present in both cache and cellar; self-finalize loses the `correction` outcome for the last session step; about 10 git subprocesses per isolated turn, T0 included; a start-up sync failure is logged `critical`, not surfaced to the operator. Internal identifiers `backtest_keg`, `KegBacktestDetail`, `kind: keg_backtest` still say backtest; everything on disk and in the ledger says replay.

## How it was QA'd

- TDD per task, 44 new tests; full-suite set-diff against `main` by patch file (no stash): no regression (the branch-only ids were six mocked-session tests, fixed in `8399050cf`, plus two `test_retrieval_broker` tests that fail serially on `main` too).
- Fresh-context review of the whole branch: 1 Critical, 4 Important, all fixed in `98d0d02e9` with tests that failed first.
- Live: restore `demo-before-month-3` → deploy → `imported=125 rebuilt=0` → seed → Dock edit → Jim ran 061–100 on Telegram with the portal approval. Chain report intact; `1 signature follows the answer it closes, across files`; V5 `hit_count: 34`; V4→V5 `keg.yaml` diff is two hunks.
