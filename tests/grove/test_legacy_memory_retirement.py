"""legacy-memory-retirement-v1 — the Grove substrate is the sole memory voice.

The upstream hermes MEMORY.md/USER.md store (tools/memory_tool.py) is retired:
- config defaults memory_enabled/user_profile_enabled to False (M1)
- prompt.config.yaml disables the memory/user_profile sections (M2)
- the Grove accumulated_domain_memory cap rises 500 -> 1000 (M3)
- identity.py drops its ungated MEMORY.md reader (M5)

These tests assert ZERO legacy memory content composes when the toggles are
off (both the volatile sections AND the identity block), that the Grove
substrate composes regardless of legacy toggle state, and the new 1000 cap.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import grove.identity as gid
from grove.identity import _IDENTITY_FILES, _resolve_raw, load_identity
from grove.memory.events import MemoryCreated
from grove.memory.provider import _DEFAULT_TOKEN_BUDGET, create_memory_provider
from grove.memory.store import MemoryStore
from grove.prompt.composer import PromptComposer, _DEFAULT_SECTIONS

_REPO = Path(__file__).resolve().parents[2]
_TS = "2026-06-01T00:00:00+00:00"


# ── M1: config defaults ──────────────────────────────────────────────────


def test_config_defaults_legacy_memory_off():
    from hermes_cli.config import DEFAULT_CONFIG

    mem = DEFAULT_CONFIG["memory"]
    assert mem["memory_enabled"] is False
    assert mem["user_profile_enabled"] is False


# ── M2: prompt.config.yaml section gating ────────────────────────────────


def test_prompt_config_removes_legacy_sections():
    # legacy-memory-tool-retirement-v1: the memory + user_profile sections are
    # fully REMOVED (not merely disabled).
    cfg = yaml.safe_load((_REPO / "config" / "prompt.config.yaml").read_text())
    sections = cfg["prompt"]["sections"]
    assert "memory" not in sections
    assert "user_profile" not in sections
    # The Grove substrate section rides the in-code default (no gating entry).
    assert "accumulated_domain_memory" not in sections or \
        sections["accumulated_domain_memory"].get("enabled", True) is not False


# ── M3: Grove cap bumped 500 -> 1000 ─────────────────────────────────────


def test_default_token_budget_is_1000():
    assert _DEFAULT_TOKEN_BUDGET == 1000


def test_provider_default_budget_caps_at_1000(tmp_path):
    store = MemoryStore(base_dir=tmp_path)
    # Each line ~100 tokens (~378 chars + framing). At 500: ~5 fit; at 1000: ~10.
    for i in range(12):
        store.append_event(MemoryCreated(
            event_id=f"evt_{i}", timestamp=_TS, record_id=f"mem_{i}",
            entity_type="DomainFact", content="x" * 378, confidence=0.9,
            dock_goal_ref=None, sources=[], supersedes=None,
        ))
    store.rebuild_index()
    provider = create_memory_provider(store=store, dock_goals_loader=lambda: [])
    result = provider({"session_id": "s", "intent_class": "conversation"})
    served = [ln for ln in result.text.splitlines() if ln.startswith("- ")]
    # Strictly more than the old 500-budget count (5); ~10 at the 1000 budget.
    assert len(served) > 5
    assert len(served) >= 9


# ── Legacy sections suppressed when toggles off ──────────────────────────


def test_legacy_providers_fully_removed():
    # legacy-memory-tool-retirement-v1: the _memory_provider / _user_profile_provider
    # functions are DELETED (not merely toggled off), and no default section
    # references them.
    import grove.prompt.composer as composer_mod
    assert not hasattr(composer_mod, "_memory_provider")
    assert not hasattr(composer_mod, "_user_profile_provider")
    section_names = [name for name, *_ in _DEFAULT_SECTIONS]
    assert "memory" not in section_names
    assert "user_profile" not in section_names


# ── Full composer integration: legacy off, Grove on ──────────────────────


def _grove_store(tmp_path):
    store = MemoryStore(base_dir=tmp_path)
    store.append_event(MemoryCreated(
        event_id="evt_g", timestamp=_TS, record_id="mem_g",
        entity_type="DomainFact", content="Grove substrate fact.",
        confidence=0.95, dock_goal_ref=None, sources=[], supersedes=None,
    ))
    store.rebuild_index()
    return store


def test_composer_keeps_grove_no_legacy(tmp_path):
    """End-to-end: no legacy section composes (they are removed), while the Grove
    accumulated_domain_memory section DOES — at context:15, 1000 cap."""
    composer = PromptComposer(config=None)
    composer.register_section(
        "accumulated_domain_memory",
        create_memory_provider(store=_grove_store(tmp_path), dock_goals_loader=lambda: []),
        order=15, tier="context",
    )

    composed = composer.compose(
        session_id="s",
        intent_class="conversation",
    )

    assert "memory" not in composed.sections          # legacy MEMORY.md gone
    assert "user_profile" not in composed.sections    # legacy USER.md gone
    assert "accumulated_domain_memory" in composed.sections   # Grove composes
    assert "Grove substrate fact." in composed.text


def test_grove_provider_ignores_legacy_toggle(tmp_path):
    """The Grove substrate is unaffected by the legacy toggle state — its
    provider does not consult memory_enabled/user_profile_enabled at all."""
    provider = create_memory_provider(
        store=_grove_store(tmp_path), dock_goals_loader=lambda: [])
    for toggles in ({"memory_enabled": False, "user_profile_enabled": False},
                    {"memory_enabled": True, "user_profile_enabled": True},
                    {}):
        ctx = {"session_id": "s", "intent_class": "conversation", **toggles}
        result = provider(ctx)
        assert result is not None
        assert "Grove substrate fact." in result.text


# ── M5: identity no longer reads the legacy MEMORY.md ────────────────────


def test_identity_files_has_no_memory_row():
    canonicals = [row[0] for row in _IDENTITY_FILES]
    assert "memory.md" not in canonicals
    legacies = [row[1] for row in _IDENTITY_FILES]
    assert "MEMORY.md" not in legacies


def test_resolve_raw_no_longer_reads_legacy_memory(tmp_path, monkeypatch):
    """_resolve_raw('memory.md') used to read get_memory_dir()/MEMORY.md. With
    the special-case removed it falls through to home/memory.md (absent) → None,
    even when a legacy memories/MEMORY.md sits on disk."""
    home = tmp_path / "grove_home"
    home.mkdir()
    mem_dir = home / "memories"
    mem_dir.mkdir()
    (mem_dir / "MEMORY.md").write_text("LEGACY-IDENTITY-SENTINEL", encoding="utf-8")
    # tools.memory_tool.get_memory_dir is fully removed (legacy-memory-tool-
    # retirement-v1); the legacy memories/MEMORY.md on disk must simply never be
    # consulted by the resolver.

    result = _resolve_raw(home, "memory.md", "MEMORY.md", None, tmp_path / "ref")
    assert result is None


def test_load_identity_does_not_surface_legacy_memory(tmp_path, monkeypatch):
    """Full composition: a populated memories/MEMORY.md must NOT appear in the
    identity block, and composition.memory stays None."""
    home = tmp_path / "grove_home"
    monkeypatch.setattr(gid, "get_hermes_home", lambda: home)
    # Seed the legacy file the retired reader used to pull in.
    mem_dir = home / "memories"
    mem_dir.mkdir(parents=True)
    (mem_dir / "MEMORY.md").write_text("LEGACY-IDENTITY-SENTINEL", encoding="utf-8")

    comp = load_identity()  # seeds constitution/soul/operator from real ref dir

    assert comp.memory is None
    assert "LEGACY-IDENTITY-SENTINEL" not in comp.compose()
