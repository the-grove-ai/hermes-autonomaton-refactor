# Zone Model — Sprint 22 (Hierarchical Tool Zones)

**Sprint:** 22 (`zone-parameter-evolution-v1`), 2026-05-24
**Status:** Shipped
**Supersedes:** the flat `tool_zones` mapping introduced in Sprint 04
**Touches:** `config/zones.schema.yaml`, `grove/zones.py`, `grove/dispatch.py`,
`grove/zone_rules.py` (new), `tools/approval.py`

## Why this exists

Sprint 04 classified actions at the tool level: every command flowing
through `terminal` was yellow, every Calendar read was green, every
`skill_manage.promote` was red. The classifier never inspected the
command's arguments. That was the right shape for v0.1 — easy to
reason about, easy for non-technical operators to edit — but it
collapsed a real distinction:

- `rm /tmp/cache.log` and `rm -rf /` are both `terminal yellow` under
  Sprint 04's flat model. One is a routine cleanup the operator
  approves a thousand times a year; the other is a system-destroying
  catastrophe. Treating them identically is what made the WebUI's
  "Approve Always" path so dangerous it had to be disabled in W5
  v0.1 — permanently allowlisting the rule-level `destructive_file`
  category would cover both shapes.

Sprint 22 evolves the schema from **tool → zone** to **tool → rule →
argument pattern**. The classifier now sees the full command string,
evaluates the tool's ordered rule list against it, and falls through
to a `default_zone` if nothing matches. Argument-level matches
override the default — `rm /tmp/cache.log` becomes green while
`rm -rf /` becomes red, both flowing through the same `terminal`
tool entry.

## Schema format

### Two forms coexist

Every `tool_zones` entry is **either** a bare zone string (the
Sprint 04 form, unchanged) **or** a mapping with `default_zone` plus
an ordered list of rules. The classifier handles both transparently;
bare-string entries produce identical behaviour to pre-Sprint-22.

**Bare-string entry (legacy):**

```yaml
tool_zones:
  calendar.read: green
  skill_manage.promote: red
  terminal: yellow              # every terminal command is yellow
```

**Hierarchical entry (Sprint 22):**

```yaml
tool_zones:
  terminal:
    default_zone: yellow
    rules:
      # Privilege escalation: always sovereign.
      - match_pattern: '^sudo\s+.*'
        zone: red
        reason: "Privilege escalation requires sovereign approval."

      # Catastrophic: hard-no even before the operator-level decision.
      - match_pattern: '^rm\s+-rf\s+/$'
        zone: red
        reason: "Catastrophic root-filesystem deletion."

      # Green: cleanup within /tmp is safe.
      - match_pattern: '^rm\s+(-[fir]+\s+)?/tmp/.*'
        zone: green
        reason: "Temporary directory cleanup is inherently safe."

      # Anything else flowing through terminal → yellow (default_zone).
```

### Rule evaluation: top-to-bottom, first match wins

The classifier walks the `rules` list in document order and returns the
first rule whose `match_pattern` `re.fullmatch`-es the command string.
**Write your most specific patterns first.**

If no rule matches, the result is `default_zone`. If the tool's entry
is a bare string (or absent from `tool_zones`), the classifier falls
through to the legacy dot-notation path (`zones.green.auto_approve`,
`zones.yellow.proposes`, `zones.red.sovereign`) — behaviour is
identical to pre-Sprint-22 for that tool.

### Pattern syntax

Standard Python `re.fullmatch` regex. Implicitly anchored — you do
**not** need `^` or `$`. Examples:

```
^sudo\s+.*                       # any sudo invocation
^rm\s+(-[fir]+\s+)?/tmp/.*       # rm of anything under /tmp
^chmod\s+\d+\s+/home/user/.*     # chmod within /home/user, any mode
^pip\s+install\s+requests$       # exact: only this command
```

### Safety guardrails (load-time)

The loader rejects per-rule, with a loud log, if a pattern:

- exceeds **200 characters**;
- matches everything (`.*`, `^.*`, `.*/.*`, `.+`);
- contains nested quantifiers (`(a+)+`, `(.*)*`) — classic ReDoS shape;
- has more than **10 alternation branches** in a single group;
- is not a syntactically valid Python regex.

