"""forge-unattended-publish-v1 P2 — PURE, side-effect-free forge package resolver.

Jail-rooted resolution of a staged forge package: given ``home`` and a ``slug``,
locate the staging slug dir, read ``meta.json`` for the display LABELS
(company / role), and resolve the two FIXED draft filenames within that dir.

Discipline (the loop's invariants):
  * READ-ONLY — zero writes, zero external calls (no Drive, no Notion, no token).
  * JAIL-ROOTED — the slug dir is the forge staging root
    (``<home>/forge/pending_review/<slug>``), containment-checked; the content
    paths are the FIXED names ``resume.md`` / ``cover-letter.md`` resolved inside
    it, re-checked for containment. No path is EVER read from ``meta.json`` — a
    ``resume_path`` (or any path key) an untrusted worker injects there is ignored.
  * LABELS ONLY — ``company`` / ``role`` come from ``meta.json`` as untrusted
    display strings; ``row_id`` is NOT sourced here (the loop takes it from the
    event, the authoritative row identity).

This resolver deliberately re-implements (does not yet share) the portal
wrapper's inline resolution (``grove.api.actions._forge_publish_core``): Phase 2
leaves the attended sovereign-write path byte-untouched. Consolidation is a
future refactor once the attended path has fuller branch coverage.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Union


@dataclass(frozen=True)
class ResolvedForgePackage:
    """A staged forge package resolved to jail-rooted, verified paths + labels."""

    slug: str
    slug_dir: Path
    company: str
    role: str
    resume_path: str
    cover_path: str


@dataclass(frozen=True)
class ForgePackageUnresolvable:
    """Why a package could not be resolved. ``kind`` is a stable check token for
    the Andon; ``reason`` is the human detail. Never raised — returned."""

    kind: str  # "no_draft_dir" | "meta_invalid" | "content_missing"
    reason: str


_STAGING_SUBPATH = ("forge", "pending_review")
_RESUME_NAME = "resume.md"
_COVER_NAME = "cover-letter.md"


def resolve_forge_package(
    home: Path, slug: str
) -> Union[ResolvedForgePackage, ForgePackageUnresolvable]:
    """Resolve the staged package for *slug* under *home*, jail-rooted + read-only.

    Returns a :class:`ResolvedForgePackage` on success, or a
    :class:`ForgePackageUnresolvable` (never raises) describing the first failure.
    """
    if not slug or not isinstance(slug, str):
        return ForgePackageUnresolvable("no_draft_dir", f"invalid slug {slug!r}")

    staging_root = Path(home).joinpath(*_STAGING_SUBPATH).resolve()
    slug_dir = (staging_root / slug).resolve()
    # Containment — the slug must not escape the staging jail (e.g. "../..").
    if not slug_dir.is_relative_to(staging_root) or not slug_dir.is_dir():
        return ForgePackageUnresolvable(
            "no_draft_dir", f"no forge staging dir for {slug!r}"
        )

    meta_path = slug_dir / "meta.json"
    if not meta_path.is_file():
        return ForgePackageUnresolvable(
            "meta_invalid", "meta.json not found in the slug dir"
        )
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return ForgePackageUnresolvable("meta_invalid", f"meta.json is unreadable: {exc}")
    if not isinstance(meta, dict) or not all(meta.get(k) for k in ("company", "role")):
        return ForgePackageUnresolvable(
            "meta_invalid", "meta.json is missing company/role"
        )

    # FIXED filenames only — meta.json paths are never consulted. Re-check
    # containment defensively (a fixed basename cannot escape, but resolve+check
    # keeps the jail guarantee explicit and local).
    resume = (slug_dir / _RESUME_NAME).resolve()
    cover = (slug_dir / _COVER_NAME).resolve()
    for label, p in (("resume.md", resume), ("cover-letter.md", cover)):
        if not p.is_relative_to(slug_dir) or not p.is_file():
            return ForgePackageUnresolvable(
                "content_missing", f"missing draft file {label} in {slug!r}"
            )

    return ResolvedForgePackage(
        slug=slug,
        slug_dir=slug_dir,
        company=str(meta["company"]),
        role=str(meta["role"]),
        resume_path=str(resume),
        cover_path=str(cover),
    )
