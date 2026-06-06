# The Grove Foundation Loop

A sprint methodology for governed software development. One sprint, one purpose, one set of writes. Every change goes through discovery, design lock, and execution gates before touching disk. The system halts on ambiguity — no workarounds, no guesses, no silent degradation.

This document is the contributor's guide to the grove-autonomaton development process. Follow it when submitting sprint-structured PRs, refactoring code, adding features, or fixing bugs that touch governance-critical paths.

---

## The Contract

Every sprint produces three artifacts:

**SPEC.md** — what the sprint does, what it doesn't, what triggers a halt. The SPEC is the contract. If the code doesn't match the SPEC, the code is wrong. If the SPEC doesn't match reality, the SPEC gets amended (with a patch log), not ignored.

**CC-PROMPT.md** — the self-contained instruction block for the executor (Claude Code or any AI coding agent). Contains the SPEC, the gate sequence, Andon triggers, tool restrictions, and the commit message. One block, one paste, no choreography.

**HANDOFF.md** — what shipped, what didn't, what the next sprint needs to know. Written after execution completes. Includes commit hashes, gate findings, Andon events, verification results, and any operational notes for deployment.

---

## The Gate Sequence

Every sprint passes through three gates. No gate can be skipped. The executor halts and reports at each gate — the operator (or reviewer) approves before the next gate begins.

### GATE-A: Discovery (read-only, no writes)

The executor reads the codebase, maps the relevant surfaces, and reports findings. No files are created, modified, or deleted.

**What GATE-A produces:**
- Inventory of affected files and code paths
- Current behavior documented with exact file:line references
- Dependencies and consumers identified
- Missing context or SPEC assumption violations surfaced
- Open questions that need operator ruling

**The executor halts and says:** "Here's what I found. Here's what the SPEC assumes. Here's where they diverge. Approve, amend, or redirect."

**Why this matters:** Most sprint failures happen because the executor starts writing before understanding the codebase. GATE-A forces the understanding to happen first, in the open, where the operator can catch wrong assumptions before they become wrong code.

### GATE-B: Design Lock (read-only, no writes)

The executor presents the exact implementation design: before/after for every change, file-by-file plan, new function signatures, test assertions, and any SPEC amendments surfaced by GATE-A findings.

**What GATE-B produces:**
- Before/after for every changed file
- New code designs (function signatures, data structures, prompt templates)
- Test plan (which tests change, what new tests assert)
- Any SPEC amendments required by GATE-A findings
- Explicit "what I will NOT change" boundaries

**The executor halts and says:** "Here's exactly what I'll write. Approve this design, or redline it."

**Why this matters:** The design review happens before code exists. Redlines cost nothing at GATE-B. After GATE-C, they cost a revert.

### GATE-C: Write to disk

The executor implements the approved GATE-B design. Creates files, modifies code, runs tests, commits.

**What GATE-C produces:**
- All files written per the GATE-B design
- Tests passing (targeted + regression suite)
- Single commit with the message specified in the SPEC
- The commit is NOT pushed — the operator pushes after review

**The executor reports:** "Commit [hash]. [N] files, [M] insertions. Tests: [results]. Not pushed."

---

## Andon: The Halt Discipline

An Andon trigger is a condition that halts sprint execution. The executor stops, reports the condition, and waits for operator ruling. The executor does not guess, work around, or "fix" the problem autonomously.

### When to fire Andon

- **SPEC assumption violation.** The SPEC says X, but the codebase shows Y.
- **Missing dependency.** A required module, tool, or API doesn't exist.
- **Scope expansion.** Implementing the SPEC requires changing files or systems outside the stated scope.
- **Competing instructions.** Two authoritative sources (e.g., a tool description and an identity directive) contradict each other.
- **Governance boundary.** The fix would weaken a governance guarantee (zone classification, disposition behavior, sovereignty boundary).
- **Ambiguity.** The SPEC doesn't address a situation the executor encountered.

### How to fire Andon

```
ANDON: [one-sentence description]

What I found: [exact evidence — file:line, error message, test output]
What the SPEC says: [the relevant SPEC clause]
Why they conflict: [the gap]

Options:
1. [option A — what it does, what it trades off]
2. [option B — what it does, what it trades off]

Awaiting operator ruling.
```

### What Andon is NOT

- A suggestion box. Don't fire Andon to propose improvements outside scope.
- A workaround request. Don't fire Andon and then suggest the workaround.
- An excuse to stop. Fire Andon with options, not with a blank stare.

---

## The Executor Contract

The executor (Claude Code, or any AI coding agent following this methodology) operates under strict constraints:

**You are a mechanical executor.** You write code to disk per the SPEC. You do not architect. You do not opine. You do not volunteer adjacent work.

**One commit per sprint.** Unless the SPEC explicitly authorizes multiple commits (e.g., "one commit per logical group"), everything ships in a single commit.

**Approval gates before every write.** You halt at GATE-A, GATE-B, and GATE-C. You do not write to disk until the operator approves the gate.

**No silent degradation.** You do not write fallback logic, error-swallowing try/except blocks, or silent-degradation pathways unless the SPEC explicitly commands them. Your default instinct as an AI — to be "helpful" by guessing workarounds — is the thing you suppress.

**Fail fast, fail loud.** If you encounter ambiguity, missing dependencies, or SPEC violations, you halt and raise Andon. You do not route around the problem.

**Out-of-scope is out-of-scope.** If you notice something adjacent that could be improved, note it in the HANDOFF for a future sprint. Do not act on it. Atomicity is the discipline.

---

## Vocabulary

