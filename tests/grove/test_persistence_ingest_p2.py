"""promoted-artifact-persistence-v1 P2 S1 — declarative poller ingest.

Record-driven enumeration ALONGSIDE FLEET_ADAPTERS: a capability declaring
``write_zone.ingest: {surface: canonical_subdirs, source_type: ...}`` gets its
per-unit canonical subdirs (the P1 promote layout, ``<sink>/<unit>/<file>``)
walked with a declaration-fed GenericPackageAdapter — one page per file,
parent-dir lineage, zero producer names.

Local: GROVE_HOME → tmp; fake T1 (the wiki-watcher test discipline); records
injected by monkeypatching load_capabilities where the watcher imports it.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

import grove.wiki.watcher as watcher
from grove.capability import CapabilityKind
from grove.wiki.adapters import GenericPackageAdapter, MalformedSourceDoc
from grove.wiki.watcher import ingest_file, scan_and_ingest


class _FakeT1:
    def __call__(self, prompt, *, system=None, tool=None, max_tokens=4096):
        # P2: all three pipeline calls are forced tools — route by NAME.
        if tool["name"] == "wiki_evaluation":
            return {"complete": True, "accurate": True,
                    "quality_score": 0.9, "issues": []}
        return {"title": "Compacted", "topics": ["t"],
                "key_entities": ["e"], "body": "body\n"}


def _install_t1(monkeypatch):
    monkeypatch.setattr("grove.wiki.pipeline.call_t1", _FakeT1())


def _cap(governance):
    return SimpleNamespace(kind=CapabilityKind.SKILL, governance=governance)


_DECLARED = {
    "write_zone": {
        "staging_dir": "sinkx/pending_review",
        "canonical_dir": "sinkx",
        "promotion": "operator_approval",
        "ingest": {"surface": "canonical_subdirs", "source_type": "sinkx_package"},
    },
}


def _install_records(monkeypatch, caps):
    monkeypatch.setattr(
        "grove.capability_registry.load_capabilities", lambda: caps
    )


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("GROVE_HOME", str(h))
    return h


def _package(home, unit="260101-acme-pm"):
    d = home / "sinkx" / unit
    d.mkdir(parents=True, exist_ok=True)
    (d / "resume.md").write_text("resume body\n", encoding="utf-8")
    (d / "cover-letter.md").write_text("cover body\n", encoding="utf-8")
    return d


# ── enumeration + declaration ────────────────────────────────────────────


def test_record_adapters_derived_from_declaration(home, monkeypatch):
    _install_records(monkeypatch, {"skill.fleet.x": _cap(_DECLARED)})
    out = watcher._record_ingest_adapters(home)
    assert len(out) == 1
    adapter, sink = out[0]
    assert isinstance(adapter, GenericPackageAdapter)
    assert adapter.source_type == "sinkx_package"
    assert sink == home / "sinkx"


def test_no_declaration_no_coverage(home, monkeypatch):
    gov = {"write_zone": {"staging_dir": "sinkx/pending_review",
                          "canonical_dir": "sinkx"}}
    _install_records(monkeypatch, {"skill.fleet.x": _cap(gov)})
    assert watcher._record_ingest_adapters(home) == []


def test_half_declaration_fails_loud(home, monkeypatch):
    gov = {"write_zone": {"canonical_dir": "sinkx",
                          "ingest": {"surface": "canonical_subdirs"}}}
    _install_records(monkeypatch, {"skill.fleet.x": _cap(gov)})
    with pytest.raises(ValueError, match="missing source_type"):
        watcher._record_ingest_adapters(home)


# ── scan: packages ingest, exclusions hold ───────────────────────────────


def test_scan_ingests_package_files_as_separate_pages(home, tmp_path, monkeypatch):
    _install_t1(monkeypatch)
    _install_records(monkeypatch, {"skill.fleet.x": _cap(_DECLARED)})
    _package(home)
    # exclusions: staging subtree, dot-dirs, flat files, meta.json
    (home / "sinkx" / "pending_review" / "u2").mkdir(parents=True)
    (home / "sinkx" / "pending_review" / "u2" / "resume.md").write_text("staged")
    (home / "sinkx" / ".archive" / "u3").mkdir(parents=True)
    (home / "sinkx" / ".archive" / "u3" / "resume.md").write_text("archived")
    (home / "sinkx" / "flat-note.md").write_text("flat")
    (home / "sinkx" / "260101-acme-pm" / "meta.json").write_text("{}")

    wiki = tmp_path / "wiki"
    pages = scan_and_ingest(wiki_root=wiki, hermes_home=home)
    assert len(pages) == 2  # resume + cover-letter, one page per file
    sources = sorted(p.source for p in pages)
    assert sources == [
        str(home / "sinkx" / "260101-acme-pm" / "cover-letter.md"),
        str(home / "sinkx" / "260101-acme-pm" / "resume.md"),
    ]
    assert {p.source_type for p in pages} == {"sinkx_package"}
    # parent-dir lineage: per-file, unit-scoped — the package files coexist
    assert sorted(p.lineage_key for p in pages) == [
        "260101-acme-pm/cover-letter.md", "260101-acme-pm/resume.md",
    ]
    on_disk = list((wiki / "pages" / "sinkx_package").glob("*.md"))
    assert len(on_disk) == 2


def test_repromote_overwrite_triggers_reingest_and_supersession(
        home, tmp_path, monkeypatch):
    """P1 last-write-wins overwrite → mtime change → re-ingest replaces the
    prior page (same source path → same source-hash → stale unlink)."""
    _install_t1(monkeypatch)
    _install_records(monkeypatch, {"skill.fleet.x": _cap(_DECLARED)})
    d = _package(home)
    wiki = tmp_path / "wiki"
    first = scan_and_ingest(wiki_root=wiki, hermes_home=home)
    assert len(first) == 2
    # unchanged rescan is a no-op (mtime ledger)
    assert scan_and_ingest(wiki_root=wiki, hermes_home=home) == []
    # redraft promote: divergent content, bumped mtime
    import os
    (d / "resume.md").write_text("revised resume\n", encoding="utf-8")
    os.utime(d / "resume.md", (0, 4102444800.0))  # deterministic future mtime
    second = scan_and_ingest(wiki_root=wiki, hermes_home=home)
    assert [p.source for p in second] == [str(d / "resume.md")]
    # supersession: still exactly 2 pages for the unit, not 3
    on_disk = list((wiki / "pages" / "sinkx_package").glob("*.md"))
    assert len(on_disk) == 2


def test_explicit_path_ingest_resolves_same_adapter(home, tmp_path, monkeypatch):
    """No-second-ingest-path symmetry: ingest_file on a package file resolves
    the record adapter (source_type from the declaration, not
    operator_curated)."""
    _install_t1(monkeypatch)
    _install_records(monkeypatch, {"skill.fleet.x": _cap(_DECLARED)})
    d = _package(home)
    page = ingest_file(d / "resume.md", wiki_root=tmp_path / "wiki")
    assert page is not None
    assert page.source_type == "sinkx_package"


def test_purge_race_vanished_file_is_skipped(home, tmp_path, monkeypatch):
    """Mitigation 1 — a file that vanishes between enumeration and read is a
    graceful skip; the rest of the scan completes."""
    _install_t1(monkeypatch)
    _install_records(monkeypatch, {"skill.fleet.x": _cap(_DECLARED)})
    d = _package(home)

    real = watcher._ingest_one

    def _racy(source, **kw):
        if source.name == "resume.md":
            raise FileNotFoundError(source)  # vanished post-enumeration
        return real(source, **kw)

    monkeypatch.setattr(watcher, "_ingest_one", _racy)
    pages = scan_and_ingest(wiki_root=tmp_path / "wiki", hermes_home=home)
    assert [p.source for p in pages] == [str(d / "cover-letter.md")]


def test_empty_package_file_fails_loud(home, tmp_path, monkeypatch):
    """A2 posture preserved: PRESENT-but-malformed (empty) stays loud."""
    _install_t1(monkeypatch)
    _install_records(monkeypatch, {"skill.fleet.x": _cap(_DECLARED)})
    d = _package(home)
    (d / "resume.md").write_text("", encoding="utf-8")
    with pytest.raises(MalformedSourceDoc, match="is empty"):
        scan_and_ingest(wiki_root=tmp_path / "wiki", hermes_home=home)


# ── generality (Verdict E extension) ─────────────────────────────────────


def test_generic_ingest_path_is_producer_blind():
    src = "".join(
        inspect.getsource(fn) for fn in (
            GenericPackageAdapter,
            watcher._record_ingest_adapters,
            watcher._iter_package_files,
            watcher._record_adapter_for,
        )
    )
    for name in ("forge", "scout", "drafter", "cultivator", "researcher"):
        assert name not in src, f"producer name {name!r} leaked into P2 ingest"
