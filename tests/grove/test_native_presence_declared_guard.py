"""native-presence-declared-v1 · P1 — THE GUARD (two-part: config + AST).

Enforces the locked class (SPEC 3a7780a78eef81349c6cc8689837b7c5 §4):

    Capability PRESENCE is declared, never inferred. A record may not gate
    its presence on the turn's classification. Concretely: no record may
    carry a non-empty ``trigger.intents`` unless it is ``always: true``.
    And the native resolver ``_registry_allowed_names`` may neither read
    ``intent_class`` on the presence path nor subtract from the accumulating
    name set (union-only).

Two parts, per GATE-B finding E (a config-only guard is blind to a new
subtractive branch added to context_budget.py next month):

  PART 1 — config assertion. Zero records declare intent-gated presence,
           asserted as EXACT MATCH against a declared suppression ledger
           (never census-zero). Every ledger entry names an owning sprint.
           The guard discloses its own suppressions on pass.

  PART 2 — AST assertion on _registry_allowed_names (the load-bearing half):
           (a) no presence-path branch reads ``intent_class``;
           (b) no removal branch exists (nothing narrows the name set).
           Precedent: writer-conformance-guard-v1 / binding-opacity-v1 —
           AST pins catch what literal greps miss (parametrized names).

This file is a TEST ONLY. It writes nothing, deletes nothing, flips no
record, proposes no fix. At P1 the guard is EXPECTED RED — its failure
list IS the census (SPEC "must FAIL loudly against current main").
Expected first run: PART 1 → 21 failures; PART 2(a) → RED; PART 2(b) → GREEN.
If PART 2(b) is RED: ANDON 1 — the union-only premise is false; halt.
"""
from __future__ import annotations

import ast
import types
from pathlib import Path

import pytest

from grove.capability_registry import load_capabilities

# ── repo geometry ─────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parents[2]
CAP_DIR = REPO_ROOT / "config" / "capabilities"          # ships in-repo; no overlay
RESOLVER_SRC = REPO_ROOT / "grove" / "context_budget.py"
RESOLVER_FUNC = "_registry_allowed_names"

# ── owning sprints ────────────────────────────────────────────────────────
FLIP_SPRINT = "native-presence-declared-v1"       # this sprint: flipped to always:true
MCP_WAVE = "the MCP-plane wave"                    # notion_write: intents live on MCP path
T1_PARITY = "t1-disclosure-pull-parity"           # complexity class: T1 pull parity
HOLD_SPRINT = "researcher-retrieval-broker-v1"     # the 4 held fleet records (hold field)

# ── THE LEDGER — exactly two entries, each naming an owning sprint (SPEC §4) ──
# The config guard is green only while the Part-1 census is EXACTLY the declared
# config-census suppression set (never census-zero). Two entries survive P2:
#   • notion_write     — kind=mcp; trigger.intents are LIVE on the MCP admission
#     path (_mcp_trigger_reason), which this sprint does not enter. Flipping it
#     always:true would pin the notion server into mcp_allow every turn. It is the
#     sole record left in the Part-1 census. Owner: the MCP wave.
#   • complexity_class — a DECLARED EXCEPTION (P1c/M3/M4): inference-SIZED but
#     pull-reachable on T2/T3, unreachable on T1. NOT in the Part-1 census (empty
#     intents, always:true); declared so the guard discloses it and catches drift.
#     Owner: t1-disclosure-pull-parity.
LEDGER: dict[str, dict] = {
    "notion_write": {
        "owning_sprint": MCP_WAVE, "kind": "config_census",
        "note": "kind=mcp; intents live on the MCP plane (_mcp_trigger_reason)",
    },
    "complexity_class": {
        "owning_sprint": T1_PARITY, "kind": "declared_exception",
        "records": [
            "browser_write", "delegate_task", "feishu_doc_read", "ha_call_service",
            "ha_get_state", "mixture_of_agents", "video_analyze", "vision_analyze",
        ],
        "note": "inference-sized; pull-reachable on T2/T3, unreachable on T1",
    },
}
# HARD CAP: 10 (SPEC "EXEMPTION CAP: 10"). Andon 2 if exceeded.
SUPPRESSION_CAP = 10
# Part-1 census suppressions, derived from the ledger.
CONFIG_SUPPRESSIONS: dict[str, dict] = {
    k: v for k, v in LEDGER.items() if v.get("kind") == "config_census"
}
COMPLEXITY_LEDGER_RECORDS: list[str] = list(LEDGER["complexity_class"]["records"])


