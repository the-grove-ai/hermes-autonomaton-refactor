# Engineering Canon — kaizen-synthesizer window

Operational guidance earned live across the kaizen-synthesizer-provider-
agnostic-v1 and kaizen-review-cap-guard-v1 sprints. Mirrored from the
operator's local CLAUDE.md engineering-canon section (which is
deliberately untracked — .git/info/exclude: not for upstream).


### journald is VM-local time

- hermes-gateway journald runs on VM-local time (EDT). `journalctl
  --since` windows MUST use VM-local time or an explicit timezone —
  a UTC timestamp can sit in the VM's future and return nothing.
- A vacuously-empty window is a FALSE-CLEAN, not a clean. Before
  claiming zero findings, verify the window is non-empty (or anchor it
  on the true restart timestamp: `systemctl show <unit>
  -p ActiveEnterTimestamp`). Live instance: two "clean" V4 sweeps were
  empty by construction until re-anchored (kaizen-review-cap-guard-v1).

### Stash discipline

- The git stash stack is operator property. Claude Code revert and
  negative controls use patch files (`git diff > x.patch` /
  `git restore` / `git apply x.patch`) — never `stash push`/`pop`.
- Live instance: a malformed `stash push -- <path> -m` (the `-m` must
  precede the pathspec) no-opped, and the reflexive `pop` grabbed an
  operator WIP stash and left UU conflicts
  (kaizen-synthesizer-provider-agnostic-v1; recovered, stack intact).

### Probe positive-control

- A verification probe claiming zero WARNINGs must first prove its own
  log capture works: fire one known WARNING through the same capture
  path before trusting silence. A probe that swallows its own signal
  produces false-cleans.
- Live instance: `logging.basicConfig(level=ERROR)` left the module
  logger's effective level at ERROR, so WARNING records were never
  created and the capture handler read empty — the ladder had fired
  correctly all along (kaizen-review-cap-guard-v1 V2 probe).
