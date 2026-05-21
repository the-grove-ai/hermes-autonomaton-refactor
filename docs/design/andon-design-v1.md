# Andon Design — v1

Reference contract for Sprint 06a (jidoka-andon-implementation-v1).
Canonical ref: GRV-001 §III Commitment 2, Architectural Map §A.4.

## Quarantine layout

~~~
~/.grove/skills/.andon/<skill-name>/SKILL.md
~/.grove/skills/.andon/<skill-name>/<supporting files>
~~~

Flat structure. One directory per proposed skill. Created on first
proposal if absent. Promotion moves the directory to
`~/.grove/skills/<skill-name>/`.

## CLI verbs

All under `sovereignty` subcommand (becomes `autonomaton sovereignty`
after cli-rename sprint):

| Verb | Action |
|------|--------|
| `sovereignty list` | Show pending proposals: name, scan verdict, proposed_at |
| `sovereignty diff <skill>` | Full SKILL.md + supporting files for review |
| `sovereignty promote <skill>` | Move .andon/ → active. Update frontmatter. Log event. |
| `sovereignty reject <skill> [--reason "..."]` | Log rejection. Delete from .andon/. Permanent. |
| `sovereignty revoke <skill>` | Move active → .andon/. Restore yellow zone. Log event. |

No `--all` flag. Each decision is a deliberate sovereignty act.

### Promote collision

If active skill with same name exists:
- Default: fail with message naming the collision.
- `--replace`: archive existing to `.archive/<skill>-<timestamp>/`,
  then promote. Both events logged.

## SKILL.md frontmatter — Grove extensions

Additive, agentskills.io-compatible:

~~~yaml
---
name: weekly-team-sync
description: Schedule a recurring weekly team sync.
created_by: autonomaton
proposed_at: 2026-05-18T14:23:01Z
promoted_at: 2026-05-20T09:11:44Z
zone: green
provenance:
  created_by: autonomaton
  approved_by: jim@the-grove.ai
  scan_verdict: safe
  scan_findings: []
---
~~~

**At proposal time:** name, description, created_by, proposed_at,
zone: yellow, provenance (created_by, scan_verdict, scan_findings).

**At promotion time:** promoted_at, zone: green, provenance.approved_by.

## Operator identity

`GROVE_OPERATOR_EMAIL` env var. If unset: warn, record "unknown",
do not block. Operator can choose anonymity.

## Sovereignty decision telemetry

Each promote/reject/revoke emits a structured event:

~~~json
{
  "event_type": "sovereignty_decision",
  "action": "promote | reject | revoke",
  "skill_name": "weekly-team-sync",
  "skill_hash": "<sha256 of SKILL.md at decision time>",
  "scan_verdict": "safe | warning | dangerous",
  "operator": "jim@the-grove.ai",
  "reason": "<for reject, null otherwise>",
  "timestamp": "2026-05-20T09:11:44Z",
  "source_path": "~/.grove/skills/.andon/weekly-team-sync/",
  "dest_path": "~/.grove/skills/weekly-team-sync/"
}
~~~

v0.1: structured JSON log. Future: migrates to stages table rows.

## Yellow zone UX — the conversation

**Skill proposals (asynchronous):**

1. Autonomaton writes skill to .andon/.
2. In-session message: "I've drafted a skill for [name]. It's in
   your review queue."
3. system_prompt.py lists andoned skills under "Proposed by you,
   awaiting promotion." Agent can read but cannot self-promote
   (red zone: skill.self_promote.*).
4. Operator reviews on own schedule. No blocking.

**Command-level actions (synchronous):**

5. classify() returns yellow → Jidoka triggers the existing
   four-choice prompt from tools/approval.py.
6. "Always" feeds Kaizen. After N approvals of same category,
   Kaizen proposes zone promotion.
7. No new UX invented. The four-choice prompt IS the Andon
   surface for command actions.

## Red zone UX — Andon halts, Kaizen proposes

At a red boundary, the TPS sequence is explicit:

> Jidoka (zone classifier) detects → Andon (the gate) halts execution
> → Kaizen (the sovereign prompt) proposes go-forward options.

**Andon's job is to stop the line.** The code path to execute does not
exist by design. **Kaizen's job is to step in as the butler** with three
choices for the operator:

1. **Cancel** — do nothing.
2. **Operator handles** — surface the exact command and the relevant
   resource. Render the `surface` and `register` fields from
   `zones.schema.yaml`'s red zone. Read access is always green; show
   the operator what's there. Name the file and line to edit. Name the
   reload method (restart in v0.1, SIGHUP when wired). Register:
   "That's in your direct control — here's how." Never say "access
   denied" or "forbidden."
3. **Try de-scoped alternative** — Kaizen proposes a version within the
   system's authority (e.g., the same command without sudo). If the
   operator picks this, re-classify the de-scoped command through the
   normal flow. It may land in yellow and trigger the four-choice
   prompt — that's correct. The system never silently weakens the red
   boundary; Kaizen offers a within-authority path *adjacent* to it.

In strict mode (`GROVE_ZONE_STRICT=1`) and in gateway/async sessions
where no operator is present interactively, Kaizen's three-option
prompt is skipped and the system returns option 2's surface message
directly. Hard block by default; sovereign prompt by default in CLI;
strict mode opt-in for operators who want the full architectural
guarantee.

Kaizen also watches longer-term boundary friction. If the operator
keeps asking about a red-zone resource, Kaizen proposes a green-zone
read skill that surfaces the information more fluently. Kaizen never
proposes weakening a red boundary — it proposes better information
delivery around it.

## Security scan integration

Existing scan pipeline runs at proposal time. Verdict recorded
in frontmatter. Scan does NOT gate the write — proposal always
lands in .andon/. Operator sees verdict in `sovereignty diff`.
Operator is the gate, not the scan.

Sprint 06a changes INSTALL_POLICY for agent-created skills:
`(allow, allow, ask)` → `(andon, andon, andon)`.

## What Sprint 06a implements

1. .andon/ directory creation and skill-write routing
2. sovereignty CLI verbs (list, diff, promote, reject, revoke)
3. Frontmatter writing at proposal and promotion time
4. Sovereignty decision telemetry (structured JSON log)
5. INSTALL_POLICY: agent-created → andon
6. system_prompt.py: "Proposed, awaiting promotion" section
7. Red zone surface rendering (classify → red → surface/register)
8. Wire classify() into tool dispatch pre-check
