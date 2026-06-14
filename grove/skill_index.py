"""Grove skill-index projection — GRV-009 E6a C2 (skill-migration-v1).

The record→index half of the skill read path: project kind=skill capability
records back into the ``<available_skills>`` index block byte-for-byte, the way
``agent.prompt_builder.build_skills_system_prompt`` renders it from the
filesystem scan. In C2 this PROVES the migrated records reproduce the golden
(the scan stays authoritative); in C3 it BECOMES the index source when the scan
is retired.

Layering: this module lives in the grove (registry) layer and must NOT import
the agent layer — at C3 ``prompt_builder`` (agent) imports THIS, so an upward
import would be circular. The two tiny frontmatter helpers are therefore
replicated here as pure functions; ``test_skill_index_parity`` guards them
against the ``agent.skill_utils`` originals byte-for-byte, so drift fails loud.

Fail-loud: a non-skill record handed to the projection, or a skill record whose
inline body carries no parseable ``name``, raises ``ValueError`` naming it — a
record that can't render its index line would silently vanish from disclosure.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Tuple

import yaml

from grove.capability import Capability, CapabilityKind

__all__ = [
    "parse_skill_frontmatter",
    "extract_index_description",
    "project_skill_records",
    "format_available_skills",
    "build_skill_index_from_records",
]


# ── Frontmatter helpers (replicated from agent.skill_utils; guarded by test) ──


def parse_skill_frontmatter(content: str) -> Tuple[Dict, str]:
    """Mirror of agent.skill_utils.parse_frontmatter (YAML frontmatter split)."""
    frontmatter: Dict = {}
    body = content
    if not content.startswith("---"):
        return frontmatter, body
    end_match = re.search(r"\n---\s*\n", content[3:])
    if not end_match:
        return frontmatter, body
    yaml_content = content[3 : end_match.start() + 3]
    body = content[end_match.end() + 3 :]
    try:
        parsed = yaml.safe_load(yaml_content)
        if isinstance(parsed, dict):
            frontmatter = parsed
    except Exception:
        for line in yaml_content.strip().split("\n"):
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            frontmatter[key.strip()] = value.strip()
    return frontmatter, body


def extract_index_description(frontmatter: Dict) -> str:
    """Mirror of agent.skill_utils.extract_skill_description (the index gloss)."""
    raw_desc = frontmatter.get("description", "")
    if not raw_desc:
        return ""
    desc = str(raw_desc).strip().strip("'\"")
    if len(desc) > 60:
        return desc[:57] + "..."
    return desc


# ── Projection: records → the index's {category: [(name, desc)]} ─────────────


def project_skill_records(
    records: Iterable[Capability],
) -> Dict[str, List[Tuple[str, str]]]:
    """Project kind=skill records into the index grouping the scan produces.

    Each record contributes one ``(frontmatter_name, description)`` entry under
    its ``skill.category`` — name/description are parsed from the inline body
    (``context.payload``) exactly as the filesystem path derives them, so the
    record is the single source of truth and the projection never re-reads disk.
    """
    by_category: Dict[str, List[Tuple[str, str]]] = {}
    for rec in records:
        if rec.kind is not CapabilityKind.SKILL:
            continue
        if rec.skill is None or not rec.skill.category:
            raise ValueError(
                f"skill record {rec.id!r} has no skill.category — cannot place it "
                f"in the index"
            )
        fm, _ = parse_skill_frontmatter(rec.context.payload)
        name = fm.get("name")
        if not name:
            raise ValueError(
                f"skill record {rec.id!r} inline body has no frontmatter 'name' — "
                f"it would render no index line"
            )
        desc = extract_index_description(fm)
        by_category.setdefault(rec.skill.category, []).append((str(name), desc))
    return by_category


# ── Format: the exact <available_skills> block bytes ─────────────────────────
# Reproduces agent.prompt_builder.build_skills_system_prompt's index_lines loop
# verbatim (sorted categories; per-category sorted + deduped skills; the
# "  {cat}: {desc}" / "    - {name}: {desc}" line shapes). The byte-golden guards
# this against the live prompt builder.


def format_available_skills(
    skills_by_category: Dict[str, List[Tuple[str, str]]],
    category_descriptions: Dict[str, str],
) -> str:
    """The newline-joined index lines (the content of <available_skills>)."""
    index_lines: List[str] = []
    for category in sorted(skills_by_category.keys()):
        cat_desc = category_descriptions.get(category, "")
        if cat_desc:
            index_lines.append(f"  {category}: {cat_desc}")
        else:
            index_lines.append(f"  {category}:")
        seen = set()
        for name, desc in sorted(skills_by_category[category], key=lambda x: x[0]):
            if name in seen:
                continue
            seen.add(name)
            if desc:
                index_lines.append(f"    - {name}: {desc}")
            else:
                index_lines.append(f"    - {name}")
    return "\n".join(index_lines)


def build_skill_index_from_records(
    records: Iterable[Capability],
    category_descriptions: Dict[str, str],
) -> str:
    """Record-driven <available_skills> block — the C2 parity target / C3 source."""
    return format_available_skills(
        project_skill_records(records), category_descriptions
    )
