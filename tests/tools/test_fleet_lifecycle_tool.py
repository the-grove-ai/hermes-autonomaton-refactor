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
import os
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
    derived pages tombstoned. P4 (instance-cold-start-parity-v1): retargeted off
    the removed operator-private forge producer onto drafter — the generic
    governance-bearing fleet record with the same canonical/.archive shape."""
    from grove.forge import feedback_store

    slug = "260101-acme-pm"
    d = grove_home / "drafter" / slug
    d.mkdir(parents=True)
    (d / "resume.md").write_text("R")
    (d / "cover-letter.md").write_text("C")
    p1 = _page(tmp_path, "drafter_package", "jim-aaa11111.md",
               str(d / "resume.md"), "resume compacted")
    p2 = _page(tmp_path, "drafter_package", "pitch-bbb22222.md",
               str(d / "cover-letter.md"), "cover compacted")
    _ledger(tmp_path, {str(d / "resume.md"): 1.0,
                       str(d / "cover-letter.md"): 1.0})

    fleet_purge("drafter", slug, unit_id="row-1")

    archived = list((grove_home / "drafter" / ".archive").glob(f"{slug}-*"))
    assert len(archived) == 1
    assert sorted(p.name for p in archived[0].iterdir()) == [
        "cover-letter.md", "purge-manifest.json", "resume.md"]
    assert not p1.exists() and not p2.exists()
    fb = feedback_store.read("drafter", "row-1")  # unit_id key, not slug
    assert fb and fb["terminal_skip"] is True


def test_purge_unknown_skill_fails_loud(grove_home):
    with pytest.raises(ValueError, match="no governance-bearing"):
        fleet_purge("nonexistent", "u1")


def test_purge_nothing_to_purge_fails_loud(grove_home):
    with pytest.raises(ValueError, match="nothing to purge"):
        fleet_purge("drafter", "ghost-unit")


# ── RED ceremony (Verdict C) ─────────────────────────────────────────────────


def test_fleet_purge_zone_is_red():
    # zones-v2-scope-keying: fleet_purge declares effect class `governance`
    # (semantic revocation of a prior operator approval), which DERIVES red.
    from grove.zones import ZoneClassifier, _resolve_schema_path

    clf = ZoneClassifier(_resolve_schema_path(None))
    assert clf._tool_effects["fleet_purge"] == "governance"
    assert clf.classify("fleet_purge").zone == "red"


def test_operator_purge_verb_mints_implicit_grant():
    from grove.grant_recognition import try_mint_implicit_grant

    g = try_mint_implicit_grant("purge 2026-01-01-moon")
    assert g is not None and g.write_class == "fleet_purge"
    assert g.disposition == "once"


def _halt_for(tool_name, args):
    intent = SimpleNamespace(tool_name=tool_name, arguments=args)
    return SimpleNamespace(intents=[intent], triggering_index=0)


def _cap_halt(tool_name, args=None, *, effect_class="external_effect",
              pattern_key=None, zone="yellow", is_promotable=True):
    """standing-grants-v1 Phase 2 — a promotable non-governance YELLOW halt
    carrying a ZoneResult (effect_class + pattern_key + is_promotable): the shape
    the capability mint and consult read from the live halt."""
    zr = SimpleNamespace(
        zone=zone, is_promotable=is_promotable,
        effect_class=effect_class, pattern_key=pattern_key,
    )
    intent = SimpleNamespace(tool_name=tool_name, arguments=args or {})
    return SimpleNamespace(intents=[intent], zone_results=[zr], triggering_index=0)


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


# ── P5-S4.2 / H2 — declaration-driven governance wiring (the map-copy gap
# class, pinned). grant-mint-unification-v1 replaced the five hand-copied
# verb→write_class maps with grove.grant_recognition.WRITE_CLASS_DECLARATION;
# these pins hold every consumer to that single source. The old
# source-parsing test (recognition ⊆ ceremony) is superseded: the inline map
# it parsed no longer exists. ────────────────────────────────────────────────


def test_native_governance_tools_is_the_declaration():
    """Provenance pin (H2, GATE-B F5): the Dispatcher's ceremony set IS the
    routing-filtered declaration — object identity, not a hand-copy. A tool
    added to the declaration is ceremony-wired by construction; a tool
    re-declared on the Dispatcher breaks identity and fails here."""
    from grove.dispatcher import Dispatcher
    from grove.grant_recognition import (
        NATIVE_GOVERNANCE_TOOLS,
        WRITE_CLASS_DECLARATION,
    )

    assert Dispatcher._NATIVE_GOVERNANCE_TOOLS is NATIVE_GOVERNANCE_TOOLS
    assert NATIVE_GOVERNANCE_TOOLS == frozenset(
        name for name, entry in WRITE_CLASS_DECLARATION.items()
        if entry.routing_class == "native"
    )


def test_verb_maps_derive_from_declaration():
    """Provenance pin: GOVERNANCE_VERBS (operator tokens) and
    NATIVE_TOOL_WRITE_CLASS (coverage/resolve/mint map) equal their
    declaration derivations — replacing either with a drifting literal
    fails here."""
    from grove.grant_recognition import (
        GOVERNANCE_VERBS,
        NATIVE_TOOL_WRITE_CLASS,
        WRITE_CLASS_DECLARATION,
    )

    assert GOVERNANCE_VERBS == {
        token: entry.write_class
        for entry in WRITE_CLASS_DECLARATION.values()
        for token in entry.verb_tokens
    }
    assert NATIVE_TOOL_WRITE_CLASS == {
        name: entry.write_class
        for name, entry in WRITE_CLASS_DECLARATION.items()
        if entry.routing_class == "native"
    }
    # Every declared entry is well-formed — the declaration is the schema.
    for name, entry in WRITE_CLASS_DECLARATION.items():
        assert entry.routing_class in ("native", "terminal"), name
        assert entry.scope_policy in ("args_derived", "global"), name
        assert entry.write_class, name


def test_always_mints_standing_grant_for_every_native_verb(tmp_path, monkeypatch):
    """Behavioral mint pin (H2, GATE-B F4) — THE test that catches the bake
    miss: for EVERY native verb in the declaration, a synthetic Always-halt
    runs the UNMOCKED dispatcher mint path into a real tmp grants.yaml and
    the (scope, write_class) entry lands with the declared scope policy
    (fleet_purge → the GLOBAL pair). Before H2, fleet_purge hit a silent
    return here and the operator's Always persisted nothing."""
    from grove.dispatcher import Dispatcher
    from grove.grant_recognition import WRITE_CLASS_DECLARATION
    import grove.grants as grants_mod
    from grove.grants import GrantStore

    store = GrantStore(tmp_path / "grants.yaml")
    monkeypatch.setattr(grants_mod, "_store", store)

    for tool, entry in WRITE_CLASS_DECLARATION.items():
        if entry.routing_class != "native":
            continue
        if entry.scope_policy == "global":
            args = {"skill": "drafter", "unit": "u1"}  # no per-target scope keys
            expected_scope = entry.write_class
        else:
            args = {"skill_name": f"target-{tool}"}
            expected_scope = f"target-{tool}"
        Dispatcher._add_standing_grant_from_halt(
            _dispatcher_stub(), _halt_for(tool, args),
        )
        minted = store.get_grant(expected_scope, entry.write_class)
        assert minted is not None, (
            f"'Always' on native verb {tool!r} minted NO standing grant — "
            f"the S4.2/H2 bake-miss class"
        )
        # sovereign-disposition-hotfix-v1 — the mint persists the execution-gate
        # token 'always', not the write-only orphan 'standing' the gate rejects.
        assert minted.disposition == "always"
        assert minted.authorized_by == "sovereignty_prompt"


