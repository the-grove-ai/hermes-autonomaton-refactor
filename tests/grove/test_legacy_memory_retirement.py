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
from grove.prompt.composer import (
    PromptComposer,
    _memory_provider,
    _user_profile_provider,
)

_REPO = Path(__file__).resolve().parents[2]
_TS = "2026-06-01T00:00:00+00:00"


# ── M1: config defaults ──────────────────────────────────────────────────


def test_config_defaults_legacy_memory_off():
    from hermes_cli.config import DEFAULT_CONFIG

    mem = DEFAULT_CONFIG["memory"]
    assert mem["memory_enabled"] is False
    assert mem["user_profile_enabled"] is False


# ── M2: prompt.config.yaml section gating ────────────────────────────────


def test_prompt_config_disables_legacy_sections():
    cfg = yaml.safe_load((_REPO / "config" / "prompt.config.yaml").read_text())
    sections = cfg["prompt"]["sections"]
    assert sections["memory"]["enabled"] is False
    assert sections["user_profile"]["enabled"] is False
    # The Grove substrate section is NOT disabled here (it has no entry, so it
    # rides the in-code default) — guard against an accidental future gate.
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


class _FakeLegacyStore:
    """Minimal stand-in for tools.memory_tool.MemoryStore — only the system
    prompt surface the composer providers touch."""

    def format_for_system_prompt(self, target: str):
        return f"LEGACY-{target.upper()}-CONTENT"


def test_legacy_memory_section_none_when_toggle_off():
    ctx = {"memory_store": _FakeLegacyStore(), "memory_enabled": False}
    assert _memory_provider(ctx) is None


def test_legacy_user_profile_section_none_when_toggle_off():
    ctx = {"memory_store": _FakeLegacyStore(), "user_profile_enabled": False}
    assert _user_profile_provider(ctx) is None


def test_legacy_sections_gate_is_the_toggle():
    # Sanity: the ONLY thing suppressing them is the toggle — flip it on and
    # the (legacy) content returns. Proves the retirement is the toggle, and
    # that nothing else is accidentally masking a still-live legacy surface.
    store = _FakeLegacyStore()
    assert _memory_provider({"memory_store": store, "memory_enabled": True}) is not None
    assert _user_profile_provider(
        {"memory_store": store, "user_profile_enabled": True}) is not None


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


def test_composer_suppresses_legacy_keeps_grove(tmp_path):
    """End-to-end: with toggles off, neither legacy section composes, while the
    Grove accumulated_domain_memory section DOES — at context:15, 1000 cap."""
    composer = PromptComposer(config=None)
    composer.register_section("memory", _memory_provider, order=10, tier="volatile")
    composer.register_section(
        "user_profile", _user_profile_provider, order=20, tier="volatile")
    composer.register_section(
        "accumulated_domain_memory",
        create_memory_provider(store=_grove_store(tmp_path), dock_goals_loader=lambda: []),
        order=15, tier="context",
    )

    composed = composer.compose(
        memory_store=_FakeLegacyStore(),
        memory_enabled=False,
        user_profile_enabled=False,
        session_id="s",
        intent_class="conversation",
    )

    assert "memory" not in composed.sections          # legacy MEMORY.md gone
    assert "user_profile" not in composed.sections    # legacy USER.md gone
    assert "LEGACY-" not in composed.text             # no legacy content at all
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
    # Point the (now-removed) legacy path resolver's source at our tmp, to prove
    # it is never consulted.
    import tools.memory_tool as mt
    monkeypatch.setattr(mt, "get_memory_dir", lambda: mem_dir)

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
