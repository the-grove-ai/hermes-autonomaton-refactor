"""forge-unattended-publish-v1 P2 — unit tests for the PURE resolver.

Pins the jail-rooting and read-only discipline directly: no path is ever taken
from meta.json, the slug cannot escape the staging root, and every failure is a
returned ForgePackageUnresolvable (never a raise).
"""

import json
from pathlib import Path

from grove.forge.resolve import (
    ForgePackageUnresolvable,
    ResolvedForgePackage,
    resolve_forge_package,
)


def _stage(home: Path, slug: str, meta: dict, *, resume=True, cover=True) -> Path:
    d = home / "forge" / "pending_review" / slug
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    if resume:
        (d / "resume.md").write_text("r", encoding="utf-8")
    if cover:
        (d / "cover-letter.md").write_text("c", encoding="utf-8")
    return d


def test_resolves_labels_and_fixed_paths(tmp_path):
    d = _stage(tmp_path, "ok", {"company": "Acme", "role": "PM", "row_id": "X"})
    r = resolve_forge_package(tmp_path, "ok")
    assert isinstance(r, ResolvedForgePackage)
    assert (r.company, r.role) == ("Acme", "PM")
    assert r.resume_path == str((d / "resume.md").resolve())
    assert r.cover_path == str((d / "cover-letter.md").resolve())


def test_meta_path_key_is_ignored(tmp_path):
    d = _stage(tmp_path, "inj", {"company": "A", "role": "R",
                                 "resume_path": "/etc/passwd"})
    r = resolve_forge_package(tmp_path, "inj")
    assert isinstance(r, ResolvedForgePackage)
    assert r.resume_path == str((d / "resume.md").resolve())


def test_missing_dir_is_no_draft_dir(tmp_path):
    r = resolve_forge_package(tmp_path, "nope")
    assert isinstance(r, ForgePackageUnresolvable) and r.kind == "no_draft_dir"


def test_slug_traversal_cannot_escape_jail(tmp_path):
    r = resolve_forge_package(tmp_path, "../../etc")
    assert isinstance(r, ForgePackageUnresolvable) and r.kind == "no_draft_dir"


def test_missing_meta_is_meta_invalid(tmp_path):
    d = tmp_path / "forge" / "pending_review" / "nometa"
    d.mkdir(parents=True)
    r = resolve_forge_package(tmp_path, "nometa")
    assert isinstance(r, ForgePackageUnresolvable) and r.kind == "meta_invalid"


def test_meta_missing_labels_is_meta_invalid(tmp_path):
    _stage(tmp_path, "bad", {"row_id": "X"})  # no company/role
    r = resolve_forge_package(tmp_path, "bad")
    assert isinstance(r, ForgePackageUnresolvable) and r.kind == "meta_invalid"


def test_missing_content_is_content_missing(tmp_path):
    _stage(tmp_path, "half", {"company": "A", "role": "R"}, cover=False)
    r = resolve_forge_package(tmp_path, "half")
    assert isinstance(r, ForgePackageUnresolvable) and r.kind == "content_missing"
