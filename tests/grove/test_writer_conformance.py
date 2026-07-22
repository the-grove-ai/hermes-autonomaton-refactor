"""writer-conformance-guard-v1 — sanctioned-writer uniqueness per surface.

Two walls, one guard. This test governs write-path DEFINITIONS at CI time:
for each governed surface, the only in-repo mutation path is its sanctioned
writer, and the pin tables below are the approval record. The runtime walls
(``is_scope_defining`` + the governance doors in dispatcher/file_tools, and
eventually privilege-broker-v1's out-of-process wall) govern EXECUTIONS.
One writer per surface = one provenance root per substrate region.

Honest calibration — incidents this guard would NOT have caught:

* the forge-arming misfire (runtime agent behavior through the generic
  ``patch`` tool; no in-repo Python source wrote the surface — the runtime
  scope wall's job, fixed f09e383ef);
* grant-mint's five stray copies (duplicated write-class DATA MAPS, not
  duplicated write paths — unified 5a015491b);
* the three stale governance doors (a rotted predicate conjunct inside
  gate conditions, not an unsanctioned writer — fixed f09e383ef).

The fence here is the fourth class: multiple in-repo Python writers of one
surface — the pre-``RoutingConfigWriter`` precedent, when
``_approve_consolidation`` also wrote ``routing.config.yaml``
(grove/config/routing_writer.py:117 remembers), and the state-overlay era
before ``set_publication_state`` (21bd59189).

Blind spots (accepted): paths passed in as arguments and written blind,
``shutil.copy``/``os.rename`` from elsewhere-staged files, shell writes
(``sed -i`` — the shell classifier's domain), and anything out-of-process.

Amendment protocol: a new sanctioned writer (or a new legitimate reader of
a pinned basename) joins by amending the pin table in a reviewed diff — the
diff IS the approval artifact. There is no runtime escape hatch and no
exemption list; if this test fails, either the new code is an unsanctioned
writer (fix the code) or the doctrine grew (amend the pin, in review).

Template: tests/grove/test_ledger_eventtype_conformance.py (same scan dirs,
same "test"-in-name exclusion, same SyntaxError skip; one shared AST walk).
"""
from __future__ import annotations

import ast
from pathlib import Path
import pytest

# guard-set-self-declaring: this whole module is a defect-class guard suite.
pytestmark = pytest.mark.guard

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCAN_DIRS = ("grove", "tools")

# ── Pin tables (derived from live source at b0a850fdb; citations inline) ──