The rest of the schema continues to load with the bad rule dropped.
This is the **one** SPEC-commanded graceful degradation in the zone
loader: per-rule rejection so a single typo doesn't take the whole
schema offline. Schema-level failures (no `schema_version`, malformed
YAML, wrong types) still raise and the previous in-memory map is
retained per `ZoneClassifier.reload()`'s last-known-good contract.

## API surface

### `grove.zones.classify(action) -> ZoneResult`

Unchanged. Dot-notation action identifier in; `ZoneResult` out. All
existing callers continue to work.

### `grove.zones.ZoneClassifier.classify_command_string(command, action, *, tool_id=None) -> ZoneResult`

New. Hierarchical-first classification path for command strings.

- `command` — the full shell command line.
- `action` — the dot-notation identifier (`command.execute.rm` etc.)
  used for the legacy fall-through.
- `tool_id` — which tool's hierarchical rules to consult. When `None`,
  derive from the action prefix via the v0.1 mapping
  `command.execute.* → terminal`. Future tools that wire hierarchical
  rules should pass this explicitly rather than expanding the
  derivation map — see the v0.1 caller-resolution note below.

Returns a `ZoneResult` with the same legacy fields plus:

- `reason` — the rule's human-readable explanation, when a
  hierarchical rule matched; `None` otherwise.
