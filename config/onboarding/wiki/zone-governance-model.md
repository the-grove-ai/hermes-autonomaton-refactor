---
title: The Zone Governance Model
source_type: reference
topics: [zones, governance, sovereignty]
key_entities: [Green, Yellow, Red, Andon, Jidoka, grant token]
confidence: 1.0
dock_goal_refs: []
---

# The Zone Governance Model

Every action a grove-autonomaton node can take is classified into one of three
zones. The zone determines whether the action runs on its own authority or must
stop at a gate first. The classification is declared and inspectable, so an
action is judged the same way each time it appears.

## The three zones

**Green.** Read-only or reversible work with no meaningful blast radius. Green
actions run freely. Most of what the node does day to day is Green.

**Yellow.** Actions that change state in bounded, recoverable ways. Yellow stops
at the gate. The operator approves, and the approval can be remembered so the
same bounded action need not be re-approved every time.

**Red.** Actions that are destructive, outward-facing, or hard to reverse. Red
always stops at the gate and is approved deliberately, case by case.

## The gate and the watcher

Andon is the gate — the halt where a Yellow or Red action waits for a decision.
Jidoka is the watcher that classifies the action and raises the halt. Together
they make the rule mechanical: the system cannot quietly downgrade a Red action
into a Green one, because the classification is not the model's to negotiate.

## Grant tokens and the sovereignty prompt

When the operator approves a gated action, the decision is recorded as a grant
token — a durable record of what was permitted and under what scope. The
sovereignty prompt is the moment of approval itself: the operator is shown what
will happen and grants or withholds authority. The operator's tap is the
approval; nothing else stands in for it.

A gate exists to fire. A node that never halts is not safe — it is unwatched.
There is no villain in plumbing, only design and consequence; the zones exist so
the consequence is always the operator's to choose.