# A. Primitive-caller pins, keyed (defining module, primitive name) — bare
# names are never conflated: two distinct ``_atomic_write_yaml`` and two
# distinct ``_rewrite`` definitions exist, each with its own key.
# Values: the complete sanctioned set of (caller file, enclosing function).
_PRIMITIVE_CALLER_PINS: dict[tuple[str, str], frozenset[tuple[str, str]]] = {
    # def at grove/capability_registry.py:1343; callers :501/:612/:710 (the
    # three state-overlay writers' shared ``_apply`` closures), :853
    # (_write_state_snapshot for set_model_binding), :2129 (_mint_skill_record).
    # capability-mutation-surface-v1 M2 (reviewed amendment, ruling A-3) —
    # write_admission_state: the sole sanctioned canonical admission-field
    # writer (uniqueness additionally pinned by the admission-writer
    # signature tests below).
    ("grove/capability_registry.py", "_atomic_write_yaml"): frozenset({
        ("grove/capability_registry.py", "_apply"),
        ("grove/capability_registry.py", "_write_state_snapshot"),
        ("grove/capability_registry.py", "_mint_skill_record"),
        ("grove/capability_registry.py", "write_admission_state"),
    }),
    # def at grove/eval/producer_pauses.py:57; sole caller :142
    # (set_producer_pause's _apply)
    ("grove/eval/producer_pauses.py", "_atomic_write_yaml"): frozenset({
        ("grove/eval/producer_pauses.py", "_apply"),
    }),
    # def at grove/eval/proposal_queue.py:796; callers :777/:881/:900/:928/:959
    ("grove/eval/proposal_queue.py", "_write_records"): frozenset({
        ("grove/eval/proposal_queue.py", "remove"),
        ("grove/eval/proposal_queue.py", "set_lease"),
        ("grove/eval/proposal_queue.py", "clear_lease"),
        ("grove/eval/proposal_queue.py", "finalize_proposal_state"),
        ("grove/eval/proposal_queue.py", "sweep_stuck_leases"),
    }),
    # GrantStore._rewrite, def at grove/grants.py:160; callers :131/:157
    ("grove/grants.py", "_rewrite"): frozenset({
        ("grove/grants.py", "add_standing_grant"),
        ("grove/grants.py", "revoke_grant"),
    }),
    # def at grove/memory/digest.py:265; caller :365 (run_digest) plus the
    # portal memory action surface's deliberate module-qualified reuse
    # (grove/api/actions.py:438/:446/:457, ``digest._rewrite`` — the P4
    # "portal memory reuses digest" precedent).
    ("grove/memory/digest.py", "_rewrite"): frozenset({
        ("grove/memory/digest.py", "run_digest"),
        ("grove/api/actions.py", "_apply_memory"),
    }),
    # def at grove/fleet/staging.py:28; in-module callers :54/:102/:113 plus
    # the pinned cross-module direct importers (grove/forge/feedback_store.py:41,
    # grove/fleet/reap.py:25, grove/fleet/runner.py:30 — the last mints the C1
    # genesis dispatch record atomically).
    ("grove/fleet/staging.py", "_atomic_write_bytes"): frozenset({
        ("grove/fleet/staging.py", "stage_draft"),
        ("grove/fleet/staging.py", "stage_package"),
        ("grove/fleet/staging.py", "write_terminal_event"),
        ("grove/forge/feedback_store.py", "write"),
        ("grove/forge/feedback_store.py", "set_terminal_skip"),
        ("grove/fleet/reap.py", "write_pidfile"),
        ("grove/fleet/runner.py", "write_dispatch_record"),
    }),
    # MemoryStore.append_event, def at grove/memory/store.py:111. The
    # event-sourced store is multi-emitter BY DESIGN (like KaizenLedger);
    # the pin freezes the emitter set, it does not claim a single caller.
    ("grove/memory/store.py", "append_event"): frozenset({
        ("grove/memory/store.py", "record_access"),
        ("grove/memory/store.py", "flush_access_events"),
        ("grove/memory/digest.py", "apply"),
        ("grove/api/actions.py", "_emit_promote_accepted"),
        ("grove/fleet/manager.py", "_publish_unattended"),
    }),
}

# Names with exactly one defining module attribute every call site to that
# key; names defined in two modules need per-site attribution (own-file
# definer, or a ``<alias>.<name>(...)`` call whose alias imports a definer).
_MULTI_DEF_NAMES = {"_atomic_write_yaml", "_rewrite"}

