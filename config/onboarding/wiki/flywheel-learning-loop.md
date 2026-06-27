---
title: The Skill Flywheel
source_type: reference
topics: [flywheel, learning, kaizen]
key_entities: [Skill Flywheel, Kaizen, TierRatchet, ConsolidationRatchet]
confidence: 1.0
dock_goal_refs: []
---

# The Skill Flywheel

The Skill Flywheel is how a grove-autonomaton node turns repeated work into
durable capability. It is a six-stage loop. The stages always run in order; the
loop is what makes self-evolution a governed process rather than an open-ended
rewrite.

## The six stages

**OBSERVE.** The node records its own work — what was asked, what it did, how it
turned out.

**DETECT.** Recurring patterns surface from the record. A task done the same way
several times is a candidate for capture.

**PROPOSE.** Kaizen, the recommender, drafts a concrete change — a new skill, a
routing adjustment, a configuration edit — with the evidence that motivated it.

**APPROVE.** The proposal stops at the gate. The operator reviews the evidence
and grants or declines. Nothing is adopted without this step.

**EXECUTE.** An approved change is applied to the live configuration.

**REFINE.** The change is watched in use. What works is kept; what underperforms
returns to OBSERVE as new evidence, and the loop turns again.

## The ratchets

Two ratchets keep the flywheel from slipping backward.

**TierRatchet** governs promotion across the Cognitive Router's tiers. Work that
proves reliably solvable at a cheaper tier is promoted down toward the Pattern
Cache; the node spends less cognition on solved problems over time.

**ConsolidationRatchet** governs the skill set itself. Overlapping or superseded
skills are merged and retired so the library sharpens rather than sprawls.

## The Kaizen voice

Kaizen proposes; it never executes. Its register is evidence and recommendation,
not command — it shows the operator what the record suggests changing and why,
and waits at the gate. The flywheel improves the node continuously, but every
turn passes through the operator's authority.
