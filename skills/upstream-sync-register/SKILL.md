---
name: upstream-sync-register
description: Editorial discipline for generating upstream-divergence.md entries
  during upstream-security-scan and upstream-release-review sprints. Enforces
  Grove no-judgment register on a public-facing architectural artifact.
created_by: grove-foundation
zone: green
tier: T0
register: standards
applies_to:
  - upstream-security-scan-vN
  - upstream-release-review-vN
  - divergence-audit-vN
---

# Upstream Sync Register — Editorial Discipline

The upstream-divergence.md ledger is a public Grove-canonical artifact. Every
entry describes a structural decision Grove made about how its architecture
relates to upstream Hermes. The discipline below applies to every entry
Claude Code generates.

## Core principle

There is no villain in plumbing. There is only design and consequence.

The ledger documents architecture, not actors. Every entry describes what
Grove preserves about its own structure. No entry describes what Hermes
got wrong.

This is the same no-judgment register Grove uses on all standards-layer
surfaces. The audience for the ledger includes Nous Research engineers,
auditors evaluating grove-autonomaton, operators considering adoption,
and future Grove maintainers. All of them benefit from precise structural
reasoning. None of them benefit from criticism of upstream choices.

## Entry types and their registers

The ledger uses four entry types, each with its own discipline.

### PORTED entries

Format: `<hash> — <subject>`

Register: factual, terse. The fact that Grove ported the change is the
information. No commentary required. If a port required adaptation (e.g.,
3-way merge against renamed file), note the adaptation in one line.

GOOD:
  abc1234 — fix: SQL injection in FTS5 query sanitizer
  Adapted: applied to grove/state/telemetry.py (renamed from hermes_state.py).

BAD (commentary, not needed):
  abc1234 — fix: SQL injection in FTS5 query sanitizer
  Important fix; nice catch by the upstream team.

### REJECTED entries

This is the highest-register category. Every rejection is a Grove
architectural commitment getting tested against an upstream alternative.
The entry exists to document what Grove preserves, not what upstream
chose differently.

Format:
  <hash> — <upstream subject>
  REJECTION REASONING: <what Grove preserves and why>

**Brevity constraint.** REJECTION REASONING is exactly three sentences:

1. What Grove preserves. Name the canonical commitment.
2. The citation. GRV-001/002/003/004 or Draft 1.x section reference.
3. The structural consequence. What the preservation enables or prevents.

Maximum length: three sentences. No bullet points, no sub-clauses
substituting for sentences, no parenthetical elaboration that turns one
sentence into two. If the reasoning cannot fit in three sentences, the
entry has either misidentified the canonical commitment or is attempting
to argue rather than document — raise Andon.

Register discipline — required:

1. Lead with what Grove preserves, not what the upstream change does.
   The rejection is in service of a Grove canonical commitment. Name
   the commitment by reference.

2. Cite the specific GRV standard or Pattern principle the rejection
   preserves. Rejections without canonical citation are not legible
   to an external reader and should be revised before commit.

3. Describe consequence, not character. "This would allow agent-authored
   skills to enter the active set without human Stage-4 approval" is
   structural. "This is a dangerous design" is character judgment.

4. Use passive voice for the upstream change; active voice for the
   Grove decision. The upstream change "would allow X"; Grove
   "preserves Y."

5. Never name the upstream author or attribute design intent.
   "Upstream change abc1234 would..." not "The Hermes team decided to..."

6. End with the structural alternative Grove offers, not a critique
   of what upstream offers. The ledger is constructive by structure.

GOOD:
  ghi9012 — feat: auto-promote skills with confidence > 0.95

  REJECTION REASONING: Grove preserves Stage-4 Approval as a human-only
  zone per GRV-001 §III Commitment 2 and the Pattern-Based Approval
  principle in Draft 1.3 Part IX. Confidence thresholds compute a
  property of the proposal and do not substitute for the operator's
  category-level grant. Grove's equivalent surface is the Andon
  quarantine plus explicit `autonomaton sovereignty promote` operator
  action.