def test_always_with_no_resolvable_store_fails_loud():
    """H2 structural floor: the silent returns are dead — an 'Always' whose
    halt resolves no store raises instead of silently dropping the
    operator's decision."""
    from grove.dispatcher import Dispatcher
    from grove.grant_recognition import resolve_always_store

    # Governance-shaped but unparseable: bare verb token, no target.
    halt = _halt_for("terminal", {"command": "promote"})
    assert resolve_always_store(halt) is None
    with pytest.raises(ValueError, match="no.*standing-grant store"):
        Dispatcher._add_standing_grant_from_halt(_dispatcher_stub(), halt)


def test_resolver_governance_and_capability():
    """standing-grants-v1 Phase 2 (D1 refusal → mint): the retired zone_rule
    store's None is now a scope-keyed CAPABILITY store for a PROMOTABLE
    non-governance YELLOW halt. Governance verbs still resolve standing_grant;
    a bare/non-promotable/RED halt still resolves None."""
    from grove.grant_recognition import (
        WRITE_CLASS_DECLARATION,
        resolve_always_store,
    )

    # Promotable non-governance YELLOW halt → capability store (was None).
    got = resolve_always_store(_cap_halt("calendar_create", {}))
    assert got == ("standing_capability", "calendar_create", "external_effect")
    # Bare halt (no ZoneResult), non-promotable, and RED all stay None.
    assert resolve_always_store(_halt_for("browser_read", {})) is None
    assert resolve_always_store(
        _cap_halt("write_file", {}, is_promotable=False)
    ) is None
    assert resolve_always_store(
        _cap_halt("write_file", {}, zone="red")
    ) is None
    # Native governance verbs still resolve a standing grant.
    for tool, entry in WRITE_CLASS_DECLARATION.items():
        if entry.routing_class != "native":
            continue
        args = {} if entry.scope_policy == "global" else {"skill_name": "tgt"}
        got = resolve_always_store(_halt_for(tool, args))
        assert got is not None and got[0] == "standing_grant", (tool, got)


