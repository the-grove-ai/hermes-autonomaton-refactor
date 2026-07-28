# Engineering Canon — wiki-writer-structured-output-v1

Operational guidance earned live across the sprint's four build phases
(P0 transport spike, P1 emit_package contract + P1.1 zone fix, P2
Writer/Editor forced-tool, P3 watcher per-file quarantine). Mirrored
from the operator's local CLAUDE.md engineering-canon section (which is
deliberately untracked — .git/info/exclude: not for upstream).


### Offering ≠ execution

- Tool trust roots are SEPARATE: registered + ceilinged (L2 floor) +
  offered (L1 allow-list) does NOT mean zoned. An undeclared tool
  defaults Yellow; a grant-less worker's deny handler refuses it
  ("That action needs your approval." — live bake
  p1bake20260710aaaaaaaa).
- Any new fleet-floor tool needs a `config/zones.schema.yaml`
  tool_zones declaration — policed by the fleet-floor zone conformance
  pin (parses the floor from Dispatcher source, classifies through the
  REAL ZoneClassifier path).

### Harness-stubbed Dispatcher

- run_worker tests stub the Dispatcher: execution-side governance NEVER
  runs in local pins. A local green covers offering + staging + events —
  not zoning. Execution-side conformance requires in-process pins
  through the real classification path; local pin greens do not cover
  zoning.

### OpenRouter finish_reason lies

- Top-level `finish_reason` reports "tool_calls" even when the response
  was cap-truncated; the truth is `native_finish_reason: "length"`
  (wire-byte confirmed, P0 spike). Truncation guards read the native
  field.
- Identical-at-cap retry is DETERMINISTIC failure (0/6); retry at a
  raised cap (2/2). Ladder shape: parse-fail → cap-check → raised-cap
  retry (bounded) → Andon.

### Transport canon

- Schema-bound tool emission over free-text sentinel/frontmatter parsing
  for ANY model-output→structured-data seam. JSON tool args are
  byte-faithful at 12KB on the live T1 route and truncation is
  deterministically catchable at the json.loads seam; prose transport
  can degrade silently (P0 spike evidence,
  wiki-writer-structured-output-v1).
- Splice-debt note: the sentinel protocol was Atlas-era transport; its
  failure class is splice debt, not design debt.
