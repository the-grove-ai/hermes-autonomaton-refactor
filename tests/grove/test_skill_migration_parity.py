"""GRV-009 E6a C2 — migrated kind=skill records reproduce the index golden.

Proves the record-driven skill read path is byte-equivalent to the filesystem
scan it will replace in C3:

* INDEX PARITY — projecting the migrated kind=skill records through
  build_skill_index_from_records reproduces the frozen <available_skills> golden
  byte-for-byte (the category/name/description tuples + category_descriptions).
* HELPER FIDELITY — grove.skill_index's replicated frontmatter helpers match the
  agent.skill_utils originals byte-for-byte (the grove layer can't import agent —
  this guard catches drift).
* BODY WRAPPER — every migrated body resolves through the A8 passive-data
  wrapper (the C1 contract holds for all 94 records).
* A4 SECURITY — the green skill records are EXACTLY the operator-signed six; a
  green record off the signed list halts.

The byte-golden was frozen from today's FS scan (bundled ∩ .bundled_manifest, the
two advisory symlink duplicates included). The legacy scan stays authoritative
until C3; this test is the equivalence guard for that swap.
"""

from __future__ import annotations

from pathlib import Path

from grove.capability import Capability, CapabilityKind, Zone
from grove.capability_registry import load_capabilities
from grove.skill_disclosure import (
    SKILL_REFERENCE_CLOSE,
    SKILL_REFERENCE_OPEN,
    load_skill_category_descriptions,
    resolve_skill_record,
)
from grove.skill_index import (
    build_skill_index_from_records,
    extract_index_description,
    parse_skill_frontmatter,
)

_FIX = Path(__file__).parent / "fixtures"
_GOLDEN = _FIX / "skill_index_golden.txt"

# The operator-signed green set (GATE-B2 zone-manifest). The canonical repo
# skills/ (the VM source) carries no jim-voice/linkedin symlink duplicates —
# those were a local ~/.grove artifact — so the conditional "symlink paths IF
# the golden emits them" resolves to no. A4 halts on any green skill record off
# this list. Fleet Phase 1 added scout + researcher as GREEN (drafter +
# cultivator are YELLOW and so are NOT signed here).
# test-baseline-hygiene R-T4: scout-jobsearch signed GREEN — born green in
# scout-jobsearch-v1 (6e6429e0d, "first browser-consuming fleet skill"); its
# capability record declares zone: green by ruled design. forge-jobsearch stays
# YELLOW (like drafter/cultivator) and is therefore NOT signed here.
_SIGNED_GREEN = {
    "skill.content.jim-voice-writing-style",
    "skill.content.linkedin-thinkpiece",
    "skill.creative.songwriting-and-ai-music",
    "skill.upstream-sync-register.upstream-sync-register",
    "skill.fleet.scout",
    "skill.fleet.researcher",
    "skill.fleet.scout-jobsearch",
}


def _skill_records():
    return [r for r in load_capabilities().values() if r.kind is CapabilityKind.SKILL]


# ── index parity ──────────────────────────────────────────────────────────────


def test_record_index_reproduces_golden_byte_for_byte():
    golden = _GOLDEN.read_text(encoding="utf-8")
    cat_desc = load_skill_category_descriptions()
    projected = build_skill_index_from_records(_skill_records(), cat_desc)
    assert projected == golden, "record-driven skill index diverged from the golden"


def test_every_migrated_skill_appears_in_the_index():
    cat_desc = load_skill_category_descriptions()
    projected = build_skill_index_from_records(_skill_records(), cat_desc)
    # one index line per record (canonical repo skills/ has no symlink dups)
    entry_lines = [ln for ln in projected.splitlines() if ln.startswith("    - ")]
    assert len(entry_lines) == len(_skill_records())


def test_live_prompt_builder_is_record_driven_and_reproduces_golden():
    """End-to-end (GRV-009 E6a C3): the LIVE build_skills_system_prompt is now
    record-driven for the bundled set — its <available_skills> block contains the
    frozen golden byte-for-byte. (Local/external skills may add extra lines; the
    bundled golden must be a subset.) This supersedes the C2 FS-vs-record format
    guard, which is moot now that the prompt builder itself projects from records."""
    from agent.prompt_builder import build_skills_system_prompt

    from agent.skill_utils import parse_frontmatter, skill_matches_platform

    live = build_skills_system_prompt(None, None)
    assert "<available_skills>" in live
    live_lines = set(
        live.split("<available_skills>\n", 1)[1]
        .split("\n</available_skills>", 1)[0]
        .splitlines()
    )
    # Platform-aware expectation. The live builder drops skills whose frontmatter
    # ``platforms:`` excludes this host (macOS-only skills — the whole ``apple``
    # category — on Linux) via ``skill_matches_platform`` in
    # ``_bundled_skill_index_from_records``. Reproject the host-admitted records
    # through the SAME ``build_skill_index_from_records`` the byte-golden was
    # frozen from: ``test_record_index_reproduces_golden_byte_for_byte`` proves
    # ``projection(all records) == golden``, so ``projection(admitted subset)`` is
    # the golden filtered to this platform (emptied categories fall away). On
    # macOS every record is admitted, so ``expected == golden`` and this stays
    # byte-identical to the frozen guard; on Linux the macOS-only skills drop
    # instead of registering as false regressions. The golden file is untouched.
    cat_desc = load_skill_category_descriptions()
    admitted = [
        r for r in _skill_records()
        if skill_matches_platform(parse_frontmatter(r.context.payload)[0])
    ]
    expected = build_skill_index_from_records(admitted, cat_desc)
    missing = [ln for ln in expected.splitlines() if ln not in live_lines]
    assert not missing, (
        f"live record-driven index missing host-admitted golden lines: {missing[:5]}"
    )


# ── helper fidelity (grove replica == agent original) ─────────────────────────


def test_frontmatter_helpers_match_agent_originals():
    from agent.skill_utils import extract_skill_description, parse_frontmatter

    for rec in _skill_records():
        payload = rec.context.payload
        assert parse_skill_frontmatter(payload) == parse_frontmatter(payload), rec.id
        fm, _ = parse_frontmatter(payload)
        assert extract_index_description(fm) == extract_skill_description(fm), rec.id


# ── body wrapper (A8 holds for every migrated record) ─────────────────────────


def test_every_migrated_body_resolves_wrapped():
    from grove.skill_disclosure import wrap_skill_body

    for rec in _skill_records():
        out = resolve_skill_record(rec)
        assert out == wrap_skill_body(rec.context.payload)
        assert out.startswith(SKILL_REFERENCE_OPEN)
        assert out.rstrip().endswith(SKILL_REFERENCE_CLOSE)


# ── A4 security — green is exactly the signed six ─────────────────────────────


def test_green_records_are_exactly_the_signed_set():
    green = {r.id for r in _skill_records() if r.zone is Zone.GREEN}
    assert green == _SIGNED_GREEN, (
        f"A4 violation: green skill records != signed manifest. "
        f"unexpected={sorted(green - _SIGNED_GREEN)} "
        f"missing={sorted(_SIGNED_GREEN - green)}"
    )


def test_no_skill_record_has_a_mutation_binding():
    # WRITE-ONCE in E6a: skills govern no tools and carry no toolset/credential
    # binding (no mutation surface until E6b lands the registry mutation guard).
    for rec in _skill_records():
        assert not rec.bindings.tools, rec.id
        assert rec.bindings.toolset_key is None, rec.id
        assert rec.bindings.credentials is None, rec.id