def test_always_affordance_names_store_or_does_not_render():
    """Affordance pin (H2): the TTY menu names the store an Always writes;
    resolver None → option [3] absent from the rendered block and the
    keystroke rejected (silent no-op prohibited)."""
    import io

    from grove.grant_recognition import always_store_label
    from grove.halt_renderer import render_yellow_sovereign_prompt
    from grove.sovereign_prompt_handlers import tty_sovereign_prompt

    # Store resolves → the label names it.
    labeled = render_yellow_sovereign_prompt(
        "terminal", {"command": "ls"}, always_store="zone rule",
    )
    assert "[3] Always (zone rule) — I'll remember it" in labeled
    grant_halt = _halt_for("fleet_purge", {"skill": "drafter", "unit": "u1"})
    assert always_store_label(grant_halt) == "standing grant"

    # standing-grants-v1 Phase 2 FIX 3 — a promotable non-governance YELLOW halt
    # states its exact BREADTH in operator terms on the Always line.
    assert always_store_label(_cap_halt("calendar_create", {})) == "all future calendar_create"
    _wf_dir = os.path.realpath("/tmp/proj/notes")
    assert always_store_label(
        _cap_halt("write_file", {"path": "/tmp/proj/notes/x.md"},
                  effect_class="workspace_write")
    ) == f"all future write_file in {_wf_dir}"

    # No store → no option 3, and choosing it re-prompts instead of minting.
    orphan = _halt_for("terminal", {"command": "promote"})
    assert always_store_label(orphan) is None
    dropped = render_yellow_sovereign_prompt(
        "terminal", {"command": "promote"}, always_store=None,
    )
    assert "[3]" not in dropped and "Always" not in dropped
    assert "[4] Not this time" in dropped

    import builtins
    answers = iter(["3", "4"])
    orig_input = builtins.input
    builtins.input = lambda *a, **k: next(answers)
    try:
        out = io.StringIO()
        assert tty_sovereign_prompt(orphan, out=out) == "deny"
    finally:
        builtins.input = orig_input
    text = out.getvalue()
    assert "[3]" not in text
    assert "Always is unavailable" in text


def _dispatcher_stub(implicit_grant=None, auth_basis="local_presence"):
    from grove.dispatcher import Dispatcher

    d = object.__new__(Dispatcher)  # no __init__ — only grant-path attrs
    d._implicit_grant = implicit_grant
    # standing-grants-v1 Phase 2 — the capability mint reads the consent-auth
    # basis; "local_presence" (tty default) permits the mint. Governance tests
    # ignore it.
    d._sovereign_auth_basis = auth_basis
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


