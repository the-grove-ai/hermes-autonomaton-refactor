# Editorial Register

This file holds the discipline that governs entries written into
ledgers, divergence registers, sync registers, and other
governance-tracking artifacts. These artifacts exist to be read by
the operator, by future contributors, by upstream reviewers — they
are the durable record of what was decided and why.

## Surfaces

This register applies to:

- `upstream-divergence.md` — the public ledger of how this fork
  differs from upstream.
- Sync registers from `upstream-security-scan-vN` /
  `upstream-release-review-vN` sprints.
- HANDOFF documents written into Notion sprint pages.
- Sprint catalog bullet entries on the workstream parent page.
- Any document that records governance decisions for posterity.

## Discipline

When operating in this register, you obey four rules.

**REJECTION REASONING gets a three-sentence cap.** Three sentences,
in this order: preserved commitment, canonical citation, structural
consequence. Reject longer; require shorter to expand. Example:

> The patch was rejected because it would have permitted Red-zone
> actions to be promoted by the Autonomaton itself (constitution
> Commitment 3, operator holds zone boundaries directly). The
> proposed mechanism wired skill_manage to auto-promote on three
> approvals (GRV-001 §III, sovereignty boundaries are not
> ratchetable by the system). Accepting would have moved a zone
> boundary without operator approval, exactly the failure mode
> the constitution exists to prevent.

**Cite canon.** Every rejection, every commitment, every
architectural reference names the source — constitution clause,
GRV-### standard, sprint number, design decision letter. The ledger
is the durable record; a reader two years from now needs the
citation, not the recollection.

**No drift.** Do not soften prior commitments to accommodate the
current rejection. The ledger is append-only governance. If a
commitment turned out to be wrong, the entry is a revision — its
own sprint, its own commitment statement — not a quiet rewrite of
the older entry.

**No judgment.** Same rule as Standards Register. Describe the
design, name the consequence, decline to judge the actors.

## Examples

**Right.** A divergence entry: "Fork keeps `hermes_constants.HOME`
as `~/.hermes/` for unmodified file-path tooling
(upstream-divergence §A.2). Operator-facing paths surface as
`~/.grove/` via the path-retrofit layer. Reader who needs the
mapping consults `config/zones.schema.yaml:32`."

Three claims. Two citations. No judgment.

**Wrong.** "We had to keep the old path because upstream's tooling
is really brittle and changing it would have broken a lot of
things, which is unfortunate but unavoidable given the timeline."

No citations. Editorializing ("brittle," "unfortunate"). Drift
away from the architectural commitment toward apologetics.

## Heritage

Editorial Register descends from the workstream parent's
operational doctrine — "three-sentence cap on REJECTION REASONING
in ledger entries" — and from the Standards Register's no-judgment
constraint, sharpened for the ledger surface where citation
discipline matters most.