# B. Basename-literal toucher pins: every non-test module whose AST string
# constants mention the surface's basename (writers, readers, walls,
# renderers alike — the pin freezes the toucher population). SPEC amendment:
# ``catalog.sovereign.yaml`` (discovery artifact) does not exist in source;
# the sovereign catalog basename is ``model-catalog.yaml``
# (grove/config/model_catalog.py:98).
_BASENAME_TOUCHER_PINS: dict[str, frozenset[str]] = {
    # writer: GrantStore._rewrite (grove/grants.py:182).
    # capability-mutation-surface-v1 M1 (reviewed amendment): fs_utils no
    # longer carries this basename — scope-defining membership literals moved
    # to config/scope_surfaces.yaml (declarative membership).
    "grants.yaml": frozenset({
        "grove/grants.py",
        "grove/fleet/paths.py",
    }),
    # writer: proposal_queue append/_write_records (grove/eval/proposal_queue.py)
    "proposals.jsonl": frozenset({
        "grove/eval/proposal_queue.py",
        "grove/api/dashboard_fragments.py",
        "grove/api/fragments.py",
        "grove/api/portal.py",
        "grove/api/telemetry_readers.py",
        "grove/eval/consolidation_ratchet.py",
        "grove/eval/exploration_scan.py",
        "grove/flywheel_cli.py",
        "grove/kaizen/renderable.py",
        "grove/kaizen/synthesizer.py",
        "grove/kaizen_promotion.py",
        "grove/memory/cli.py",
        "grove/memory/detector.py",
        "grove/memory/digest.py",
        "grove/memory/freshness.py",
        "grove/memory/graduation.py",
        "tools/flywheel_review_tool.py",
    }),
    # writer: RoutingConfigWriter (grove/config/routing_writer.py:1 —
    # self-declared sole sanctioned writer)
    "routing.config.yaml": frozenset({
        "grove/config/routing_writer.py",
        "grove/affordances.py",
        "grove/api/fragments.py",
        "grove/cellar.py",
        "grove/classify.py",
        "grove/config/model_catalog.py",
        "grove/dispatcher.py",
        "grove/dock/attachment.py",
        "grove/errors.py",
        "grove/escalation_policy.py",
        "grove/eval/consolidation_ratchet.py",
        "grove/eval/hero_runner.py",
        "grove/eval/pattern_compiler.py",
        "grove/fleet/worker_entry.py",
        "grove/flywheel_cli.py",
        "grove/kaizen/rendering.py",
        "grove/kaizen/synthesizer.py",
        "grove/memory/detector.py",
        "grove/pattern_cache.py",
        "grove/providers.py",
        "grove/router.py",
        "grove/router_merge.py",
        "grove/skill_binding.py",
        "grove/sovereignty.py",
        "grove/t1_call.py",
        "grove/tier_budget.py",
        # capability-mutation-surface-v1 M1 (reviewed amendment): fs_utils
        # literal moved to config/scope_surfaces.yaml.
        # P5 (reviewed amendment): governance_tool dropped OUT (Pipeline-A's
        # allowlist + raw write retired); red_pending_store carries the
        # basename now (the writer-registry resolution + the
        # routing_config_replace adapter over RoutingConfigWriter).
        "grove/red_pending_store.py",
    }),
    # writer: append_machine_goal (grove/dock/__init__.py:380-384)
    "dock.autonomaton.yaml": frozenset({
        "grove/dock/__init__.py",
        "grove/dock/detector.py",
        "grove/flywheel_cli.py",
        "grove/kaizen/rendering.py",
    }),
    # writer: update_dock_goal_status (grove/dock/writer.py:91-94).
    # capability-mutation-surface-v1 P5 (reviewed amendment): governance_tool
    # dropped OUT (dock-door allowlist retired); red_pending_store carries the
    # basename now (dock_goal_status resolution + adapter over the sole
    # sanctioned dock writer).
    "dock.yaml": frozenset({
        "grove/dock/writer.py",
        "grove/api/actions.py",
        "grove/api/fragments.py",
        "grove/classify.py",
        "grove/dock/__init__.py",
        "grove/dock/attachment.py",
        "grove/dock/attachment_store.py",
        "grove/dock/detector.py",
        "grove/identity.py",
        "grove/utils/fs_utils.py",
        "grove/wiki/pipeline.py",
        "grove/wiki/watcher.py",
        "grove/red_pending_store.py",
    }),
    # writer: save_zone_rule (grove/zone_rules.py:410 — writes the operator
    # overlay; docstrings there still say "zones.schema.yaml", naming rot).
    # capability-mutation-surface-v1 M1 (reviewed amendment): fs_utils
    # literal moved to config/scope_surfaces.yaml.
    "zones.autonomaton.yaml": frozenset({
        "grove/zone_rules.py",
        "grove/red_policy.py",
        "grove/zones.py",
    }),
    # writer: write_sovereign_catalog (grove/config/model_catalog.py:628)
    "model-catalog.yaml": frozenset({
        "grove/config/model_catalog.py",
        "tools/catalog_tool.py",
        "tools/file_tools.py",
    }),
    # writer: set_producer_pause (grove/eval/producer_pauses.py:76)
    "producer_pauses.yaml": frozenset({
        "grove/eval/producer_pauses.py",
        "grove/dispatcher.py",
        "grove/flywheel_cli.py",
        "grove/kaizen/rendering.py",
    }),
    # capability-mutation-surface-v1 P4 item 4 — ZERO-writer pin for the
    # declarative scope membership: NO code writes it (git+deploy only). The
    # sole sanctioned toucher is its loader (fs_utils reads it; the filename
    # constant + docstrings live there). Any second module mentioning the
    # basename is drift.
    "scope_surfaces.yaml": frozenset({
        "grove/utils/fs_utils.py",
    }),
}

