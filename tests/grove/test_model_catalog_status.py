"""The catalog says which models are on offer, for which tier, and why not:
status, fit and capabilities. Deprecated models leave the picker but still
resolve; an override inherits what it does not state; and the Models page
shows what our own records say about each model."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from grove.config import model_catalog as mc


def _m(slug, out=1.0, **extra):
    return {"slug": slug, "display_name": slug.split("/")[1].title(), "provider": "openrouter",
            "input_cost_per_mtok": out / 2, "output_cost_per_mtok": out, **extra}


CATALOG = [
    _m("a/fast-old", 0.4, status="deprecated", superseded_by="a/fast", fits=["T1"]),
    _m("a/fast", 0.5, fits=["Telemetry", "T1"], context=1048576, tools=True,
       structured_output=True),
    _m("b/cheap", 0.3, status="candidate", fits=["T1"]),
    _m("a/big", 10.0, fits=["T3"]),
    _m("c/anywhere", 2.0),
]


def test_the_picker_offers_what_fits_cheapest_first_and_never_the_deprecated():
    assert [m["slug"] for m in mc.get_models_for_tier("T1", CATALOG)] == [
        "b/cheap", "a/fast", "c/anywhere"]
    assert [m["slug"] for m in mc.get_models_for_tier("T3", CATALOG)] == ["c/anywhere", "a/big"]
    # A tier the operator added, which no entry names, is offered every live model.
    assert [m["slug"] for m in mc.get_models_for_tier("T-QA", CATALOG)] == [
        "b/cheap", "a/fast", "c/anywhere", "a/big"]


def test_a_deprecated_model_still_resolves(tmp_path):
    path = tmp_path / "model-catalog.yaml"
    import yaml
    path.write_text(yaml.safe_dump({"models": CATALOG}), encoding="utf-8")
    loaded = {m["slug"]: m for m in mc._load_catalog_file(path)}
    assert loaded["a/fast-old"]["status"] == "deprecated"
    assert loaded["a/fast-old"]["superseded_by"] == "a/fast"


@pytest.mark.parametrize("bad,why", [
    ({"status": "retired"}, "status must be one of"),
    ({"fits": "T1"}, "non-empty list of tier names"),
    ({"fits": []}, "non-empty list of tier names"),
    ({"tools": "yes"}, "must be true or false"),
    ({"context": 0}, "whole number of tokens"),
    ({"context": True}, "whole number of tokens"),
    ({"superseded_by": ""}, "non-empty string"),
])
def test_a_bad_status_fit_or_capability_is_refused(bad, why):
    with pytest.raises(ValueError, match=why):
        mc._validate_catalog([_m("a/x", **bad)], Path("catalog"))


def test_an_override_inherits_status_and_capabilities_it_does_not_state():
    override = [_m("a/fast-old", 0.9), _m("a/fast", 0.6, fits=["T2"]), _m("z/new", 1.0)]
    merged = {m["slug"]: m for m in mc.merge_catalogs(CATALOG, override)}
    # A price override does not bring a deprecated model back into the picker.
    assert merged["a/fast-old"]["output_cost_per_mtok"] == 0.9
    assert merged["a/fast-old"]["status"] == "deprecated"
    # What the override states, it wins on; what it does not, it inherits.
    assert merged["a/fast"]["fits"] == ["T2"] and merged["a/fast"]["tools"] is True
    assert "status" not in merged["z/new"]
    assert [m["slug"] for m in mc.merge_catalogs(CATALOG, override)][-1] == "z/new"


def test_the_shipped_catalog_is_coherent():
    catalog = mc._load_catalog_file(mc._repo_catalog_path())
    by_slug = {m["slug"]: m for m in catalog}
    for m in catalog:
        if m.get("superseded_by"):
            successor = by_slug.get(m["superseded_by"])
            assert successor is not None, f"{m['slug']} is superseded by an unknown model"
            assert successor.get("status") != "deprecated", (
                f"{m['slug']} is superseded by {successor['slug']}, itself deprecated")
        for field in ("context", "tools", "structured_output"):
            assert field in m, f"{m['slug']} does not state {field}"
    # Every tier has something on offer, and everything on offer can call tools.
    for tier in ("Telemetry", "T1", "T2", "T3"):
        offered = mc.get_models_for_tier(tier, catalog)
        assert offered and all(m["tools"] for m in offered), tier


def test_the_picker_keeps_the_current_model_and_says_why_it_is_not_offered():
    from grove.api import fragments

    html = fragments._model_options_html(CATALOG, "a/fast-old", "T1")
    assert '<option value="a/fast-old" selected>Fast-Old (deprecated)</option>' in html
    assert html.count("a/fast-old") == 1 and "(candidate)" in html
    assert "not offered for this tier" in fragments._model_options_html(CATALOG, "a/big", "T1")
    assert "(not in catalog)" in fragments._model_options_html(CATALOG, "q/gone", "T1")
    # With no tier (a skill pin), every model that is not deprecated.
    plain = fragments._model_options_html(CATALOG, None)
    assert "a/fast-old" not in plain and "a/big" in plain


def test_the_card_flags_a_deprecated_binding_and_prefers_declared_prices(monkeypatch):
    from grove import audit
    from grove.api import fragments

    monkeypatch.setattr(audit, "_prices", lambda home: {"facts": {}, "tiers": {}, "source": None})
    card = fragments.render_tier_card("T1", {"model": "a/fast-old"}, CATALOG)
    assert "Deprecated: superseded by Fast." in card and "(display-only)" in card
    monkeypatch.setattr(audit, "_prices", lambda home: {"facts": {"a/fast": {
        "cost_per_mtok_input": 0.07, "cost_per_mtok_output": 0.21}}, "tiers": {}, "source": "x"})
    card = fragments.render_tier_card("T1", {"model": "a/fast"}, CATALOG)
    assert "$0.07 in / $0.21 out per Mtok (your declared price)" in card
    assert "1M context · tools · structured output" in card


def test_model_evidence_is_counted_from_the_records(tmp_path):
    from grove import audit

    def turn(uid, model, tier, attempts=None, out=1000):
        return {"turn_uid": uid, "model_used": model, "tier_selected": tier, "stages": {
            "compilation": {"escalation": {"attempts": attempts} if attempts else None},
            "execution": {"tokens": {"input": 1000, "output": out}}}}

    rows = [
        turn("u1", "a/fast", "T1"), turn("u2", "a/fast", "T1"),
        turn("u3", "a/big", "T2", attempts=[{"turn_uid": "u2", "tier": "T1"}]),
        turn("u4", "session_rule", "T0"), turn("u5", "pattern_cache", "T0"),
    ]
    (tmp_path / "intent_records.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    (tmp_path / "routing.operational.yaml").write_text(
        "model_facts:\n  a/fast:\n    cost_per_mtok_input: 1\n    cost_per_mtok_output: 2\n",
        encoding="utf-8")
    (tmp_path / "decisions").mkdir()
    (tmp_path / "decisions" / "g.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"kind": "proposed", "id": "p1", "turn_uid": "u1"},
        {"kind": "decided", "ref": "p1", "decision": "confirm"},
        {"kind": "proposed", "id": "p2", "turn_uid": "u3"},
        {"kind": "decided", "ref": "p2", "decision": "confirm"},
        {"kind": "decided", "ref": "p2", "decision": "correct"},
    ]), encoding="utf-8")
    fast, big = audit.model_evidence(tmp_path)
    assert (fast["model"], fast["turns"], fast["failed_upward"], fast["proposed"],
            fast["revised"], fast["tiers"]) == ("a/fast", 2, 1, 1, 0, ["T1"])
    assert fast["cost_per_turn"] == pytest.approx(0.003)
    assert (big["turns"], big["failed_upward"], big["proposed"], big["revised"]) == (1, 0, 1, 1)
    assert big["cost_per_turn"] is None                       # no declared price: not guessed
