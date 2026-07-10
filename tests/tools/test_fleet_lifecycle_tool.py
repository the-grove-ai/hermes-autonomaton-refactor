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


# ── P5-S4.1 — tool admission (the andon_write gap class, pinned shut) ────────


def test_fleet_purge_is_admitted_through_the_real_gate():
    """The record loads, binds the tool, and fleet_purge passes
    get_admitted_tools() — the LIVE authority chain, not an import-level
    registry check (the in-process-passes/live-fails trap)."""
    from grove.capability_registry import load_capabilities
    from grove.tool_admission import get_admitted_tools
    from tools.registry import ToolRegistry, register_builtin_tools

    cap = load_capabilities()["fleet_purge"]
    assert cap.zone.value == "red"
    assert cap.bindings.tools == ["fleet_purge"]
    assert cap.trigger.always is True

    reg = ToolRegistry()
    register_builtin_tools(reg)
    admitted = get_admitted_tools(reg, "cli", {})
    assert "fleet_purge" in admitted


def test_every_lifecycle_tool_has_an_admitting_record():
    """STRUCTURAL pin: the gap class cannot recur silently — every tool this
    module registers must be bound by some capability record (else
    get_admitted_tools() filters it and the verb is dead on arrival)."""
    from types import SimpleNamespace

    from grove.capability_registry import load_capabilities

    registered = []
    register(SimpleNamespace(register=lambda **kw: registered.append(kw["name"])))
    bound = set()
    for cap in load_capabilities().values():
        bound.update(cap.bindings.tools)
    missing = [t for t in registered if t not in bound]
    assert not missing, (
        f"lifecycle tool(s) {missing} registered but bound by NO capability "
        f"record — get_admitted_tools() will filter them (the andon_write / "
        f"P5-S4.1 gap class)"
    )


# ── P5-S4.2 — governance-halt wiring (the ceremony-deaf gap class, pinned) ────


def test_recognition_wired_tools_are_ceremony_wired():
    """STRUCTURAL pin (the class dies here): every native tool the grant-
    recognition coverage map knows (grant_covers_halt._NATIVE_TOOL_WRITE_CLASS)
    MUST appear in Dispatcher._NATIVE_GOVERNANCE_TOOLS — a tool present in the
    first but absent from the second is recognition-wired but ceremony-deaf:
    the dispatcher never consults the grant, and every operator-initiated halt
    store-pends to the portal (the S4.2 bake miss). Tools with NO recognition
    (computer_use, propose_governance_change, ...) intentionally take the
    pending-store ceremony and are out of this invariant."""
    import inspect
    import re as _re

    from grove.dispatcher import Dispatcher
    from grove.grant_recognition import grant_covers_halt

    src = inspect.getsource(grant_covers_halt)
    block = src.split("_NATIVE_TOOL_WRITE_CLASS")[1].split("}")[0]
    recognized = set(_re.findall(r'"([a-z_]+)"\s*:', block))
    assert recognized, "coverage-map keys not found — pin needs updating"
    deaf = recognized - Dispatcher._NATIVE_GOVERNANCE_TOOLS
    assert not deaf, (
        f"tool(s) {sorted(deaf)} are in grant_covers_halt's coverage map but "
        f"NOT in Dispatcher._NATIVE_GOVERNANCE_TOOLS — recognition-wired but "
        f"ceremony-deaf: their implicit/standing grants can never resolve"
    )


def _dispatcher_stub(implicit_grant=None):
    from grove.dispatcher import Dispatcher

    d = object.__new__(Dispatcher)  # no __init__ — only grant-path attrs
    d._implicit_grant = implicit_grant
    return d


def test_dispatcher_resolves_implicit_grant_for_fleet_purge_halt():
    """Pin 4: the operator-minted implicit grant resolves a fleet_purge halt
    through _resolve_governance_grant ITSELF — the dispatcher path the S4.2
    bake miss proved untested, not just the grant_covers_halt helper."""
    from grove.dispatcher import Dispatcher
    from grove.grant_recognition import try_mint_implicit_grant

    token = try_mint_implicit_grant(
        "Purge the merchants capital unit from forge.")
    assert token is not None and token.write_class == "fleet_purge"

    halt = _halt_for("fleet_purge", {"skill": "forge-jobsearch",
                                     "unit": "260706-merchants"})
    d = _dispatcher_stub(implicit_grant=token)
    assert Dispatcher._is_governance_mutation_halt(d, halt) is True
    resolved = Dispatcher._resolve_governance_grant(d, halt)
    assert resolved is token  # the T0 implicit grant, not a store-pend


def test_dispatcher_resolves_standing_global_pair_for_fleet_purge(monkeypatch):
    """R2 standing pair: with no implicit token, the store lookup uses the
    GLOBAL (fleet_purge, fleet_purge) pair — args carry no per-target scope."""
    from grove.dispatcher import Dispatcher
    from grove.grants import GrantToken

    standing = GrantToken(source="standing", scope="fleet_purge",
                          write_class="fleet_purge", disposition="standing",
                          authorized_by="operator")
    asked = []

    class _Store:
        def get_grant(self, scope, write_class):
            asked.append((scope, write_class))
            return standing

    import grove.grants as grants_mod
    monkeypatch.setattr(grants_mod, "get_grant_store", lambda: _Store())

    halt = _halt_for("fleet_purge", {"skill": "drafter", "unit": "u1"})
    d = _dispatcher_stub(implicit_grant=None)
    resolved = Dispatcher._resolve_governance_grant(d, halt)
    assert resolved is standing
    assert asked == [("fleet_purge", "fleet_purge")]  # the exact global pair