# C. Ledger-dirname special case. SPEC amendment: "only grove/kaizen_ledger.py"
# was false on clean base — the retention reaper and two readers legitimately
# carry the dirname. The pin freezes that exact population; the WRITER among
# them is kaizen_ledger.py alone (open-for-append at :388).
_KAIZEN_LEDGER_DIRNAME = ".kaizen_ledger"
_KAIZEN_LEDGER_TOUCHER_PIN = frozenset({
    "grove/kaizen_ledger.py",          # sole writer (KaizenLedger.record)
    "grove/ledger_retention.py",       # sanctioned retention reaper
    "grove/api/telemetry_readers.py",  # reader
    "grove/flywheel_cli.py",           # reader
})

# D. Attachment event types: the attachment store owns no file — attachments
# are event-sourced into the Kaizen ledger. Its uniqueness is event-type
# level: only grove/dock/attachment_store.py may .record() these types
# (grove/dock/attachment_store.py:197/:243/:276).
_ATTACHMENT_EVENT_TYPES = frozenset({
    "artifact_goal_attached",
    "artifact_goal_detached",
    "artifact_goal_suppressed",
})
_ATTACHMENT_EMITTER_PIN = frozenset({"grove/dock/attachment_store.py"})


# ── Shared single-pass scan ──────────────────────────────────────────────

_TRACKED_PRIMITIVES = {name for (_, name) in _PRIMITIVE_CALLER_PINS}
_TRACKED_BASENAMES = set(_BASENAME_TOUCHER_PINS) | {_KAIZEN_LEDGER_DIRNAME}


