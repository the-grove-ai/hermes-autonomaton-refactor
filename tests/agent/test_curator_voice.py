"""GRV-009 soul-aligned-curator-review-v2 — voice preamble + sovereign override.

Voice-only: a static professional advisory preamble shapes the curator review's
PHRASING; an operator-sovereign synced override file (~/.grove/curator-voice.md)
can replace it; the agent never writes it. No governance/merit/identity content.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture
def curator_env(tmp_path, monkeypatch):
    home = tmp_path / ".grove"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("GROVE_HOME", str(home))
    import tools.skill_usage as usage
    importlib.reload(usage)
    import agent.curator as curator
    importlib.reload(curator)
    monkeypatch.setattr(curator, "_load_config", lambda: {})
    return {"home": home, "curator": curator, "usage": usage}


def _write_skill(skills_dir: Path, name: str) -> None:
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: x\n---\n", encoding="utf-8")


# ── voice-only content (proof 5: no governance leak) ──────────────────────────


def test_preamble_is_voice_only_no_governance_or_identity(curator_env):
    c = curator_env["curator"]
    p = c.CURATOR_VOICE_PREAMBLE.lower()
    # voice words present
    assert "register" in p or "advisor" in p or "phrase" in p
    # NO merit/governance/identity/schema language — voice shaping ONLY
    for forbidden in (
        "soul", "goal", "constitution", "merit", "evaluate", "disposition",
        "alignment_score", "zone", "schema", "yaml", "consolidations:",
    ):
        assert forbidden not in p, f"preamble leaks {forbidden!r}"


def test_chatty_guard_lives_in_review_prompt(curator_env):
    c = curator_env["curator"]
    rp = c.CURATOR_REVIEW_PROMPT
    assert "OUTPUT DISCIPLINE" in rp
    assert "EXACTLY the schema" in rp
    # the schema prompt owns format; the voice preamble does not
    assert "shapes the prose summary" in rp.lower() or "phrasing only" in rp.lower()


def test_no_governance_leak_in_schema(curator_env):
    """The output schema carries no disposition/alignment_score field; the record
    payload stays consolidations/prunings only."""
    c = curator_env["curator"]
    rp = c.CURATOR_REVIEW_PROMPT
    assert "consolidations:" in rp and "prunings:" in rp
    assert "disposition" not in rp.lower()
    assert "alignment_score" not in rp.lower()
    # Capability.transition untouched (voice sprint touches no lifecycle code)
    from grove.capability import Capability
    assert hasattr(Capability, "transition")


# ── override precedence (proof 3) ─────────────────────────────────────────────


def test_override_precedence_file_wins(curator_env):
    c = curator_env["curator"]
    (curator_env["home"] / "curator-voice.md").write_text(
        "OPERATOR VOICE OVERRIDE — terse.\n", encoding="utf-8")
    assert c._curator_voice_preamble() == "OPERATOR VOICE OVERRIDE — terse."


def test_override_absent_falls_back_to_constant(curator_env):
    c = curator_env["curator"]
    assert not (curator_env["home"] / "curator-voice.md").exists()
    assert c._curator_voice_preamble() == c.CURATOR_VOICE_PREAMBLE.strip()


def test_override_empty_falls_back_to_constant(curator_env):
    c = curator_env["curator"]
    (curator_env["home"] / "curator-voice.md").write_text("   \n\n", encoding="utf-8")
    assert c._curator_voice_preamble() == c.CURATOR_VOICE_PREAMBLE.strip()


def test_both_empty_is_identity_blind(curator_env, monkeypatch):
    c = curator_env["curator"]
    monkeypatch.setattr(c, "CURATOR_VOICE_PREAMBLE", "")
    assert not (curator_env["home"] / "curator-voice.md").exists()
    assert c._curator_voice_preamble() == ""


# ── both branches + assembly order (proofs 1, 2) ──────────────────────────────


def _capture_prompt(curator_env, monkeypatch, *, dry_run: bool) -> str:
    c = curator_env["curator"]
    u = curator_env["usage"]
    _write_skill(curator_env["home"] / "skills", "a")
    u.mark_agent_created("a")
    captured = {}
    monkeypatch.setattr(c, "_run_llm_review", lambda prompt: captured.setdefault("p", prompt) or {
        "final": "ok", "summary": "ok", "model": "", "provider": "", "tool_calls": [], "error": None,
    })
    c.run_curator_review(synchronous=True, dry_run=dry_run)
    return captured["p"]


def test_live_branch_carries_preamble_in_order(curator_env, monkeypatch):
    c = curator_env["curator"]
    prompt = _capture_prompt(curator_env, monkeypatch, dry_run=False)
    voice = c.CURATOR_VOICE_PREAMBLE.strip()
    assert voice in prompt
    # order: voice -> CURATOR_REVIEW_PROMPT -> candidate_list
    assert prompt.index(voice) < prompt.index("background skill CURATOR")
    assert prompt.index("background skill CURATOR") < prompt.index("Agent-created skills")


def test_dry_run_branch_carries_preamble_after_banner(curator_env, monkeypatch):
    c = curator_env["curator"]
    prompt = _capture_prompt(curator_env, monkeypatch, dry_run=True)
    voice = c.CURATOR_VOICE_PREAMBLE.strip()
    assert "DRY-RUN" in prompt and voice in prompt
    # banner -> voice -> review prompt
    assert prompt.index("DRY-RUN") < prompt.index(voice)
    assert prompt.index(voice) < prompt.index("background skill CURATOR")


def test_empty_voice_yields_no_slot(curator_env, monkeypatch):
    """Graceful degradation: empty voice -> the prompt equals today's identity-
    blind assembly (no leading blank slot)."""
    c = curator_env["curator"]
    monkeypatch.setattr(c, "_curator_voice_preamble", lambda: "")
    prompt = _capture_prompt(curator_env, monkeypatch, dry_run=False)
    assert prompt.startswith("You are running as Hermes' background skill CURATOR")


# ── whitelist (proof 4) ───────────────────────────────────────────────────────


def test_curator_voice_on_sync_whitelist():
    sync = Path(__file__).resolve().parents[2] / "scripts" / "sync-operator.sh"
    text = sync.read_text(encoding="utf-8")
    import re
    m = re.search(r"WHITELIST=\(([^)]*)\)", text)
    assert m and "curator-voice.md" in m.group(1)
    # not added to the never-sync blocklist
    assert "--exclude=curator-voice.md" not in text
