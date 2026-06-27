---
title: GRV-001 Core Principles
source_type: reference
topics: [grv-001, architecture, principles]
key_entities: [GRV-001, Autonomaton, operator, sovereignty]
confidence: 1.0
dock_goal_refs: []
---

# GRV-001 Core Principles

GRV-001 is the protocol that governs a grove-autonomaton node. It names four
commitments. Each is structural: the architecture enforces it, and policy
describes how it is kept. Architecture is the guarantee; policy is the promise.

## Self-evolution

The Autonomaton improves its own behavior over time. It observes its work,
detects recurring patterns, and proposes new skills and configuration changes.
Improvement is a governed loop, not an unbounded rewrite — every change passes
through the same approval the operator controls. The system gets richer; the
shape of how it changes stays fixed.

## Operator sovereignty

The operator governs the node. Behavior is declared in artifacts the operator
can read and edit — markdown, YAML, and the Dock — not buried in code. A
change the operator cannot make by editing a file is an incomplete feature.
The Autonomaton acts; the operator holds the authority to define what acting is
allowed to mean.

## Zone governance

Every action carries a risk classification — Green, Yellow, or Red. Green runs
freely. Yellow and Red pass through a gate before they execute. The
classification is declared, inspectable, and consistent, so the same action is
judged the same way every time. Governance is a property of the architecture,
not a habit of the model.

## Model independence

The node is not bound to one model or one vendor. Cognition is tiered and
routed, and the routing is configuration. A model can be swapped without
rewriting the system around it. Independence keeps the operator's leverage with
the operator rather than with a supplier.

These four principles are read together. Self-evolution without sovereignty is
drift; sovereignty without zones is unenforceable; zones without model
independence concede control to whoever supplies the cognition.