- `pattern_key` — the matched regex string, when a hierarchical rule
  matched; `None` otherwise. Used by future callers (notably the
  WebUI's currently-disabled Approve Always) to key allowlist entries
  on the specific pattern rather than the rule-level category.

### `grove.dispatch.classify_command(command, env_type, *, tool_id=None) -> ZoneResult`

Wraps `command_to_action` + `classify_command_string`. The `tool_id`
keyword is additive — existing callers (e.g. `tools/approval.py`)
continue to work without modification.

### `grove.zone_rules.synthesize_pattern(command) -> SynthesisResult`

Produces a conservative regex from an example command:

- **Directory-scoped** for FS-path commands. `rm /tmp/foo.txt` →
  `^rm\s+(-[Rfirv]+\s+)?/tmp/.*` (matches future `rm` of any flag
  bundle within `/tmp/`).
- **Mode-generalised** for `chmod` / `chown` / `chgrp` / `umask`
  numeric arguments. `chmod 644 /home/user/.bashrc` matches
  `chmod 755 /home/user/.bashrc`.
- **Exact** for subcommand-style verbs (`pip install`, `apt install`,
  `systemctl start`, `git push`, `docker run`, …). These take
  package / service / branch names, not file paths, so
  directory-scoping is meaningless.
- **Refused** for:
  - verb denylist (`sudo`, `su`, `doas`, `pkexec`) — privilege
    escalation always requires an interactive decision;
  - shape denylist (`rm -rf /`, `chmod 777 /`, `dd of=/dev/sda`,
    `mkfs.*`, `> /dev/sda`) — root-level catastrophes that should
    never be greenlistable;
  - sensitive system directories (`/etc`, `/bin`, `/sbin`, `/usr`,
    `/var`, `/boot`, `/sys`, `/proc`, `/dev`, `/root`, `/lib`,
    `/lib64`, `/opt`) — directory-scoped greenlisting under any of
    these is refused. Operators who want broad system-path approval
    must edit `zones.schema.yaml` by hand.
- **Validated** against `check_pattern_safety` before being returned
  — defence in depth against synthesis bugs.

Returns `SynthesisResult(ok, pattern, reason)`. Callers can surface
the refusal text directly to operators rather than fabricating their
own error messages.

### `grove.zone_rules.save_zone_rule(tool_id, pattern, zone, reason)`

Appends a rule to `~/.grove/zones.schema.yaml`:

- Uses **`ruamel.yaml`** for round-trip comment preservation — the
  schema is the operator's primary governance interface and stripping
  its 200 lines of inline operator-facing commentary would be a
  non-starter. Falls back to `pyyaml.safe_dump` with a loud log only
  if ruamel fails to load at runtime.
- **Normalises bare-string entries** to dict form in place: the
  original zone becomes `default_zone`, the new rule is the first
  entry in a freshly-created `rules` list. Existing dict entries get
  the new rule appended at the end.
- **Re-validates** the pattern against `check_pattern_safety` before
  write — refusing with `ValueError` if it fails (in addition to the
  load-time check).
- Calls `grove.zones.reload()` so the new rule takes effect for any
  caller holding the classifier singleton, no process restart needed.

## Backward compatibility

- **Every existing schema continues to work unchanged.** No
  operator-side action is required on upgrade. Bare-string
  `tool_zones` entries flow through the exact same matcher they did
  in Sprint 04.
- **Every existing caller of `classify_command` continues to work
  unchanged.** The new `tool_id` keyword is additive; the legacy
  signature `classify_command(command, env_type)` still resolves
  correctly.
- **Every existing caller reading `ZoneResult.zone` /
  `.matched_rule` / `.source`** continues to work. The new `reason`
  and `pattern_key` fields default to `None` and are only populated
  for hierarchical rule matches.

## v0.1 caller-resolution note

`_ACTION_PREFIX_TO_TOOL` in `grove/zones.py` ships with a **single
explicit mapping**: `command.execute.* → terminal`. The terminal tool
is the only one in v0.1 whose operators need argument-level rule
authoring; expanding the derivation map for additional tools is
explicitly *not* the supported extension path.

The principled way to add hierarchical rules for a second tool is to
have its dispatch code pass `tool_id` **explicitly** into the
calling chain:

```python
result = classify_command(command, env_type, tool_id="git_op")
```

The derivation map is for the v0.1 zero-touch path; explicit
`tool_id` is for everything else. Generalising
action→tool derivation (probably via a registry tools opt into) is
deferred until at least two tools actually request hierarchical
rules.

## I4 preservation

W3.0a's "zone checks unsuppressible" invariant holds through the
evolution. Specifically:

- A loader exception during schema parse triggers
  `ZoneClassifier.reload()`'s last-known-good snapshot — the in-memory
  map is **not** wiped. This is the SPEC-commanded graceful
  degradation, identical to Sprint 04 behaviour.
- A classifier exception inside `tools/approval.py:check_all_command_guards`
  (whether from the legacy `classify(action)` path or the new
  `classify_command_string` path) hits the existing fail-closed
  `except` at lines 1182-1211 and returns `approved=False` with
  `classifier_failed=True`. No silent fall-through to the legacy
  approval flow.
- A pattern that fails `check_pattern_safety` is rejected at load
  time — the rule never reaches the matcher, so it cannot cause a
  runtime classification exception in production.

Coverage: the existing W3.0a invariant tests
(`tests/test_w3_0a_governance_invariants.py`) cover I4 at the
classifier-error level (which is unchanged by S22). S22-specific
verification lives in `tests/grove/test_s22_zone_evolution.py`:
`TestI4Preserved::test_classifier_exception_inside_check_all_command_guards_returns_blocked`.

## Flags for follow-up

- **Action→tool derivation registry.** When the second tool wants
  hierarchical rules, replace `_ACTION_PREFIX_TO_TOOL` with a small
  registry tools opt into rather than expanding the v0.1 hardcoded
  map.
- **WebUI Approve Always re-enablement.** S22 unblocks W5's
  intentionally-disabled `always` choice. The WebUI surface needs:
  (1) the modal's Card 3 re-enabled with a two-step confirmation
  per the W5 spec; (2) the endpoint accepts `"always"` and calls
  `synthesize_pattern` + `save_zone_rule` to persist;
  (3) `_yellow_pending` data includes the command so the WebUI can
  pass it to `synthesize_pattern`.
- **Rule reordering UX.** `save_zone_rule` appends to the END of
  `rules`. Operators who need a new very-specific rule that should
  beat existing patterns must edit YAML by hand. A future
  governance-UX sprint could add a panel for visualising + reordering
  rules.
- **Per-rule telemetry.** When a hierarchical rule fires, the
  `routing_decision` telemetry could include the matched
  `pattern_key` and `reason`. Useful for the
  `Yellow → Green promotion` Kaizen heuristic and for operator
  audit. Not in S22 scope; flag for a telemetry-evolution sprint.