# ── predicate (SPEC §4 PART 1, verbatim) ──────────────────────────────────
def _is_intent_gated(cap) -> bool:
    """A record declares intent-gated presence iff it carries a non-empty
    ``trigger.intents`` and is NOT ``always: true``. ``always: true`` bypasses
    the intent match entirely (context_budget.py:544-547), so an always-record's
    intents are moot — not intent-gated presence."""
    return bool(cap.trigger.intents) and cap.trigger.always is not True


def _intent_gated_census() -> dict[str, object]:
    """The live census over the in-repo record set (explicit dir = no overlay,
    no state — exactly what ships)."""
    recs = load_capabilities(directory=CAP_DIR)
    return {cid: c for cid, c in recs.items() if _is_intent_gated(c)}


# ── PART 1 · tests ────────────────────────────────────────────────────────
def test_config_no_undeclared_intent_gated_presence():
    """THE CONFIG GUARD. Every intent-gated record must be a declared config-census
    suppression; any that is not is an undeclared violation. Green only when the
    census is EXACTLY the declared suppression set — never census-zero. After P2b
    the census is {notion_write}; the guard discloses it (owner: the MCP wave)."""
    census = set(_intent_gated_census())
    undeclared = sorted(census - set(CONFIG_SUPPRESSIONS))
    if not undeclared:  # disclose suppressions on pass (SPEC §4)
        print(
            "[native-presence-declared] PASS — sanctioned intent-gated records:\n"
            + "\n".join(f"  {cid}  [{meta['owning_sprint']}]"
                        for cid, meta in sorted(CONFIG_SUPPRESSIONS.items()))
        )
    assert not undeclared, (
        f"{len(undeclared)} record(s) declare intent-gated presence (non-empty "
        f"trigger.intents, not always:true) and are NOT in the declared suppression "
        f"ledger:\n" + "\n".join(f"  {cid}" for cid in undeclared)
    )


def test_baseline_always_false_class_is_empty():
    """ASSERTION 3 (P2c). The class {disclosure: baseline AND always: false} must be
    EMPTY. Baseline admits unconditionally, IGNORING always — so such a record
    declares itself absent (always:false) yet is offered every turn. web_search was
    the only instance; P2b flipped it. Assert the class closes structurally, not by
    coincidence."""
    recs = load_capabilities(directory=CAP_DIR)
    offenders = sorted(
        cid for cid, c in recs.items()
        if c.trigger.disclosure.value == "baseline" and c.trigger.always is not True
    )
    assert not offenders, (
        "baseline + always:false records declare absent yet are offered every turn "
        f"(baseline ignores always): {offenders}"
    )


def test_complexity_exception_matches_ledger():
    """Drift guard for the DECLARED complexity exception. The complexity-disclosure
    class must be EXACTLY the ledger's declared records (owner t1-disclosure-pull-
    parity). Discloses the exception on pass; a new complexity record or a removed
    one flags here."""
    recs = load_capabilities(directory=CAP_DIR)
    live = sorted(cid for cid, c in recs.items() if c.trigger.disclosure.value == "complexity")
    declared = sorted(COMPLEXITY_LEDGER_RECORDS)
    if live == declared:
        print("[native-presence-declared] complexity exception (owner %s): %s"
              % (LEDGER["complexity_class"]["owning_sprint"], declared))
    assert live == declared, (
        "complexity class diverged from the declared exception ledger.\n"
        f"  live not declared: {sorted(set(live) - set(declared))}\n"
        f"  declared not live: {sorted(set(declared) - set(live))}"
    )


def test_ledger_has_exactly_two_entries():
    """SPEC §4/P2c: exactly two ledger entries, each naming an owning sprint."""
    assert len(LEDGER) == 2, f"expected exactly 2 ledger entries, got {sorted(LEDGER)}"
    for key, meta in LEDGER.items():
        assert isinstance(meta.get("owning_sprint"), str) and meta["owning_sprint"].strip(), \
            f"ledger entry {key!r} names no owning sprint"


def test_suppression_ledger_under_cap_and_reasoned():
    assert len(CONFIG_SUPPRESSIONS) <= SUPPRESSION_CAP, (
        f"{len(CONFIG_SUPPRESSIONS)} suppressions exceeds the cap of {SUPPRESSION_CAP} "
        "— ANDON 2: the class is not being enforced; halt rather than write a pile."
    )
    for cid, meta in CONFIG_SUPPRESSIONS.items():
        assert meta.get("owning_sprint", "").strip(), \
            f"suppression {cid!r} names no owning sprint (SPEC §4)"


def test_suppression_ledger_not_stale():
    """A suppression that no longer trips the predicate must be removed — its
    owning sprint has landed."""
    census = set(_intent_gated_census())
    stale = sorted(set(CONFIG_SUPPRESSIONS) - census)
    assert not stale, f"stale suppressions (no longer intent-gated): {stale}"