# ── standing-grants-v1 Phase 2 — capability grant mint / consult / floors ─────


def test_capability_mint_consult_revoke_cycle(tmp_path, monkeypatch):
    """The conformant Always end-to-end: a promotable non-governance YELLOW
    'always' mints a capability grant (D-D record shape), the next identical
    halt consults it (bypass), and a same-process revoke kills it."""
    import grove.grants as grants_mod
    from grove.grants import GrantStore

    store = GrantStore(tmp_path / "grants.yaml")
    monkeypatch.setattr(grants_mod, "_store", store)
    d = _dispatcher_stub()

    assert d._add_capability_grant_from_halt(
        _cap_halt("calendar_create", {}, effect_class="external_effect")
    ) is True
    recs = store.list_grants()
    assert len(recs) == 1
    g = recs[0]
    assert g.source == "standing_capability"
    assert g.scope == "calendar_create"
    assert g.write_class == "external_effect"
    assert g.disposition == "always"
    assert g.authorized_by == "sovereignty_prompt"
    assert not g.revoked

    # consult finds it on the next identical halt
    assert d._resolve_capability_grant(_cap_halt("calendar_create", {})) is not None
    # revoke → consult None, same process (synchronous cache invalidation)
    assert store.revoke_grant(g.id) is True
    assert d._resolve_capability_grant(_cap_halt("calendar_create", {})) is None


def test_capability_two_tier_scope_key(tmp_path, monkeypatch):
    """D-A two-tier: arg-bearing tools key on pattern_key (a mismatch is a
    non-match, no fallback); pure-effect tools key on bare tool_name."""
    import grove.grants as grants_mod
    from grove.grants import GrantStore

    monkeypatch.setattr(grants_mod, "_store", GrantStore(tmp_path / "grants.yaml"))
    d = _dispatcher_stub()

    # Arg-bearing: minted at pattern_key "sig1".
    assert d._add_capability_grant_from_halt(
        _cap_halt("execute_code", {"command": "x"},
                  effect_class="workspace_write", pattern_key="sig1")
    ) is True
    assert d._resolve_capability_grant(
        _cap_halt("execute_code", {"command": "x"},
                  effect_class="workspace_write", pattern_key="sig1")
    ) is not None
    # Different pattern_key → NON-MATCH (no fallback to bare tool_name).
    assert d._resolve_capability_grant(
        _cap_halt("execute_code", {"command": "x"},
                  effect_class="workspace_write", pattern_key="sig2")
    ) is None

    # Pure-effect: bare tool granularity — pattern_key is irrelevant.
    assert d._add_capability_grant_from_halt(
        _cap_halt("calendar_create", {}, effect_class="external_effect",
                  pattern_key=None)
    ) is True
    assert d._resolve_capability_grant(
        _cap_halt("calendar_create", {}, effect_class="external_effect",
                  pattern_key="anything")
    ) is not None


def test_capability_consult_leaves_zone_yellow(tmp_path, monkeypatch):
    """The consult relaxes the PROMPT, not the classification — the halt's zone
    stays YELLOW (a grant relaxes YELLOW, never greens it at classify time)."""
    import grove.grants as grants_mod
    from grove.grants import GrantStore

    monkeypatch.setattr(grants_mod, "_store", GrantStore(tmp_path / "grants.yaml"))
    d = _dispatcher_stub()
    d._add_capability_grant_from_halt(_cap_halt("calendar_create", {}))

    h2 = _cap_halt("calendar_create", {})
    assert d._resolve_capability_grant(h2) is not None
    assert h2.zone_results[0].zone == "yellow"  # untouched by the consult