BAD (multiple register violations):
  ghi9012 — feat: auto-promote skills with confidence > 0.95

  REJECTION REASONING: This is dangerous and contradicts everything
  the Pattern stands for. Auto-promotion based on confidence is
  exactly the failure mode we built the Sovereignty Gate to prevent.
  The Hermes team apparently doesn't understand why human approval
  matters.

Why the BAD version fails:
  - "dangerous" — character judgment, not structural reasoning
  - "everything the Pattern stands for" — vague; no GRV citation
  - "we built the Sovereignty Gate to prevent" — possessive register,
    "we" vs "they" framing
  - "The Hermes team apparently doesn't understand" — attribution of
    intent and capability to upstream actors
  - "matters" — appeal to emotion; structural reasoning describes
    consequence, not importance
  - Five sentences — exceeds three-sentence constraint by structural
    measure; entry is arguing, not documenting.

### DEFERRED entries

Format:
  <hash> — <subject>
  RATIONALE: <why not now>
  REVISIT: <date or trigger>

Register: practical, neutral. Deferrals are not rejections. The change
may land in a future Grove version. The entry exists to document the
queue, not commentary the upstream choice.

GOOD:
  mno7890 — feat: web-based skill editor UI
  RATIONALE: Interesting; not on v0.2 critical path. Web surface is
  a v0.3 candidate after CLI maturity locks.
  REVISIT: Q4 2026.

### N/A entries

Format: `<hash> — <subject>`
       `N/A: <which Grove module replaces or renames the touched file>`

Register: mechanical. These entries exist for completeness; they
document why a cherry-pick didn't apply rather than evaluating the
upstream change.

GOOD:
  pqr1234 — refactor: skills_guard.py error messages
  N/A: Grove replaced this module with tools/jidoka.py + tools/andon.py
  in jidoka-andon-implementation-v1.

## Disallowed phrasings

The following constructions never appear in ledger entries. Claude Code
must rewrite or refuse if a draft contains them.

- "wrong" / "incorrect" / "bad" / "good" applied to upstream changes
- "the Hermes team" / "Nous" / "they" / "upstream apparently"
- "obviously" / "clearly" / "of course" (rhetorical pressure)
- "we" / "our" in possessive contexts ("our architecture", "we believe")
  — use "Grove" or "the Pattern" instead
- "should have" / "would have been better to" (counterfactual judgment)
- "concerning" / "troubling" / "dangerous" / "risky" (character framing)
- Emotional appeals ("matters", "critical", "vital", "essential" used
  as intensifiers rather than as architectural descriptors)
- Sales register ("powerful", "revolutionary", "best-in-class")
- Any REJECTION REASONING longer than three sentences. The constraint
  is structural: one sentence per architectural job (preserved
  commitment, citation, consequence). Bloat is itself a register
  defect.

## Required phrasings

Every REJECTED entry must contain at least one of:

- "Grove preserves [commitment name] per [GRV citation]"
- "The Pattern's [principle name] requires [structural property]"
- "Grove's equivalent surface is [named mechanism]"

These phrasings force the entry into structural register and provide
the canonical citation that makes the rejection legible to external
readers.

## When Claude Code is uncertain

If a draft entry feels even slightly outside register, Claude Code
should:

1. Re-read this skill.

1.5. Count sentences in REJECTION REASONING. If more than three,
     identify which sentence carries no architectural job and remove
     it. If every sentence carries a job and the total still exceeds
     three, the entry is attempting multiple rejections — split into
     separate ledger entries.

2. Identify the specific phrasing that feels off.

3. Rewrite that phrasing using the templates above.

4. If the rewrite cannot be made to fit, raise Andon — surface the
   draft to Jim with a note about which guideline the entry conflicts
   with.

Andon on register is correct behavior. Sloppy register on a public
artifact is a structural defect, not a stylistic one.

## When the guidelines themselves are insufficient

This skill is a v1 register. If repeated entries surface a register
question this skill doesn't address, do not improvise. Raise Andon;
the guidelines need an update sprint, not an ad-hoc decision.

## Lineage

- The Pattern Document: Draft 1.3 Part X ("The system never makes the
  human feel bad...").
- GRV-004 §II: the protocol commodifies; the stewardship role does not.
  The ledger is a stewardship surface.
- The "no villain in plumbing" framing: Grove canon, recurring across
  standards-layer surfaces.