def test_predicate_positive_control():
    """The predicate MUST flag intent-gated and MUST clear both always:true+intents
    and empty-intents. If it does not, the guard is broken, not the codebase."""
    ns = types.SimpleNamespace
    gated = ns(trigger=ns(intents=["system_admin"], always=False))
    always_with_intents = ns(trigger=ns(intents=["system_admin"], always=True))
    baseline_no_intents = ns(trigger=ns(intents=[], always=False))
    assert _is_intent_gated(gated) is True
    assert _is_intent_gated(always_with_intents) is False
    assert _is_intent_gated(baseline_no_intents) is False


# ── PART 2 · AST over _registry_allowed_names ─────────────────────────────
def _resolver_func_node() -> ast.FunctionDef:
    tree = ast.parse(RESOLVER_SRC.read_text(), filename=str(RESOLVER_SRC))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == RESOLVER_FUNC:
            return node
    raise AssertionError(f"{RESOLVER_FUNC} not found in {RESOLVER_SRC}")


def _intent_class_reads(func_node: ast.FunctionDef) -> list[int]:
    """Load-context references to the ``intent_class`` parameter anywhere in the
    body (signature args are ast.arg, not ast.Name, so they are excluded)."""
    return sorted({
        n.lineno for n in ast.walk(func_node)
        if isinstance(n, ast.Name) and n.id == "intent_class"
        and isinstance(n.ctx, ast.Load)
    })


# Any op that narrows an accumulating set — the shapes a literal grep for
# ``.remove`` would miss (parametrized method names, operator forms).
_REMOVAL_ATTRS = frozenset({
    "remove", "discard", "pop", "clear",
    "difference", "difference_update",
    "intersection", "intersection_update",
    "symmetric_difference", "symmetric_difference_update",
})


def _removal_ops(func_node: ast.FunctionDef) -> list[tuple[str, int]]:
    hits: list[tuple[str, int]] = []
    for n in ast.walk(func_node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr in _REMOVAL_ATTRS:
            hits.append((f".{n.func.attr}()", n.lineno))
        elif isinstance(n, ast.AugAssign) and isinstance(n.op, (ast.Sub, ast.BitAnd)):
            hits.append((f"aug {type(n.op).__name__}", n.lineno))
        elif isinstance(n, ast.Assign) and isinstance(n.value, ast.BinOp) \
                and isinstance(n.value.op, (ast.Sub, ast.BitAnd)):
            hits.append((f"binop {type(n.value.op).__name__}", n.lineno))
    return sorted(hits, key=lambda h: h[1])


def test_ast_a_presence_path_does_not_read_intent_class():
    """(a) EXPECTED RED at P1. Presence must not be inference-determined; the
    resolver reads ``intent_class`` (SPEC pins :560, ``intent_class in intents``).
    Phase 2 deletes the intent_match branch. Green only when no read remains."""
    reads = _intent_class_reads(_resolver_func_node())
    assert not reads, (
        f"{RESOLVER_FUNC} reads the `intent_class` parameter at line(s) {reads} — "
        "presence is inference-determined. SPEC §3 deletes the intent_match branch. "
        "EXPECTED RED at native-presence-declared-v1 P1."
    )


def test_ast_b_no_removal_branch():
    """(b) EXPECTED GREEN. The native surface is union-only (names.update per
    matched record). Any set-narrowing op falsifies GATE-A's additive-only
    finding → ANDON 1: halt, the union-only premise is false."""
    hits = _removal_ops(_resolver_func_node())
    assert not hits, (
        f"ANDON 1 — union-only premise FALSE. {RESOLVER_FUNC} narrows the name set: "
        f"{hits}. GATE-A recorded every native input as additive-only; halt and re-scope."
    )


# ── PART 2 · positive controls (detectors must catch known shapes) ─────────
def _first_func(src: str) -> ast.FunctionDef:
    return next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef))


def test_ast_read_detector_positive_control():
    ctrl = (
        "def f(intent_class, intents):\n"
        "    names = set()\n"
        "    if intent_class in intents:\n"
        "        names.add(1)\n"
        "    return names\n"
    )
    assert _intent_class_reads(_first_func(ctrl)), \
        "read detector failed to flag a synthetic `intent_class in intents` branch"


def test_ast_removal_detector_positive_control():
    ctrl = (
        "def f(a, b):\n"
        "    names = set()\n"
        "    names.update(a)\n"
        "    names.discard(b)\n"
        "    names -= {1}\n"
        "    return names\n"
    )
    hits = _removal_ops(_first_func(ctrl))
    assert any(h[0] == ".discard()" for h in hits) and any("Sub" in h[0] for h in hits), \
        f"removal detector failed to flag synthetic narrowing ops; got {hits}"