The grove-autonomaton project uses specific vocabulary. Use these terms without glosses — the operator wrote the canon.

| Term | Not this | Definition |
|---|---|---|
| Autonomaton | agent, assistant, copilot, bot | The governed system |
| Operator | user, person | The human with sovereign authority |
| Jidoka | detector, watcher | Detects in-flight abnormality |
| Andon | gate, blocker, halt | Halts and surfaces the issue |
| Kaizen | recommender, suggester | Proposes go-forward options |
| Green zone | allowed, permitted | Full autonomy — system executes and logs |
| Yellow zone | needs approval, gated | System halts for operator consent before execution |
| Red zone | blocked, forbidden | System refuses — action stays with the operator |
| T0 Pattern Cache | cache, lookup | Deterministic — no model call |
| T1 Cheap Cognition | small model | Routine work, low cost |
| T2 Premium Cognition | medium model | Moderate complexity |
| T3 Apex Cognition | large model | High-stakes, ambiguous, creative |
| The Cognitive Router | router, classifier | Tier selection and intent classification |
| The Skill Flywheel | skill system | Observe → detect → propose → approve → execute |
| The Sovereignty Guardrails | permissions, RBAC | Zone-based governance (Green/Yellow/Red) |

---

## Sprint Structure Template

```markdown
# Sprint NN — slug-name-v1

## Purpose
One paragraph. What this sprint does and why.

## Deliverables
Numbered list of concrete outputs.

## Constraints
What the sprint does NOT do. What it does NOT change.

## Andon triggers
Conditions that halt execution.

## Out of scope
Explicit boundaries.

## Lodestar
One sentence. The design principle this sprint serves.
```

---

## CC-PROMPT Template

```markdown
# Sprint NN — slug-name-v1
# Paste this entire block into Claude Code.

## Identity
You are the mechanical executor for the grove-autonomaton Foundation Loop.
You write code to disk. You do not architect. You do not opine.
One commit. Approval gates before every write.

## SPEC
[Concise restatement of purpose and deliverables]

---

## GATE-A: Discovery (read-only, no writes)
[Specific checks to run, greps to execute, files to read]
Report all findings. HALT for GATE-A approval.

---

## GATE-B: Design lock
[What the design must address, format requirements]
HALT for GATE-B approval.

---

## GATE-C: Write to disk
[Implementation steps, commit message]
Do NOT push. Operator pushes after review.

---

## Andon triggers
[Specific halt conditions]

## Tool restrictions
[What the executor cannot do]

## Out of scope
[Explicit boundaries]
```

---

## HANDOFF Template

```markdown
# Sprint NN HANDOFF — slug-name-v1

## Commit
- Hash:
- Message:
- Files touched:

## GATE-A findings
[What discovery revealed]

## GATE-B design changes
[Any deviations from SPEC]

## Andons fired
[None, or description of each]

## Verification
[Test results, manual checks]

## Operational notes
[Deployment steps, re-seed requirements, known limitations]
```

---

## Common Failure Modes

### The Naval-Gazing Failure
Three paragraphs of context-setting before the one decision. Each turn doubles in length.

**Counter:** Status sentence: 8 words max. One question or one prompt. Not both.

### The Re-Explaining Failure
Defining Andon, Jidoka, Kaizen, Foundation Loop every time they appear.

**Counter:** Use canonical terms without glosses. If the operator doesn't know a term, they'll ask.

### The False-Choice Failure
Surfacing five options when one is obviously correct.

**Counter:** If the recommendation is clear, give it as the answer. Don't manufacture choice.

### The Helpfulness-Drift Failure
Volunteering to "also do" something adjacent.

**Counter:** Out-of-scope is out-of-scope. Note it for a future sprint, don't act.

### The Cached-Context Failure
Acting on what was true last session instead of checking current state.

**Counter:** Always re-read the relevant files before making assumptions. Trust the codebase, not memory.

### The Surrogate-Authority Failure
Confirming things on the operator's behalf or skipping gates.

**Counter:** Gates are the operator's to confirm. Even if the answer seems obvious.

---

## The Three-Agent Workflow (Optional)

The grove-autonomaton project uses three AI agents in coordination. Contributors are not required to follow this workflow, but understanding it explains the commit history and sprint structure.

**Claude Desktop (Sprint PM)** — drafts SPECs, CC-PROMPTs, and HANDOFFs. Manages Notion. Surfaces decisions one at a time. Does not write code.

**Claude Code (Executor)** — receives the CC-PROMPT, runs the gate sequence, writes code to disk. Does not make architectural decisions. Halts on ambiguity.

**Gemini (Architectural Co-Pilot)** — reviews SPECs and CC-PROMPTs before execution. Pressure-tests design assumptions, catches traps, flags scope risks. Suggestions require vocabulary and factual verification before accepting.

The workflow: PM drafts → Gemini reviews → PM incorporates → Operator approves → Executor runs → PM writes HANDOFF.

---

## Contributing a Sprint

To submit a sprint-structured contribution:

1. **Open an issue** describing the change and its purpose.
2. **Draft a SPEC** following the template above. Include purpose, deliverables, constraints, Andon triggers, and out-of-scope.
3. **Run GATE-A** against the codebase. Report your findings in the PR description.
4. **Present GATE-B** with your design. Before/after for every file.
5. **Implement at GATE-C.** One commit with a descriptive message.
6. **Write the HANDOFF** in your PR description. What shipped, what didn't, what tests verify it.

PRs that skip gates, weaken governance, or modify zone/disposition behavior without explicit SPEC authorization will be rejected.

---

*Architecture is the guarantee; policy is the promise. Model independence is not theater.*