def _scan():
    """One AST walk over grove/ + tools/ feeding every assertion.

    Returns (primitive_sites, basename_touchers, record_sites) where
    primitive_sites maps a (module, name) pin key -> list of
    (file, enclosing_function, lineno); unattributable call sites of a
    multi-definition name land under the key ("<unattributed>", name).
    """
    primitive_sites: dict[tuple[str, str], list] = {}
    basename_touchers: dict[str, set[str]] = {b: set() for b in _TRACKED_BASENAMES}
    record_sites: list[tuple[str, str, int]] = []  # (event_type, file, lineno)

    defining_modules: dict[str, set[str]] = {}
    for (mod, name) in _PRIMITIVE_CALLER_PINS:
        defining_modules.setdefault(name, set()).add(mod)

    for d in _SCAN_DIRS:
        for py in sorted((_REPO_ROOT / d).rglob("*.py")):
            if "test" in py.name:
                continue
            try:
                tree = ast.parse(py.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            rel = str(py.relative_to(_REPO_ROOT))

            # parent links for enclosing-function resolution
            parents: dict[ast.AST, ast.AST] = {}
            for node in ast.walk(tree):
                for child in ast.iter_child_nodes(node):
                    parents[child] = node

            def _enclosing(node) -> str:
                cur = parents.get(node)
                while cur is not None:
                    if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        return cur.name
                    cur = parents.get(cur)
                return "<module>"

            # import alias map: local alias -> module relpath (definers only)
            alias_to_module: dict[str, str] = {}
            local_defs: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    local_defs.add(node.name)
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    for a in node.names:
                        dotted = (
                            f"{node.module}.{a.name}"
                            if isinstance(node, ast.ImportFrom) and node.module
                            else a.name
                        )
                        alias_to_module[a.asname or a.name.split(".")[0]] = (
                            dotted.replace(".", "/") + ".py"
                        )

            for node in ast.walk(tree):
                # basename literals (docstrings included — they are Constants)
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    for b in _TRACKED_BASENAMES:
                        if b in node.value:
                            basename_touchers[b].add(rel)
                    continue
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = (
                    func.id if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute)
                    else None
                )
                # eventtype-extractor shape for assertion D
                if (
                    name == "record"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    record_sites.append((node.args[0].value, rel, node.lineno))
                if name not in _TRACKED_PRIMITIVES:
                    continue
                # attribute the call site to a pin key
                definers = defining_modules[name]
                if len(definers) == 1:
                    key = (next(iter(definers)), name)
                elif rel in definers and name in local_defs:
                    key = (rel, name)  # in-module call in a defining module
                elif (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and alias_to_module.get(func.value.id) in definers
                ):
                    key = (alias_to_module[func.value.id], name)
                else:
                    key = ("<unattributed>", name)
                primitive_sites.setdefault(key, []).append(
                    (rel, _enclosing(node), node.lineno)
                )

    return primitive_sites, basename_touchers, record_sites


_SCAN_RESULT = _scan()


# ── Assertions ───────────────────────────────────────────────────────────


def test_primitive_caller_sets_match_pins_exactly():
    primitive_sites, _, _ = _SCAN_RESULT
    assert primitive_sites, "scan found no primitive call sites — surface broke"

    problems = []
    for key, pinned in _PRIMITIVE_CALLER_PINS.items():
        found = primitive_sites.get(key, [])
        found_set = {(f, fn) for (f, fn, _) in found}
        for f, fn, ln in found:
            if (f, fn) not in pinned:
                problems.append(
                    f"  UNSANCTIONED caller of {key[1]} ({key[0]}): "
                    f"{f}:{ln} in {fn}()"
                )
        for f, fn in sorted(pinned - found_set):
            problems.append(
                f"  pinned caller of {key[1]} ({key[0]}) VANISHED: {f} {fn}() "
                f"— the guard lost sight; re-derive the pin"
            )
    for key, found in primitive_sites.items():
        if key[0] == "<unattributed>":
            for f, fn, ln in found:
                problems.append(
                    f"  UNATTRIBUTABLE call of multi-definition primitive "
                    f"{key[1]}: {f}:{ln} in {fn}() — cannot bind to a "
                    f"defining module; make the call module-qualified"
                )
    assert not problems, (
        "writer-conformance violations (amend _PRIMITIVE_CALLER_PINS in a "
        "reviewed diff if the doctrine grew):\n" + "\n".join(problems)
    )


def test_basename_toucher_sets_match_pins_exactly():
    _, basename_touchers, _ = _SCAN_RESULT
    problems = []
    for b, pinned in _BASENAME_TOUCHER_PINS.items():
        found = basename_touchers[b]
        for f in sorted(found - pinned):
            problems.append(f"  NEW toucher of {b!r}: {f}")
        for f in sorted(pinned - found):
            problems.append(
                f"  pinned toucher of {b!r} VANISHED: {f} — re-derive the pin"
            )
    assert not problems, (
        "surface-basename population drift (amend _BASENAME_TOUCHER_PINS in "
        "a reviewed diff):\n" + "\n".join(problems)
    )


def test_kaizen_ledger_dirname_population_is_pinned():
    _, basename_touchers, _ = _SCAN_RESULT
    found = basename_touchers[_KAIZEN_LEDGER_DIRNAME]
    assert found == _KAIZEN_LEDGER_TOUCHER_PIN, (
        f"{_KAIZEN_LEDGER_DIRNAME!r} dirname population drifted "
        f"(sole writer is grove/kaizen_ledger.py; reaper + readers pinned):\n"
        f"  new: {sorted(found - _KAIZEN_LEDGER_TOUCHER_PIN)}\n"
        f"  vanished: {sorted(_KAIZEN_LEDGER_TOUCHER_PIN - found)}"
    )
    assert "grove/kaizen_ledger.py" in found, (
        "scan lost sight of the ledger writer itself — extractor regression"
    )


def test_attachment_event_types_recorded_only_by_attachment_store():
    _, _, record_sites = _SCAN_RESULT
    assert record_sites, "scan found no .record() sites — surface broke"
    offenders = [
        (et, f, ln)
        for (et, f, ln) in record_sites
        if et in _ATTACHMENT_EVENT_TYPES and f not in _ATTACHMENT_EMITTER_PIN
    ]
    assert not offenders, (
        "attachment event types recorded outside the attachment store:\n"
        + "\n".join(f"  {et!r} at {f}:{ln}" for et, f, ln in offenders)
    )
    seen = {et for (et, f, _) in record_sites if f in _ATTACHMENT_EMITTER_PIN}
    assert _ATTACHMENT_EVENT_TYPES <= seen, (
        f"scan lost sight of attachment emitters: "
        f"{sorted(_ATTACHMENT_EVENT_TYPES - seen)}"
    )


# ── capability-mutation-surface-v1 T1 — admission-field writer uniqueness ──
#
# CONTRACT (banked failing; Gate P0 rulings A-3/A-1): capability state overlay
# ADMISSION-FIELD writes (canonical keys ``intents`` / ``tiers``, always
# provenance-stamped) have exactly ONE sanctioned writer:
#
#     grove/capability_registry.py :: write_admission_state
#
# The detector below keys on the writer's structural signature — a function
# that BOTH calls the ``_atomic_write_yaml`` primitive AND carries the
# ``"provenance"`` string constant (the stamp key no other state writer emits;
# ruling A-3 makes the stamp mandatory on admission-field writes, so the
# signature is exact, not heuristic). A second function matching the signature
# is an unsanctioned admission-field writer and must fail the pin.

_ADMISSION_WRITER_MODULE = "grove/capability_registry.py"
_ADMISSION_WRITER_PIN = frozenset({"write_admission_state"})


def _admission_field_writer_functions(source: str) -> set[str]:
    """Names of functions that call ``_atomic_write_yaml`` and contain the
    ``"provenance"`` constant — the admission-field-writer structural
    signature. Pure helper so the guard logic is itself testable against a
    hypothetical second caller (T1b)."""
    tree = ast.parse(source)
    hits: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls_primitive = False
        has_provenance_const = False
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                f = sub.func
                name = (
                    f.id if isinstance(f, ast.Name)
                    else f.attr if isinstance(f, ast.Attribute)
                    else None
                )
                if name == "_atomic_write_yaml":
                    calls_primitive = True
            elif (
                isinstance(sub, ast.Constant)
                and sub.value == "provenance"
            ):
                has_provenance_const = True
        if calls_primitive and has_provenance_const:
            hits.add(node.name)
    return hits


def test_admission_field_writer_pinned_unique():
    """T1a (RED until P2): exactly one sanctioned admission-field writer.

    Fails while the canonical writer does not exist (empty hit set), and fails
    again if any second function ever matches the writer signature."""
    source = (_REPO_ROOT / _ADMISSION_WRITER_MODULE).read_text(encoding="utf-8")
    writers = _admission_field_writer_functions(source)
    assert writers == set(_ADMISSION_WRITER_PIN), (
        "admission-field writer population does not match the pin "
        f"(expected exactly {sorted(_ADMISSION_WRITER_PIN)}, found "
        f"{sorted(writers) or 'NONE — canonical writer not implemented'}). "
        "The capability state overlay admission surface admits ONE sanctioned "
        "writer; amend the pin only in a reviewed diff."
    )


def test_admission_field_writer_guard_flags_second_caller():
    """T1b (guard self-test, expected green): a hypothetical second caller
    matching the writer signature is detected and fails the uniqueness pin."""
    synthetic = (
        "def write_admission_state(record_id, intents=None, tiers=None,\n"
        "                          provenance=None, state_dir=None):\n"
        "    doc = {'id': record_id, 'provenance': provenance}\n"
        "    _atomic_write_yaml(state_dir, doc)\n"
        "\n"
        "def rogue_admission_writer(record_id):\n"
        "    doc = {'id': record_id, 'provenance': {}}\n"
        "    _atomic_write_yaml(record_id, doc)\n"
    )
    writers = _admission_field_writer_functions(synthetic)
    assert writers == {"write_admission_state", "rogue_admission_writer"}, (
        "detector failed to see both the canonical and the rogue writer"
    )
    # The uniqueness pin must FAIL on this population — a second caller can
    # never pass silently.
    assert writers != set(_ADMISSION_WRITER_PIN), (
        "uniqueness pin accepted a two-writer population — guard is blind"
    )
