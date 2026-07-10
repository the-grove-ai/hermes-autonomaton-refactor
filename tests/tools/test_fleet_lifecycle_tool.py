"""promoted-artifact-persistence-v1 P5 S4 — fleet_purge: RED verb + action layer.

Covers the handler end-to-end (moves+manifest → terminal_skip → wiki
tombstone + ingest-ledger drop), the RED ceremony wiring (zone entry, implicit
grant, standing-grant coverage, effect-signature binding), registration, and
the generality pin.

Local: GROVE_HOME + GROVE_WIKI_PATH → tmp; REAL capability records + fleet
worker config (producer names in tests are fixtures, not lifecycle code).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tools.fleet_lifecycle_tool import (
    FLEET_PURGE_SCHEMA,
    _strip_pattern,
    fleet_purge,
    register,
)


@pytest.fixture()
def grove_home(tmp_path, monkeypatch):
    home = tmp_path / "grove"
    home.mkdir()
    monkeypatch.setenv("GROVE_HOME", str(home))
    monkeypatch.setenv("GROVE_WIKI_PATH", str(tmp_path / "wiki"))
    return home


def _page(tmp_path, source_type: str, name: str, source: str, body: str):
    d = tmp_path / "wiki" / "pages" / source_type
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(
        f"---\ntitle: {name}\nsource_type: {source_type}\nsource: {source}\n"
        f"topics: [t]\nkey_entities: [e]\n---\n\n{body}\n", encoding="utf-8")
    return p


def _ledger(tmp_path, entries: dict):
    d = tmp_path / "wiki" / ".index"
    d.mkdir(parents=True, exist_ok=True)
    (d / "ingest_state.json").write_text(json.dumps(entries), encoding="utf-8")


# ── handler end-to-end: flat file-producer unit ───────────────────────────────


def test_purge_flat_unit_end_to_end(grove_home, tmp_path):
    from grove.forge import feedback_store
    from grove.wiki.index import WikiIndex

    src = grove_home / "drafter" / "draft-2026-01-01-moon.md"
    src.parent.mkdir(parents=True)
    src.write_text("zebra moon draft body", encoding="utf-8")
    keep = grove_home / "drafter" / "draft-2026-01-01-keep.md"
    keep.write_text("unrelated keep body", encoding="utf-8")

    purged_page = _page(tmp_path, "drafter_draft", "moon-abc12345.md",
                        str(src), "zebra moon compacted")
    kept_page = _page(tmp_path, "drafter_draft", "keep-def67890.md",
                      str(keep), "unrelated compacted")
    _ledger(tmp_path, {str(src): 1.0, str(keep): 2.0})
    idx = WikiIndex()
    idx.build_index()
    assert any("moon" in r.title.lower() for r in idx.query("zebra moon"))

    out = fleet_purge("drafter", "2026-01-01-moon", reason="stale")

    # moves + manifest (core, already unit-pinned — smoke here)
    assert not src.exists()
    archived = list((grove_home / "drafter" / ".archive").glob("2026-01-01-moon-*"))
    assert len(archived) == 1
    assert (archived[0] / "purge-manifest.json").is_file()
    assert keep.exists()  # unrelated canonical untouched
    # terminal_skip marker
    fb = feedback_store.read("drafter", "2026-01-01-moon")
    assert fb and fb["terminal_skip"] is True
    # wiki tombstone: purged page unlinked + FTS rows gone; neighbour intact
    assert not purged_page.exists()
    assert kept_page.exists()
    assert not any("moon" in r.title.lower() for r in WikiIndex().query("zebra moon"))
    # ingest-ledger drop: purged source's entry gone, neighbour's stays
    ledger = json.loads((tmp_path / "wiki" / ".index" / "ingest_state.json")
                        .read_text())
    assert str(src) not in ledger and str(keep) in ledger
    assert "1 wiki page(s) tombstoned" in out and "1 ingest-ledger" in out


def test_purge_package_unit_end_to_end(grove_home, tmp_path):
    """Remote-sink P1 subdir layout: dir source, both files archived, both
    derived pages tombstoned."""
    from grove.forge import feedback_store

    slug = "260101-acme-pm"
    d = grove_home / "forge" / slug
    d.mkdir(parents=True)
    (d / "resume.md").write_text("R")
    (d / "cover-letter.md").write_text("C")
    p1 = _page(tmp_path, "forge_package", "jim-aaa11111.md",
               str(d / "resume.md"), "resume compacted")
    p2 = _page(tmp_path, "forge_package", "pitch-bbb22222.md",
               str(d / "cover-letter.md"), "cover compacted")
    _ledger(tmp_path, {str(d / "resume.md"): 1.0,
                       str(d / "cover-letter.md"): 1.0})

    fleet_purge("forge-jobsearch", slug, unit_id="row-1")

    archived = list((grove_home / "forge" / ".archive").glob(f"{slug}-*"))
    assert len(archived) == 1
    assert sorted(p.name for p in archived[0].iterdir()) == [
        "cover-letter.md", "purge-manifest.json", "resume.md"]
    assert not p1.exists() and not p2.exists()
    fb = feedback_store.read("forge", "row-1")  # unit_id key, not slug
    assert fb and fb["terminal_skip"] is True


def test_purge_unknown_skill_fails_loud(grove_home):
    with pytest.raises(ValueError, match="no governance-bearing"):
        fleet_purge("nonexistent", "u1")


def test_purge_nothing_to_purge_fails_loud(grove_home):
    with pytest.raises(ValueError, match="nothing to purge"):
        fleet_purge("drafter", "ghost-unit")


# ── RED ceremony (Verdict C) ─────────────────────────────────────────────────


def test_fleet_purge_zone_is_red():
    schema = yaml.safe_load(
        Path("config/zones.schema.yaml").read_text(encoding="utf-8"))
    assert schema["tool_zones"]["fleet_purge"] == "red"


def test_operator_purge_verb_mints_implicit_grant():
    from grove.grant_recognition import try_mint_implicit_grant

    g = try_mint_implicit_grant("purge 2026-01-01-moon")
    assert g is not None and g.write_class == "fleet_purge"
    assert g.disposition == "once"


def _halt_for(tool_name, args):
    intent = SimpleNamespace(tool_name=tool_name, arguments=args)
    return SimpleNamespace(intents=[intent], triggering_index=0)


def test_standing_grant_exact_pair_covers_fleet_purge():
    from grove.grant_recognition import grant_covers_halt
    from grove.grants import GrantToken

    grant = GrantToken(source="t", scope="fleet_purge",
                       write_class="fleet_purge", disposition="standing",
                       authorized_by="operator")
    halt = _halt_for("fleet_purge", {"skill": "drafter", "unit": "u1"})
    assert grant_covers_halt(grant, halt) is True
    wrong = GrantToken(source="t", scope="fleet_purge",
                       write_class="andon_reject", disposition="standing",
                       authorized_by="operator")
    assert grant_covers_halt(wrong, halt) is False


def test_effect_signature_binds_purge_args():
    from grove.effect_signature import canonical_effect_signature

    a = canonical_effect_signature("fleet_purge", {"skill": "s", "unit": "u1"})
    b = canonical_effect_signature("fleet_purge", {"skill": "s", "unit": "u1"})
    c = canonical_effect_signature("fleet_purge", {"skill": "s", "unit": "u2"})
    assert a == b and a != c


# ── registration + generality ────────────────────────────────────────────────


def test_registers_fleet_purge():
    calls = []
    register(SimpleNamespace(register=lambda **kw: calls.append(kw)))
    assert len(calls) == 1
    assert calls[0]["name"] == "fleet_purge"
    assert calls[0]["toolset"] == "fleet_lifecycle"
    assert calls[0]["schema"] is FLEET_PURGE_SCHEMA


def test_strip_pattern_read_side_rule():
    assert _strip_pattern("draft-2026-01-01-moon.md", "draft-*.md") == "2026-01-01-moon"
    assert _strip_pattern("digest-x.json", "digest-*.json") == "x"
    assert _strip_pattern("odd.md", "*") == "odd.md"


def test_purge_action_layer_is_producer_blind():
    """Generality pin extended: the verb + resolvers name no producer —
    skill/unit arrive as tool ARGUMENTS."""
    import inspect

    import tools.fleet_lifecycle_tool as flt

    src = (inspect.getsource(flt.fleet_purge)
           + inspect.getsource(flt._capability_for)
           + inspect.getsource(flt._worker_for)
           + inspect.getsource(flt._strip_pattern))
    for name in ("forge", "scout", "drafter", "cultivator", "researcher"):
        assert name not in src, f"producer name {name!r} in the action layer"
