# Hermes Autonomaton — Goal Context

## What Hermes Is

Hermes is the canonical open-source reference implementation of the
Autonomaton Pattern (GRV-001), built as a fork of NousResearch/hermes-agent
(MIT license, 156k stars). It demonstrates the five-stage invariant
pipeline in running code, applied to a popular agent framework.

The system name is "Mylo." The codebase is "grove-autonomaton."
Home directory: `~/.grove/` with `GROVE_HOME` env var.
CLI command: `autonomaton`

## The Architecture

### Five-Stage Pipeline (Invariant)
Telemetry → Recognition → Compilation → Approval → Execution

The dispatcher owns four stages. The LLM is consulted at exactly one
stage: Recognition. It returns a structured intent. That intent is data
the dispatcher validates, not a command it obeys.

### Zone Model (Sovereignty Gradient)
- Green: Autonomous routine. Execute confirmed skills, write telemetry.
- Yellow: Supervised proposals. Propose new skills, propose rule changes.
- Red: Human-only. Surface information, never act.

Zones are a sovereignty gradient, not categories. Operators author zone
boundaries. The gradient is canonical; additive zones compose on top.

### Sovereignty Gate
Agent-authored skills are quarantined in `~/.grove/skills/.andon/`.
Promotion requires explicit operator approval via
`autonomaton sovereignty promote`. Nothing self-authored runs without
the operator's hand on the approval.

### Three TPS Mechanisms
- Jidoka (watcher): Detects abnormalities, stops the line
- Andon (gate): Human approval required before writes
- Kaizen (recommender): Proposes improvements through the pipeline

### Cognitive Router
Four tiers bound to model endpoints:
- Tier 0: Pattern cache (deterministic, free)
- Tier 1: Local/small model (cheap, fast)
- Tier 2: Mid-tier API model (balanced)
- Tier 3: Frontier API model (expensive, novel/high-stakes)

Mature cognition migrates down tiers. Cost falls as the system learns.

### Skill Ratchet
New actions enter as Yellow (supervised). Repeated successful approvals
build evidence. Once threshold is met, the dispatcher promotes the
pattern to Green (autonomous). Supervision is front-loaded and decays.

### The Dock
This file's parent system. Holds strategic goals so each turn can
identify the relevant vector, load context, and unlock skills.

## Current State

- Sovereignty Gate: operational
- Cognitive Router: four-tier dispatch working
- Dock: being integrated (this package)
- Foundation Loop sprint methodology: being ported
- Atlas feature parity: in progress
- Notion MCP integration: in progress
- Public repo readiness: pending cleanup

## What Atlas Had (Parity Target)

Atlas was the previous implementation with:
- Identity Composition layer (5 Notion entries)
- Operational Doctrine layer (5 Notion entries)
- PromptCache
- Digital Jidoka error escalation (8 subsystems)
- RatchetInterpreter
- 43 tests passing
- ~700 lines of hardcoded TypeScript remaining

Atlas is deprecated. Hermes replaces it. The Hermes version should
demonstrate the pattern more cleanly because it's built on a popular
framework rather than custom infrastructure.

## Development Workflow

### Foundation Loop Sprint Discipline
SPEC.md → CC-PROMPT.md → HANDOFF.md

CC states plan, lists files, waits for explicit greenlight before
executing (Stage 4). Andon before any writes. One fix per sprint
where possible.

### Three-Node Workflow
- Claude Desktop: PM/strategy/writing (never modifies code directly)
- Claude Code: Execution (receives declarative files and prompts)
- Jim: Operator/approver (greenlight gate)

## Key Files
- `~/GitHub/grove-autonomaton/` — repo root
- `~/.grove/dock/dock.yaml` — this Dock
- `~/.grove/skills/` — skill directory (with `.andon/` quarantine)

## Why This Matters for HumanityAI
Hermes IS the proof point. A working Autonomaton in daily use
demonstrates that the pattern is real, not theoretical. The fact that
it's built on a 156k-star open-source project shows the pattern can
be applied to existing codebases, not just greenfield.