def test_capability_mint_floors_refuse(tmp_path, monkeypatch):
    """The D-C mint floors each REFUSE (fail closed, no crash, no record):
    floor 1a RED/non-promotable, floor 1b scope-defining target, floor 2
    grant-ledger identity, floor 4 admit_all + absent auth basis."""
    from pathlib import Path

    import grove.grants as grants_mod
    import grove.utils.fs_utils as fu
    from grove.grants import GrantStore, get_grant_store

    monkeypatch.setattr(grants_mod, "_store", GrantStore(tmp_path / "grants.yaml"))
    d = _dispatcher_stub()

    # floor 1a — RED / non-promotable (the scope wall is supreme).
    assert d._add_capability_grant_from_halt(
        _cap_halt("write_file", {}, zone="red", is_promotable=False)
    ) is False
    # floor 1b — a scope-defining write target (repo config twin, always walled).
    sd = str(Path(fu._MODULE_CONFIG_ROOT) / "zones.schema.yaml")
    assert d._add_capability_grant_from_halt(
        _cap_halt("write_file", {"path": sd}, effect_class="workspace_write")
    ) is False
    # floor 2 — a grant-ledger identity tool cannot green its own control.
    assert d._add_capability_grant_from_halt(_cap_halt("revoke_grant", {})) is False
    # floor 4 — admit_all and absent auth basis both fail closed.
    assert _dispatcher_stub(auth_basis="admit_all")._add_capability_grant_from_halt(
        _cap_halt("calendar_create", {})
    ) is False
    assert _dispatcher_stub(auth_basis=None)._add_capability_grant_from_halt(
        _cap_halt("calendar_create", {})
    ) is False

    # Not one floor-refused halt minted anything.
    assert get_grant_store().list_grants() == []


def test_capability_mint_floor_grantless_store(tmp_path, monkeypatch):
    """D-C floor 3 (SPEC constraint 4) — a fleet worker's grantless principal
    never mints. The store path is matched via the fleet paths helper, and the
    worker rebinds the process-global store to it BEFORE the Dispatcher is
    constructed, so any consult/mint resolves the grantless store and refuses."""
    import os

    import grove.grants as grants_mod
    from grove.grants import GrantStore, get_grant_store
    from grove.dispatcher import Dispatcher
    from grove.fleet import paths

    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    monkeypatch.setattr(grants_mod, "_store", None)  # fresh process
    grantless = paths.grantless_grants_path("worker-x")
    os.makedirs(grantless.parent, exist_ok=True)

    # Worker step: rebind the singleton to the grantless path (before construct).
    get_grant_store(grants_path=grantless)
    # A later no-arg call (the consult path) returns the SAME grantless store.
    assert get_grant_store()._path == grantless
    assert Dispatcher._is_grantless_store(get_grant_store()) is True

    d = _dispatcher_stub()
    assert d._add_capability_grant_from_halt(_cap_halt("calendar_create", {})) is False
    assert get_grant_store().list_grants() == []


# ── standing-grants-v1 Phase 2 FIX — per-family discriminator + legibility ────


def test_capability_scope_extractor_per_family(tmp_path, monkeypatch):
    """One case per extractor family: the mint writes a family-correct key and
    the consult RECOMPUTES the identical key from an equivalent live halt."""
    import grove.grants as grants_mod
    from grove.grants import GrantStore

    monkeypatch.setattr(grants_mod, "_store", GrantStore(tmp_path / "grants.yaml"))
    d = _dispatcher_stub()

    fpath = str(tmp_path / "notes" / "todo.md")
    fdir = os.path.realpath(str(tmp_path / "notes"))
    cases = [
        # (tool, args, effect_class, pattern_key, expected_scope)
        ("execute_code", {"command": "ls"}, "workspace_write", "sigA",
         "execute_code::sigA"),
        ("write_file", {"path": fpath}, "workspace_write", None,
         f"write_file::{fdir}"),
        ("mcp_notion_notion_create_pages", {"parent": {"database_id": "db1"}},
         "external_effect", None, "mcp_notion_notion_create_pages::db1"),
        ("mcp_notion_notion_update_page", {"page_id": "pg1"},
         "external_effect", None, "mcp_notion_notion_update_page::pg1"),
        ("calendar_create", {"summary": "x"}, "external_effect", None,
         "calendar_create"),
    ]
    from grove.grant_recognition import resolve_always_store
    for tool, args, eff, pk, expected in cases:
        h = _cap_halt(tool, args, effect_class=eff, pattern_key=pk)
        assert resolve_always_store(h)[1] == expected, tool
        assert d._add_capability_grant_from_halt(h) is True, tool
        # consult RECOMPUTES the identical key from an equivalent halt.
        assert d._resolve_capability_grant(
            _cap_halt(tool, dict(args), effect_class=eff, pattern_key=pk)
        ) is not None, tool


