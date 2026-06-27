---
title: The Cognitive Router
source_type: reference
topics: [cognitive-router, routing, tiers]
key_entities: [Cognitive Router, Tier 0 Pattern Cache, Tier 1 Cheap Cognition, Tier 2 Premium Cognition, Tier 3 Apex Cognition]
confidence: 1.0
dock_goal_refs: []
---

# The Cognitive Router

The Cognitive Router decides how much cognition each request receives. It sorts
work across four tiers so that effort matches difficulty. Routing is
configuration: the operator can change the rules by editing declarative
artifacts, without touching code.

## The four tiers

**Tier 0 — Pattern Cache.** A response retrieved from a stored pattern. No model
call. The fastest and cheapest path, reserved for work the node has already
solved and can replay safely.

**Tier 1 — Cheap Cognition.** A small, fast model for routine, low-stakes work —
classification, short replies, mechanical transforms where a light model is
sufficient.

**Tier 2 — Premium Cognition.** A capable general model for most substantive
reasoning. This is the default working tier for open-ended tasks.

**Tier 3 — Apex Cognition.** The most capable model, reserved for the hardest
reasoning. It is the most expensive path and is used deliberately, not by
habit.

## Routing rules

The router reads the request, any matching pattern, and the declared rules, then
selects a tier. When the right tier is uncertain, the rule is to favor Tier 2:
under-powering a task produces a wrong answer that costs more to repair than the
premium call would have cost to avoid. The Pattern Cache is consulted first so
that solved work never pays for cognition twice.

## Why tiers

Tiering separates the question "what should we do" from "which model should do
it." That separation is what makes model independence practical — a tier is a
role, and the model filling that role can be swapped without redrawing the
system. The router's shape stays fixed while the models behind it change.
