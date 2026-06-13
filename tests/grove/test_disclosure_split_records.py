"""GRV-009 E5b C1 — the eager/pull disclosure split, derived from records.

Proves the record-driven split (disclosure_split_sets + build_disclosure_units)
reproduces the legacy build_manifest split BYTE-FOR-BYTE:

* the split-parity golden (eager names + schema hash + pull-index string + token
  counts per tier x intent x complexity cell), captured pre-swap;
* the equivalence gate, run live against the legacy build_manifest on the
  CURRENT/repo taxonomy (== the VM operator-copy): 0 mismatches, and the
  structural identity proactive-always == taxonomy.core;
* _valid_group_names is now the constant catalog (no tool_groups read).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from grove.classify import COMPLEXITY_SIGNALS, INTENT_CLASSES
from grove.context_budget import _name_of, load_taxonomy, resolve_tools_for_tier
from grove.disclosure import (
    build_disclosure_units,
    build_pull_tool_defs,
    disclosure_split_sets,
)
from grove.tier_budget import load_tier_budgets

_REPO = Path(__file__).resolve().parents[2]
_GOLDEN = Path(__file__).parent / "fixtures" / "disclosure_split_golden.json"


def _reg():
    from hermes_cli.tools_config import _cli_registry
    return _cli_registry()


def _setup():
    reg = _reg()
    native = sorted(n for n in {e.name for e in reg._tools.values()} if not n.startswith("mcp_"))
    defs_by = {_name_of(d): d for d in reg.get_definitions(set(native), quiet=True)}
    tooldicts = [defs_by[n] for n in native if n in defs_by]
    budgets = load_tier_budgets(_REPO / "config" / "routing.config.yaml",
                                taxonomy_path=_REPO / "config" / "tool_groups.yaml")
    return reg, defs_by, tooldicts, budgets


def _index_string(units, eager_ids):
    pull = [u for u in units if u.kind in ("tool", "mcp") and u.id not in eager_ids]
    return "\n".join(f"- {u.id}: {u.oneline}" for u in pull) or "(none)"


def _record_split(units, defs_by, res_tools, intent):
    """Reproduce _apply_disclosure's eager/pull from records (native scope)."""
    core, intent_map = disclosure_split_sets()
    matched = {t for t, ins in intent_map.items() if intent is not None and intent in ins}
    eager = [n for n in (_name_of(t) for t in res_tools) if n in core or n in matched]
    return eager, _index_string(units, set(eager))


def test_split_parity_matches_golden_byte_for_byte():
    # The C1-determined artifacts: the eager NAME list (ordered), the pull-index
    # STRING (verbatim), and the index token count. These are exactly what the
    # record-driven split computes. The eager toolset's SCHEMA bytes come verbatim
    # from the registry (res.tools) and are NOT changed by C1 — their per-run
    # identity (record-split eager == legacy eager → same schemas) is proven by
    # test_equivalence_gate; the frozen cross-run schema hash in the golden is a
    # reference only (registry availability varies by environment), not asserted
    # here, so the test tracks C1's change rather than registry env drift.
    from agent.model_metadata import estimate_tokens_rough
    golden = json.loads(_GOLDEN.read_text())
    reg, defs_by, tooldicts, budgets = _setup()
    units = build_disclosure_units(reg)
    mism = {}
    for tier in ("T2", "T3"):
        for intent in INTENT_CLASSES:
            for cx in COMPLEXITY_SIGNALS:
                cell = f"{tier}|{intent}|{cx}"
                g = golden[cell]
                res = resolve_tools_for_tier(tooldicts, intent, cx, None, budgets[tier], mcp_allow=None).tools
                eager, idx = _record_split(units, defs_by, res, intent)
                if (eager != g["eager_names"] or idx != g["pull_index"]
                        or estimate_tokens_rough(idx) != g["pull_tokens"]):
                    mism[cell] = {
                        "eager_diff": sorted(set(eager) ^ set(g["eager_names"])),
                        "index_diff": idx != g["pull_index"],
                    }
    assert not mism, f"split-parity byte mismatch in {len(mism)} cell(s): {dict(list(mism.items())[:5])}"


def test_equivalence_gate_full_byte_parity_in_run():
    # The env-INDEPENDENT byte proof (the operator's "model sees the same bytes",
    # robust to the credential-gated tool surface): for every cell, compute BOTH
    # the legacy build_manifest split AND the record split against the SAME in-run
    # surface, and assert the eager NAME list (ordered), the pull-index STRING
    # (verbatim) and its token count are identical. Same res + same units ⇒ same
    # eager schemas, so this is full byte parity. Also: proactive-always == core.
    from agent.model_metadata import estimate_tokens_rough
    from grove.manifest import build_manifest, matched_tool_units
    reg, defs_by, tooldicts, budgets = _setup()
    repo_tax = load_taxonomy(_REPO / "config" / "tool_groups.yaml")
    legacy_manifest = build_manifest(reg, taxonomy=repo_tax)
    lean_units = build_disclosure_units(reg)
    legacy_core = set(repo_tax.get("core", []))
    rec_core, intent_map = disclosure_split_sets()
    assert set(rec_core) == legacy_core, sorted(set(rec_core) ^ legacy_core)

    def idx(units, eager):
        pull = [u for u in units if u.kind in ("tool", "mcp") and u.id not in set(eager)]
        return "\n".join(f"- {u.id}: {u.oneline}" for u in pull) or "(none)"

    mism = []
    for tier in ("T2", "T3"):
        for intent in INTENT_CLASSES:
            for cx in COMPLEXITY_SIGNALS:
                res = [_name_of(t) for t in
                       resolve_tools_for_tier(tooldicts, intent, cx, None, budgets[tier], mcp_allow=None).tools]
                matched_legacy = matched_tool_units(legacy_manifest, intent_class=intent)
                eager_legacy = [n for n in res if n in legacy_core or n in matched_legacy]
                rec_matched = {t for t, ins in intent_map.items() if intent in ins}
                eager_rec = [n for n in res if n in rec_core or n in rec_matched]
                idx_legacy = idx(legacy_manifest, eager_legacy)
                idx_rec = idx(lean_units, eager_rec)
                if (eager_legacy != eager_rec or idx_legacy != idx_rec
                        or estimate_tokens_rough(idx_legacy) != estimate_tokens_rough(idx_rec)):
                    mism.append(f"{tier}|{intent}|{cx}")
    assert not mism, f"record split diverges from legacy (bytes) in {len(mism)} cells: {mism[:6]}"


def test_build_disclosure_units_pull_index_byte_identical_to_legacy():
    from grove.manifest import build_manifest
    reg, *_ = _setup()
    repo_tax = load_taxonomy(_REPO / "config" / "tool_groups.yaml")
    legacy = build_manifest(reg, taxonomy=repo_tax)
    lean = build_disclosure_units(reg)
    # Same ids, kinds, onelines, order → identical pull index for any eager set.
    assert [(u.id, u.kind, u.oneline) for u in legacy] == [(u.id, u.kind, u.oneline) for u in lean]


def test_valid_group_names_is_constant_catalog():
    from grove.tier_budget import _valid_group_names
    cat = _valid_group_names()  # no args — no tool_groups read
    assert cat == frozenset({"core", "exploratory"} | set(INTENT_CLASSES))
    # accepts real allow_groups names, rejects unknown
    for real in ("core", "retrieval", "code_generation", "exploratory"):
        assert real in cat
    assert "bogus_group" not in cat