def test_capability_extraction_failure_refuses(tmp_path, monkeypatch):
    """An arg-bearing tool whose discriminator is absent → resolve None → mint
    REFUSED (deny). No bare-tool fallback for an arg-bearing tool."""
    import grove.grants as grants_mod
    from grove.grants import GrantStore, get_grant_store
    from grove.grant_recognition import resolve_always_store

    monkeypatch.setattr(grants_mod, "_store", GrantStore(tmp_path / "grants.yaml"))
    d = _dispatcher_stub()

    for tool, args in [
        ("write_file", {}),                                  # no path
        ("execute_code", {"command": "x"}),                  # no pattern_key (None)
        ("mcp_notion_notion_create_pages", {"pages": []}),   # no parent id
        ("mcp_notion_notion_update_page", {}),               # no page id
    ]:
        h = _cap_halt(tool, args, effect_class="workspace_write", pattern_key=None)
        assert resolve_always_store(h) is None, tool
        assert d._add_capability_grant_from_halt(h) is False, tool
    assert get_grant_store().list_grants() == []


def test_capability_effect_class_none_refuses(tmp_path, monkeypatch):
    """FIX 2 (Andon-1 rider) — a null effect_class would mint a null write_class;
    fail closed at BOTH the resolver and the mint floor."""
    import grove.grants as grants_mod
    from grove.grants import GrantStore, get_grant_store
    from grove.grant_recognition import resolve_always_store

    monkeypatch.setattr(grants_mod, "_store", GrantStore(tmp_path / "grants.yaml"))
    d = _dispatcher_stub()
    h = _cap_halt("calendar_create", {}, effect_class=None)
    assert resolve_always_store(h) is None
    assert d._add_capability_grant_from_halt(h) is False
    assert get_grant_store().list_grants() == []


def test_capability_path_normalization_one_key(tmp_path, monkeypatch):
    """write_file/patch key on realpath(dirname): relative/.. spellings that
    resolve to the same directory produce ONE key (mint then consult across
    spellings matches)."""
    import grove.grants as grants_mod
    from grove.grants import GrantStore

    monkeypatch.setattr(grants_mod, "_store", GrantStore(tmp_path / "grants.yaml"))
    d = _dispatcher_stub()

    p_direct = str(tmp_path / "proj" / "a.md")
    p_dotted = str(tmp_path / "proj" / "sub" / ".." / "b.md")  # same dir realpath
    assert d._add_capability_grant_from_halt(
        _cap_halt("write_file", {"path": p_direct}, effect_class="workspace_write")
    ) is True
    # consult with the OTHER spelling of the same directory → match.
    assert d._resolve_capability_grant(
        _cap_halt("write_file", {"path": p_dotted}, effect_class="workspace_write")
    ) is not None
    # exactly one grant (the second spelling did not mint a second).
    from grove.grants import get_grant_store
    assert len(get_grant_store().list_grants()) == 1


def test_capability_grant_round_trip_reload(tmp_path, monkeypatch):
    """Andon-2 ratification — a capability mint PERSISTS with
    source=='standing_capability' and a FRESH GrantStore reload preserves it
    (not relabeled to the governance 'standing')."""
    import grove.grants as grants_mod
    from grove.grants import GrantStore

    path = tmp_path / "grants.yaml"
    monkeypatch.setattr(grants_mod, "_store", GrantStore(path))
    d = _dispatcher_stub()
    assert d._add_capability_grant_from_halt(_cap_halt("calendar_create", {})) is True

    # Fresh store, cold read from disk.
    reloaded = GrantStore(path).list_grants()
    assert len(reloaded) == 1
    assert reloaded[0].source == "standing_capability"
    assert reloaded[0].scope == "calendar_create"
    assert reloaded[0].write_class == "external_effect"