# ── P5-S4.3 — bake-closure pins ───────────────────────────────────────────────


@pytest.fixture()
def symlinked_home(tmp_path, monkeypatch):
    """The VM trap as a fixture: GROVE_HOME is a SYMLINK into the real data
    dir (~/.grove -> /mnt/grove-data/.grove on prod). The poller records the
    symlink spelling; the purge core realpaths to the target — the S4.3
    matching class."""
    real = tmp_path / "mnt" / "grove-data" / ".grove"
    real.mkdir(parents=True)
    link = tmp_path / "home-grove"
    link.symlink_to(real)
    monkeypatch.setenv("GROVE_HOME", str(link))
    monkeypatch.setenv("GROVE_WIKI_PATH", str(tmp_path / "wiki"))
    return link


def test_tombstone_matches_across_the_symlink(symlinked_home, tmp_path):
    """Pin 1 (the merchants miss): page source: and ledger keys carry the
    SYMLINK spelling; the purge still tombstones + drops them."""
    from grove.wiki.index import WikiIndex

    src = symlinked_home / "drafter" / "draft-2026-01-01-moon.md"
    src.parent.mkdir(parents=True)
    src.write_text("zebra moon body", encoding="utf-8")
    # frontmatter + ledger recorded via the SYMLINK path (as the poller does)
    page = _page(tmp_path, "drafter_draft", "moon-abc12345.md",
                 str(src), "zebra moon compacted")
    _ledger(tmp_path, {str(src): 1.0})
    WikiIndex().build_index()

    out = fleet_purge("drafter", "2026-01-01-moon")

    assert not page.exists()  # tombstoned across the symlink boundary
    ledger = json.loads((tmp_path / "wiki" / ".index" / "ingest_state.json")
                        .read_text())
    assert str(src) not in ledger
    assert "1 wiki page(s) tombstoned" in out and "1 ingest-ledger" in out


def test_resume_discriminator_skips_promote_residue(grove_home):
    """Pin 3a: a manifest-less archive dir WITH meta.json is promote/reject
    residue — the purge mints its OWN dir (resumed False), residue untouched."""
    from grove.utils.fs_utils import purge_artifacts

    gov = {"write_zone": {"staging_dir": "sinkr/pending_review",
                          "canonical_dir": "sinkr"}}
    d = grove_home / "sinkr" / "u1"
    d.mkdir(parents=True)
    (d / "resume.md").write_text("R")
    residue = grove_home / "sinkr" / ".archive" / "u1-20260101T000000Z"
    residue.mkdir(parents=True)
    (residue / "meta.json").write_text("{}")  # promote-era meta-only archive

    res = purge_artifacts([str(d)], gov, unit="u1", reason="r",
                          initiated_by="operator")
    assert res["resumed"] is False
    assert res["archive_dir"] != str(residue)
    assert sorted(p.name for p in residue.iterdir()) == ["meta.json"]  # untouched


def test_resume_discriminator_still_resumes_true_interruptions(grove_home):
    """Pin 3b: manifest-less WITHOUT meta.json = interrupted purge — resumed."""
    from grove.utils.fs_utils import purge_artifacts, storage_transfer

    gov = {"write_zone": {"staging_dir": "sinkr/pending_review",
                          "canonical_dir": "sinkr"}}
    d = grove_home / "sinkr" / "u1"
    d.mkdir(parents=True)
    (d / "resume.md").write_text("R")
    crash = grove_home / "sinkr" / ".archive" / "u1-20260101T000000Z"
    storage_transfer([d / "resume.md"], crash)  # moves done, no manifest

    res = purge_artifacts([str(d)], gov, unit="u1", reason="r",
                          initiated_by="operator")
    assert res["resumed"] is True and res["archive_dir"] == str(crash)


def test_retap_of_completed_purge_finishes_post_steps(grove_home, tmp_path):
    """Pin 4 (the merchants remediation path): purge completed (manifest
    present) but a post-step was missed — the re-tap does NOT raise; it
    completes marker/tombstone/ledger idempotently from the manifest."""
    from grove.wiki.index import WikiIndex

    src = grove_home / "drafter" / "draft-2026-01-01-moon.md"
    src.parent.mkdir(parents=True)
    src.write_text("zebra moon body", encoding="utf-8")
    fleet_purge("drafter", "2026-01-01-moon")  # completed purge

    # simulate the missed tombstone: the derived page + ledger entry linger
    page = _page(tmp_path, "drafter_draft", "moon-late5678.md",
                 str(src), "zebra moon leftover")
    _ledger(tmp_path, {str(src): 1.0})
    WikiIndex().build_index()

    out = fleet_purge("drafter", "2026-01-01-moon")  # re-tap: must not raise
    assert "resumed" in out or "interrupted" in out or "archived" in out
    assert not page.exists()  # leftover tombstoned on the re-tap
    ledger = json.loads((tmp_path / "wiki" / ".index" / "ingest_state.json")
                        .read_text())
    assert str(src) not in ledger
    # still exactly ONE archive dir + one manifest (idempotent, no duplicates)
    dirs = list((grove_home / "drafter" / ".archive").glob("2026-01-01-moon-*"))
    assert len(dirs) == 1
