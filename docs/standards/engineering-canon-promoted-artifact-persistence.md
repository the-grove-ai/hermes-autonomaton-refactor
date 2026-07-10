# Engineering Canon — promoted-artifact-persistence-v1

Operational guidance earned live across the arc's five phases
(P1 custody, P2 retrievability, P3 acceptance events, P4
canonical-only corpus, P5 lifecycle/purge). Mirrored from the
operator's local CLAUDE.md engineering-canon section (which is
deliberately untracked — .git/info/exclude: not for upstream).


### Governed-verb wiring (five points, or it fails silently)

A new governed tool clears FIVE wiring points, or it fails SILENTLY at
runtime while every unit test passes:

1. Tool registry (`register(reg)` in tools/ — auto-discovered).
2. `config/zones.schema.yaml` tool_zones entry (the classification guard).
3. A capability record binding the tool — absent record ⇒
   `get_admitted_tools()` filters it (admission-dead). PINNED for the
   fleet_lifecycle toolset; MANUAL for new toolsets.
4. `Dispatcher._NATIVE_GOVERNANCE_TOOLS` + the grant-recognition maps —
   absent ⇒ ceremony-deaf (implicit/standing grants ignored; every halt
   store-pends). PINNED: coverage-map ⊆ dispatcher set. NOT PINNED: the
   mint-side map in `_add_standing_grant_from_halt` (a fourth copy).
5. Verify each layer ON THE LIVE SURFACE (prod venv + live config), not
   import-level — in-process-passes/live-fails is the standing trap.

### Lifecycle storage

- `storage_transfer(files, dest_dir)` (fs_utils) is THE destination
  chokepoint: destinations are parameters, the contract is
  atomic-or-loud, POSIX rename is today's implementation only. New
  lifecycle ops route through it, period. (`canonicalize_files` is a
  true alias; the whole-dir archive rename is the one documented
  exception.)
- Orchestrator-only cores (`promote_artifact`, `purge_artifacts`) are
  the single doors for custody transitions — never model-reachable;
  approval surfaces call them, they never self-serve.
- Moves-then-manifest; a re-tap completes remaining steps idempotently;
  report-not-hide on partial state.

### Declarations

- Capability records are sole authority; declarations are additive +
  optional and thread verbatim through `from_dict`/`to_dict` (only the
  presentation subtree is strict-validated). Precedents:
  `write_zone.ingest`, `write_zone.retention`.
- `pending_review` means "awaiting operator approval, never ambient" —
  everywhere, no exceptions (browser extractions moved to
  `research/extractions/` for exactly this).
- Four-state is derived-on-read; absence is not a state (purge
  suppression is uid-set subtraction, never a fifth state).
- Extending the memory-event taxonomy: `_EVENT_TYPES` is a closed
  fail-loud set — a new event kind needs the frozen dataclass + the
  registry entry + an explicit no-op fold branch (observational
  precedent: `MemoryAccessed`); old readers skip unknown types at
  warning, so the extension is forward-safe.

### Test discipline (learned live)

- Producer-blind generality pins via `inspect.getsource` — they catch
  docstrings and import paths too; reword the code, never the pin.
- ANY path-matching logic gets a SYMLINK fixture: VM `~/.grove` is a
  symlink into `/mnt/grove-data`; tmp-dir fixtures lie about realpath.
- Set-diff verdicts for behavior-preservation; the full suite is
  xdist-order-flaky (~150±30 baseline failures) — regression proof is
  stash-differential per candidate, never raw counts.
- Never gate a commit on `pytest | tail` — the pipe masks the exit code.
- Deploy health: `is-active` ≠ listening while MCP warm-up retries a
  dead peer; poll the API until 200 before declaring the gateway up.
