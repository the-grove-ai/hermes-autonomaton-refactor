"""Source adapters — the Writer's input contract for the living cellar.

Sprint K1 (living-cellar-v1) Phase 3. A Strategy pattern over the two
invocation models that feed the wiki:

* **Four fleet adapters** (glob-keyed, walked by the Phase 5 watcher) — each
  parses one Fleet skill's sink output. The shapes are heterogeneous (two
  nested-JSON envelopes, one Markdown-with-frontmatter, one declared JSON), so
  each adapter owns its own parser; there is no monolithic normalizer. Glob
  matching is strict: off-glob files (e.g. ``thinkpiece-*.md`` in the
  researcher sink) are ignored at the walk via :func:`fleet_adapter_for`, never
  errored. A2 — a file that MATCHES its glob but FAILS its parser shape raises
  :class:`MalformedSourceDoc` (fail loud), never a silent skip.

* **One operator_curated adapter** (path-invoked via ``hermes wiki ingest
  <file>``, NOT glob-walked) — ingests a plain ``.md``/``.txt`` at an explicit
  path. The body goes to the Writer; optional YAML frontmatter is parsed
  best-effort if present (never required). It fails loud only on an
  unreadable/empty file.

Every adapter returns a :class:`NormalizedDoc` carrying the raw content the
Writer compacts plus DETERMINISTIC, adapter-owned metadata (source path,
source_type, source mtime as the created/updated basis, and any
``dock_goal_refs`` present in the source — else empty). The Writer never sets
these deterministic fields.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)

# Researcher brief filename -> lineage slug: brief-YYYY-MM-DD-SLUG.json. The
# slug (everything after the ISO date) is the replace-by-topic recurrence key.
_BRIEF_SLUG_RE = re.compile(r"^brief-\d{4}-\d{2}-\d{2}-(.+)$")


class MalformedSourceDoc(ValueError):
    """A source file matched its adapter but cannot be parsed to its declared
    shape — fail loud (A2)."""


@dataclass(frozen=True)
class NormalizedDoc:
    """Adapter output: raw content for the Writer + deterministic metadata."""

    source_type: str
    source_path: str
    source_mtime: float
    dock_goal_refs: List[str]
    raw_content: str
    # Mesh-primitive lineage seam (Sprint R1): the recurrence/slug key a
    # superseding adapter computes, or None (the default) for adapters with no
    # recurrence. The Writer matches on this FIELD generically — it never reads
    # an adapter's source_type or a skill name.
    lineage_key: Optional[str] = None


# ── shared helpers ──────────────────────────────────────────────────────


def _as_str_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    raise MalformedSourceDoc(
        f"expected a list of strings for dock_goal_refs, got {type(value).__name__}"
    )


def _read_text(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise MalformedSourceDoc(f"unreadable source file {path}: {exc}") from exc


def _load_json_object(path: Path, source_type: str) -> Dict[str, Any]:
    text = _read_text(path)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MalformedSourceDoc(
            f"{source_type} {Path(path).name} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise MalformedSourceDoc(
            f"{source_type} {Path(path).name} is not a JSON object"
        )
    return data


def _require_keys(
    data: Dict[str, Any], required: Tuple[str, ...], source_type: str, name: str
) -> None:
    missing = [k for k in required if k not in data]
    if missing:
        raise MalformedSourceDoc(
            f"{source_type} {name} missing required keys: {missing}"
        )


def _split_frontmatter(text: str) -> Optional[Tuple[str, str]]:
    """Return (frontmatter_str, body_str) if the text opens with a terminated
    ``---`` block, else None."""
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return None
    end = stripped.find("\n---", 3)
    if end == -1:
        return None
    return stripped[3:end], stripped[end + 4:].lstrip("\n")


# ── base adapter ────────────────────────────────────────────────────────


class Adapter:
    """Base Strategy. ``glob`` is the watcher's filename pattern, or None for
    a path-invoked adapter."""

    source_type: str = ""
    glob: Optional[str] = None
    # The fleet sink directory (relative to the hermes home) the watcher walks
    # for this adapter; None for a path-invoked adapter.
    sink_dir: Optional[str] = None
    unvalidated_against_live: bool = False

    def parse(self, path: Path) -> NormalizedDoc:  # pragma: no cover - interface
        raise NotImplementedError

    def lineage_key(self, path: Path) -> Optional[str]:
        """The supersede key for a source at ``path`` — the mesh-primitive
        lineage seam. ``None`` (the default) means this capability has no
        recurrence, so its pages never supersede. ONLY adapters that define
        recurrence override this; the Writer matches the emitted frontmatter
        field generically and never branches on a skill or source_type."""
        return None


# ── four fleet adapters ─────────────────────────────────────────────────


class _JsonFleetAdapter(Adapter):
    """Common shape for the JSON fleet sinks: validate the declared top-level
    keys, carry the raw JSON text to the Writer, extract dock_goal_refs if the
    source declares them."""

    required_keys: Tuple[str, ...] = ()

    def parse(self, path: Path) -> NormalizedDoc:
        path = Path(path)
        data = _load_json_object(path, self.source_type)
        _require_keys(data, self.required_keys, self.source_type, path.name)
        return NormalizedDoc(
            source_type=self.source_type,
            source_path=str(path),
            source_mtime=path.stat().st_mtime,
            dock_goal_refs=_as_str_list(data.get("dock_goal_refs")),
            raw_content=_read_text(path),
            lineage_key=self.lineage_key(path),
        )


class ScoutDigestAdapter(_JsonFleetAdapter):
    source_type = "scout_digest"
    glob = "digest-*.json"
    sink_dir = "scout"
    required_keys = (
        "generated_at",
        "keyword_clusters_searched",
        "opportunities",
        "flagged_for_review",
        "summary",
    )

    def lineage_key(self, path: Path) -> Optional[str]:
        # Replace-by-recurrence: every scout digest shares ONE key, so a new
        # digest supersedes all priors -> exactly one active scout_digest page.
        # The constant source_type is the recurrence slot.
        return self.source_type


class ResearcherBriefAdapter(_JsonFleetAdapter):
    source_type = "researcher_brief"
    glob = "brief-*.json"
    sink_dir = "researcher"
    required_keys = (
        "generated_at",
        "source_article",
        "operator_intent",
        "research",
        "synthesis",
    )

    def lineage_key(self, path: Path) -> Optional[str]:
        # Replace-by-topic-slug: the SLUG from brief-YYYY-MM-DD-SLUG.json. Same
        # slug supersedes; a new slug coexists. Fail loud (A2) if the contract
        # filename shape is violated — no silent fallback that would mis-key the
        # recurrence.
        match = _BRIEF_SLUG_RE.match(Path(path).stem)
        if match is None:
            raise MalformedSourceDoc(
                f"researcher_brief {Path(path).name}: filename does not match "
                "brief-YYYY-MM-DD-SLUG.json; cannot derive lineage slug"
            )
        return match.group(1)


class CultivatorProspectsAdapter(_JsonFleetAdapter):
    source_type = "cultivator_prospects"
    glob = "prospects-*.json"
    sink_dir = "cultivator"
    # No live instance existed at build time — the required keys come from the
    # declared SKILL.md contract. Fail loud if a real file's shape differs.
    unvalidated_against_live = True
    required_keys = (
        "generated_at",
        "input_source",
        "input_detail",
        "prospects",
        "summary",
    )


class DrafterDraftAdapter(Adapter):
    """Markdown + YAML frontmatter. Validate the declared frontmatter keys;
    carry the full file (frontmatter + body) to the Writer."""

    source_type = "drafter_draft"
    glob = "draft-*.md"
    sink_dir = "drafter"
    required_frontmatter = (
        "title",
        "format",
        "source_brief",
        "angle",
        "audience",
        "word_count",
        "status",
        "drafted_at",
    )

    def parse(self, path: Path) -> NormalizedDoc:
        path = Path(path)
        text = _read_text(path)
        split = _split_frontmatter(text)
        if split is None:
            raise MalformedSourceDoc(
                f"{self.source_type} {path.name} has no YAML frontmatter block"
            )
        fm_str, _body = split
        try:
            meta = yaml.safe_load(fm_str)
        except yaml.YAMLError as exc:
            raise MalformedSourceDoc(
                f"{self.source_type} {path.name} has unparseable frontmatter: {exc}"
            ) from exc
        if not isinstance(meta, dict):
            raise MalformedSourceDoc(
                f"{self.source_type} {path.name} frontmatter is not a mapping"
            )
        _require_keys(meta, self.required_frontmatter, self.source_type, path.name)
        if not isinstance(meta.get("title"), str) or not meta["title"].strip():
            raise MalformedSourceDoc(
                f"{self.source_type} {path.name}: 'title' must be a non-empty string"
            )
        return NormalizedDoc(
            source_type=self.source_type,
            source_path=str(path),
            source_mtime=path.stat().st_mtime,
            dock_goal_refs=_as_str_list(meta.get("dock_goal_refs")),
            raw_content=text,
        )


# ── generic package adapter (declaration-fed) ───────────────────────────


class GenericPackageAdapter(Adapter):
    """Declaration-fed package adapter (promoted-artifact-persistence-v1 P2).

    Built at enumeration time from a capability record's
    ``write_zone.ingest: {surface: canonical_subdirs, source_type: ...}``
    declaration — ``sink_dir`` and ``source_type`` are constructor DATA, never
    a producer-named class (GATE-B ruling 2, generality pin extended). Parses
    ONE content file inside a per-unit canonical subdir
    (``<sink>/<unit>/<file>``) as plain text with best-effort optional
    frontmatter (the operator_curated discipline — package content files
    declare no frontmatter contract). One page per file; there is no
    package-level compaction concept.

    ``lineage_key`` is ``"<unit-dir>/<filename>"`` — a re-promoted file
    (P1 last-write-wins overwrite) supersedes ITS OWN prior page; distinct
    units never cross-supersede, and the two files of one package coexist."""

    glob = None  # enumerated by the record-driven walker, never the fleet glob walk

    def __init__(self, *, sink_dir: str, source_type: str):
        if not sink_dir or not source_type:
            raise ValueError(
                "GenericPackageAdapter requires both sink_dir and source_type "
                "from the write_zone.ingest declaration"
            )
        self.sink_dir = sink_dir
        self.source_type = source_type

    def lineage_key(self, path: Path) -> Optional[str]:
        p = Path(path)
        return f"{p.parent.name}/{p.name}"

    def parse(self, path: Path) -> NormalizedDoc:
        path = Path(path)
        text = _read_text(path)
        if not text.strip():
            raise MalformedSourceDoc(
                f"{self.source_type} source {path.name} is empty"
            )
        dock_goal_refs: List[str] = []
        body = text
        split = _split_frontmatter(text)
        if split is not None:
            fm_str, fm_body = split
            try:
                meta = yaml.safe_load(fm_str)
            except yaml.YAMLError:
                logger.debug(
                    "[wiki] %s %s: frontmatter did not parse; treating entire "
                    "file as body (best-effort).", self.source_type, path.name,
                )
            else:
                if isinstance(meta, dict):
                    dock_goal_refs = _as_str_list(meta.get("dock_goal_refs"))
                    body = fm_body
        return NormalizedDoc(
            source_type=self.source_type,
            source_path=str(path),
            source_mtime=path.stat().st_mtime,
            dock_goal_refs=dock_goal_refs,
            raw_content=body,
            lineage_key=self.lineage_key(path),
        )


# ── plain-text adapters (path-invoked + ambient-session) ─────────────────


class _PlainTextAdapter(Adapter):
    """Shared parser for plain ``.md``/``.txt`` sources with best-effort
    optional frontmatter — the operator_curated discipline (fails loud only on
    an empty/unreadable file; unparseable frontmatter falls back to
    whole-file-as-body, logged not swallowed). Subclasses set ``source_type``;
    the parse body reads ``self.source_type`` so each subclass logs honestly
    and its ``NormalizedDoc`` carries its own label. No recurrence
    (``lineage_key`` stays the base None) — these sources never supersede.

    notes-research-ingest-v1: extracted verbatim from OperatorCuratedAdapter
    so the ambient-session adapter shares the EXACT parse behavior with a
    distinct provenance label; operator_curated's NormalizedDoc output is
    byte-identical (only the debug-log wording is now self.source_type)."""

    def parse(self, path: Path) -> NormalizedDoc:
        path = Path(path)
        text = _read_text(path)
        if not text.strip():
            raise MalformedSourceDoc(
                f"{self.source_type} source {path.name} is empty"
            )

        dock_goal_refs: List[str] = []
        body = text
        split = _split_frontmatter(text)
        if split is not None:
            fm_str, fm_body = split
            try:
                meta = yaml.safe_load(fm_str)
            except yaml.YAMLError:
                # SPEC-authorized best-effort: optional frontmatter that doesn't
                # parse is tolerated — treat the whole file as body. Logged, not
                # swallowed silently.
                logger.debug(
                    "[wiki] %s %s: frontmatter did not parse; treating entire "
                    "file as body (best-effort).",
                    self.source_type, path.name,
                )
            else:
                if isinstance(meta, dict):
                    dock_goal_refs = _as_str_list(meta.get("dock_goal_refs"))
                    body = fm_body

        return NormalizedDoc(
            source_type=self.source_type,
            source_path=str(path),
            source_mtime=path.stat().st_mtime,
            dock_goal_refs=dock_goal_refs,
            raw_content=body,
        )


class OperatorCuratedAdapter(_PlainTextAdapter):
    source_type = "operator_curated"
    glob = None  # path-invoked; never glob-walked


class AgentSessionAdapter(_PlainTextAdapter):
    """Attended-session artifacts (notes-research-ingest-v1) — files an agent
    (Mylo) writes to ~/.grove/notes/ and ~/.grove/research/ during an ATTENDED
    session. Distinct provenance from operator_curated: NOT operator-vetted.
    RULING (banked): attended-session artifacts may auto-ingest into ambient
    context with honest provenance — approval happened in-loop at creation;
    unattended-run artifacts always gate. These dirs are attended surfaces by
    definition, so the poll walks them (see grove.wiki.watcher scan-path
    registration). The label renders as its own knowledge-browser category."""

    source_type = "agent_session"
    glob = None  # walked by the watcher's explicit ambient-dir loop, not the fleet glob


# ── registries ──────────────────────────────────────────────────────────

FLEET_ADAPTERS: Tuple[Adapter, ...] = (
    ScoutDigestAdapter(),
    ResearcherBriefAdapter(),
    DrafterDraftAdapter(),
    CultivatorProspectsAdapter(),
)

_OPERATOR_CURATED = OperatorCuratedAdapter()
_AGENT_SESSION = AgentSessionAdapter()

# Keyed by source_type for Phase 4 (pipeline) and Phase 5 (watcher/CLI) dispatch.
ADAPTERS: Dict[str, Adapter] = {
    a.source_type: a for a in (*FLEET_ADAPTERS, _OPERATOR_CURATED, _AGENT_SESSION)
}


def fleet_adapter_for(path) -> Optional[Adapter]:
    """Return the fleet adapter whose glob matches ``path``'s filename, or None.

    Strict, filename-only matching — the watcher uses this to include only
    on-contract files; everything else (off-glob residue, operator_curated
    docs) returns None and is skipped, never errored.
    """
    name = Path(path).name
    for adapter in FLEET_ADAPTERS:
        if adapter.glob and fnmatch.fnmatch(name, adapter.glob):
            return adapter
    return None
