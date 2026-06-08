"""Tests for grove.identity — Atlas-pattern layered identity composition.

Covers the Sprint 07 contract: tiered failure (constitution/soul hard-fail;
operator/goals/memory graceful; agents silent), composition order, soul.md
frontmatter parsing, first-run seeding, the persona parameter, and the
backward-compatible legacy-filename fallbacks.

Every test uses tmp_path — no test touches the real ~/.grove/.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from grove import identity as gid
from grove.identity import IdentityComposition, IdentityError, load_identity


# ----- minimal valid file bodies ---------------------------------------------

_CONSTITUTION = "# Constitution\n\nThe operator controls the system.\n"
_SOUL_WITH_FM = (
    "---\n"
    "name: test-autonomaton\n"
    "register: strategic-concise\n"
    "declared_identity: \"test partner\"\n"
    "---\n"
    "# Soul\n\nStrategic, concise, direct.\n"
)
_SOUL_NO_FM = "# Soul\n\nStrategic, concise, direct.\n"
_OPERATOR = "# Operator\n\nThe operator is a test fixture.\n"
_GOALS = "# Goals\n\nShip Sprint 07.\n"


def _make_ref_dir(tmp_path: Path, files: dict[str, str]) -> Path:
    """Build a fake config/identity/ containing only the given files."""
    ref = tmp_path / "ref_identity"
    ref.mkdir(exist_ok=True)
    for name, content in files.items():
        (ref / name).write_text(content, encoding="utf-8")
    return ref


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty ~/.grove/ pointed at tmp; reference dir = the real config/identity/."""
    home = tmp_path / "grove_home"
    monkeypatch.setattr(gid, "get_hermes_home", lambda: home)
    return home


# ----- first-run seeding -----------------------------------------------------

def test_first_run_seeds_templated_files(fake_home: Path) -> None:
    """A fresh ~/.grove/ gets constitution / soul / operator seeded from the
    real config/identity/ reference templates. Goals are NOT a file anymore
    (Sprint 69): they render from the Dock, so with no dock.yaml present
    comp.goals is None and no goals.md is seeded."""
    comp = load_identity()
    assert comp.constitution is not None
    assert comp.soul is not None
    assert comp.operator is not None
    assert comp.goals is None  # no Dock in the test home → goals layer omitted
    for name in ("constitution.md", "soul.md", "operator.md"):
        assert (fake_home / name).exists(), f"{name} not seeded"
    assert not (fake_home / "goals.md").exists()  # retired in Sprint 69


def test_goals_layer_renders_from_dock(
    fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sprint 69: the goals layer renders active goals from the Dock."""
    import grove.dock as gdock
    dock_yaml = tmp_path / "dock.yaml"
    dock_yaml.write_text(
        "version: 1\n"
        "goals:\n"
        "  - id: g1\n"
        "    name: \"Test Goal\"\n"
        "    vector: strategic\n"
        "    status: cruising\n"
        "    definition_of_done: \"it is done\"\n"
        "    context_sources: []\n"
        "    keywords: []\n"
        "    unlocked_skills: []\n",
        encoding="utf-8",
    )
    real = gdock.load_dock(path=dock_yaml)
    monkeypatch.setattr(gdock, "load_dock", lambda path=None: real)
    comp = load_identity()
    assert comp.goals is not None
    assert "Test Goal" in comp.goals
    assert "it is done" in comp.goals


def test_goals_layer_fails_loud_on_malformed_dock(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed dock.yaml is NOT swallowed — it fails loud through
    identity composition (Architectural Prime Directive)."""
    import grove.dock as gdock

    def _boom(path=None):
        raise ValueError("dock.yaml is malformed")

    monkeypatch.setattr(gdock, "load_dock", _boom)
    with pytest.raises(ValueError, match="malformed"):
        load_identity()


def test_first_run_does_not_seed_memory_or_agents(fake_home: Path) -> None:
    """memory.md / agents.md have no reference template — they are not seeded."""
    comp = load_identity()
    assert comp.memory is None
    assert comp.agents is None
    assert not (fake_home / "memory.md").exists()
    assert not (fake_home / "agents.md").exists()


# ----- tiered failure: Jidoka hard-fail --------------------------------------

def test_missing_constitution_hard_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """constitution.md missing AND unseedable → IdentityError (Jidoka-tier)."""
    home = tmp_path / "grove_home"
    empty_ref = tmp_path / "empty_ref"
    empty_ref.mkdir()
    monkeypatch.setattr(gid, "get_hermes_home", lambda: home)
    monkeypatch.setattr(gid, "_reference_dir", lambda: empty_ref)
    with pytest.raises(IdentityError, match="constitution.md"):
        load_identity()


def test_missing_soul_hard_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """soul.md missing AND unseedable → IdentityError, even when constitution
    resolves fine."""
    home = tmp_path / "grove_home"
    ref = _make_ref_dir(tmp_path, {"constitution.md": _CONSTITUTION})
    monkeypatch.setattr(gid, "get_hermes_home", lambda: home)
    monkeypatch.setattr(gid, "_reference_dir", lambda: ref)
    with pytest.raises(IdentityError, match="soul.md"):
        load_identity()


def test_empty_constitution_hard_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A constitution.md that exists but is empty also hard-fails — an empty
    constitution is no constitution."""
    home = tmp_path / "grove_home"
    home.mkdir()
    (home / "constitution.md").write_text("   \n", encoding="utf-8")
    (home / "soul.md").write_text(_SOUL_NO_FM, encoding="utf-8")
    monkeypatch.setattr(gid, "get_hermes_home", lambda: home)
    with pytest.raises(IdentityError, match="constitution.md"):
        load_identity()


# ----- tiered failure: graceful + silent -------------------------------------

def test_missing_operator_warns_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """operator.md missing AND unseedable → warning logged, composition
    continues (graceful-tier)."""
    home = tmp_path / "grove_home"
    # Reference dir has the Jidoka files but NOT operator/goals.
    ref = _make_ref_dir(
        tmp_path, {"constitution.md": _CONSTITUTION, "soul.md": _SOUL_NO_FM}
    )
    monkeypatch.setattr(gid, "get_hermes_home", lambda: home)
    monkeypatch.setattr(gid, "_reference_dir", lambda: ref)
    with caplog.at_level(logging.WARNING, logger="grove.identity"):
        comp = load_identity()
    assert comp.operator is None
    assert comp.goals is None
    assert comp.constitution is not None and comp.soul is not None
    assert any("operator.md" in r.getMessage() for r in caplog.records)


def test_missing_agents_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """agents.md missing → silent skip, no warning."""
    home = tmp_path / "grove_home"
    ref = _make_ref_dir(
        tmp_path, {"constitution.md": _CONSTITUTION, "soul.md": _SOUL_NO_FM}
    )
    monkeypatch.setattr(gid, "get_hermes_home", lambda: home)
    monkeypatch.setattr(gid, "_reference_dir", lambda: ref)
    with caplog.at_level(logging.WARNING, logger="grove.identity"):
        comp = load_identity()
    assert comp.agents is None
    assert not any("agents.md" in r.getMessage() for r in caplog.records)


# ----- composition order -----------------------------------------------------

def test_compose_stable_order_constitution_then_soul(fake_home: Path) -> None:
    composed = load_identity().compose_stable()
    assert "# Constitution" in composed
    assert "# Soul" in composed
    assert composed.index("# Constitution") < composed.index("# Soul")


def test_compose_full_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """compose() assembles all six in D4 order, skipping absent layers."""
    comp = IdentityComposition(
        constitution="C", soul="S", operator="O",
        goals="G", memory="M", agents="A",
    )
    assert comp.compose() == "C\n\nS\n\nO\n\nG\n\nM\n\nA"


def test_compose_skips_absent_layers() -> None:
    comp = IdentityComposition(constitution="C", soul="S", operator=None,
                               goals=None, memory="M", agents=None)
    assert comp.compose() == "C\n\nS\n\nM"


# ----- frontmatter -----------------------------------------------------------

def test_soul_frontmatter_parsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "grove_home"
    home.mkdir()
    (home / "constitution.md").write_text(_CONSTITUTION, encoding="utf-8")
    (home / "soul.md").write_text(_SOUL_WITH_FM, encoding="utf-8")
    monkeypatch.setattr(gid, "get_hermes_home", lambda: home)
    comp = load_identity()
    assert comp.frontmatter.get("name") == "test-autonomaton"
    assert comp.frontmatter.get("register") == "strategic-concise"


def test_soul_without_frontmatter_yields_empty_dict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "grove_home"
    home.mkdir()
    (home / "constitution.md").write_text(_CONSTITUTION, encoding="utf-8")
    (home / "soul.md").write_text(_SOUL_NO_FM, encoding="utf-8")
    monkeypatch.setattr(gid, "get_hermes_home", lambda: home)
    comp = load_identity()
    assert comp.frontmatter == {}
    assert comp.soul is not None  # prose body still loaded


# ----- PL-2: frontmatter must not leak into composed output ------------------

def test_compose_stable_strips_soul_frontmatter() -> None:
    """compose_stable() must not emit soul.md's YAML frontmatter as prose.
    The frontmatter is parsed into .frontmatter; emitting it again in the
    composed system prompt is PL-2."""
    comp = IdentityComposition(constitution="# Constitution", soul=_SOUL_WITH_FM)
    composed = comp.compose_stable()
    assert "---" not in composed
    assert "name: test-autonomaton" not in composed
    assert "register: strategic-concise" not in composed
    # the soul prose body survives the strip
    assert "# Soul" in composed
    assert "Strategic, concise, direct." in composed


def test_compose_strips_soul_frontmatter() -> None:
    """compose() strips soul frontmatter on the same terms as compose_stable()."""
    comp = IdentityComposition(constitution="# Constitution", soul=_SOUL_WITH_FM)
    composed = comp.compose()
    assert "---" not in composed
    assert "declared_identity" not in composed
    assert "# Soul" in composed


def test_compose_stable_soul_without_frontmatter_is_unchanged() -> None:
    """A soul with no frontmatter passes through compose_stable() intact —
    the strip is a no-op when there is nothing to strip."""
    comp = IdentityComposition(constitution="# Constitution", soul=_SOUL_NO_FM)
    composed = comp.compose_stable()
    assert "# Soul" in composed
    assert "Strategic, concise, direct." in composed


# ----- persona parameter -----------------------------------------------------

def test_persona_none_is_flat(fake_home: Path) -> None:
    """persona=None composes from flat ~/.grove/ files — the v0.1 path."""
    comp = load_identity(persona=None)
    assert isinstance(comp, IdentityComposition)


def test_persona_string_raises_not_implemented(fake_home: Path) -> None:
    """A persona string is the v0.1.5 multi-persona path — not implemented."""
    with pytest.raises(NotImplementedError, match="v0.1.5"):
        load_identity(persona="research-analyst")


# ----- backward compatibility ------------------------------------------------

def test_legacy_soul_md_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An install with old SOUL.md (not soul.md) uses its content — no forced
    migration, no seed of a fresh template over the operator's real identity.

    Note: on a case-insensitive filesystem (macOS default) ``soul.md`` and
    ``SOUL.md`` are the same path, so this test asserts on content, not on
    filename casing — the content check is the real backward-compat proof.
    """
    home = tmp_path / "grove_home"
    home.mkdir()
    (home / "constitution.md").write_text(_CONSTITUTION, encoding="utf-8")
    (home / "SOUL.md").write_text("# Legacy Soul\n\nOld identity.\n", encoding="utf-8")
    monkeypatch.setattr(gid, "get_hermes_home", lambda: home)
    comp = load_identity()
    assert comp.soul is not None
    # The operator's real legacy content is used — NOT the fresh reference
    # template (which would say "This file is who the Autonomaton IS...").
    assert "Legacy Soul" in comp.soul
    assert "Old identity." in comp.soul


def test_legacy_user_md_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "grove_home"
    home.mkdir()
    (home / "constitution.md").write_text(_CONSTITUTION, encoding="utf-8")
    (home / "soul.md").write_text(_SOUL_NO_FM, encoding="utf-8")
    (home / "USER.md").write_text("# Legacy User\n\nOld operator.\n", encoding="utf-8")
    monkeypatch.setattr(gid, "get_hermes_home", lambda: home)
    comp = load_identity()
    assert comp.operator is not None
    assert "Legacy User" in comp.operator


def test_legacy_memory_and_agents_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "grove_home"
    home.mkdir()
    (home / "constitution.md").write_text(_CONSTITUTION, encoding="utf-8")
    (home / "soul.md").write_text(_SOUL_NO_FM, encoding="utf-8")
    (home / "MEMORY.md").write_text("# Legacy Memory\n", encoding="utf-8")
    (home / "AGENTS.md").write_text("# Legacy Agents\n", encoding="utf-8")
    monkeypatch.setattr(gid, "get_hermes_home", lambda: home)
    comp = load_identity()
    assert comp.memory is not None and "Legacy Memory" in comp.memory
    assert comp.agents is not None and "Legacy Agents" in comp.agents


# ── Sprint 75 Phase 1: tier-aware identity composition ───────────────────

def _full_comp():
    """An IdentityComposition with every layer set to an identifiable marker."""
    return IdentityComposition(
        constitution="CONSTITUTION_X",
        soul="SOUL_X",
        operator="OPERATOR_X",
        goals="DOCKGOALS_X",
        register_overlay="REGISTER_X",
        affordances="AFFORDANCES_X",
        capabilities="CAPABILITIES_X",
    )


def test_compose_stable_t1_is_irreducible():
    # Refinement 1: operator rides T1 too (as the condensed stub); only the
    # heavy self-model layers (affordances/capabilities) are gated off.
    out = _full_comp().compose_stable("T1")
    for must in ("CONSTITUTION_X", "SOUL_X", "REGISTER_X", "OPERATOR_X", "DOCKGOALS_X"):
        assert must in out
    for excluded in ("AFFORDANCES_X", "CAPABILITIES_X"):
        assert excluded not in out


def test_compose_stable_t1_order():
    out = _full_comp().compose_stable("T1")
    assert (
        out.index("CONSTITUTION_X")
        < out.index("SOUL_X")
        < out.index("REGISTER_X")
        < out.index("OPERATOR_X")
        < out.index("DOCKGOALS_X")
    )


def test_compose_stable_t2_adds_operator_and_capabilities():
    out = _full_comp().compose_stable("T2")
    for must in ("CONSTITUTION_X", "SOUL_X", "REGISTER_X", "DOCKGOALS_X",
                 "OPERATOR_X", "CAPABILITIES_X"):
        assert must in out
    assert "AFFORDANCES_X" not in out          # affordances is T3-only


def test_compose_stable_t3_is_full():
    out = _full_comp().compose_stable("T3")
    for must in ("CONSTITUTION_X", "SOUL_X", "REGISTER_X", "AFFORDANCES_X",
                 "CAPABILITIES_X", "OPERATOR_X", "DOCKGOALS_X"):
        assert must in out


def test_compose_stable_no_tier_is_full_legacy():
    c = _full_comp()
    assert c.compose_stable() == c.compose_stable(None)
    assert c.compose_stable() == c.compose_stable("T3")   # full == T3 layer set


def test_compose_stable_unknown_tier_defaults_full():
    # An unrecognized tier must not silently drop character — default to full.
    out = _full_comp().compose_stable("T9")
    for must in ("CONSTITUTION_X", "SOUL_X", "AFFORDANCES_X", "OPERATOR_X"):
        assert must in out


def test_load_identity_t1_nulls_gated_layers_and_skips_introspection(
    fake_home, monkeypatch
):
    # On T1, load skips the expensive capability introspection and nulls the
    # gated layers so the composition reflects the tier actually sent.
    import grove.affordances as aff
    sentinel = {"called": False}

    def _boom():
        sentinel["called"] = True
        return "CAPS"

    monkeypatch.setattr(aff, "introspect_capabilities", _boom)
    comp = load_identity(tier="T1")
    assert comp.constitution and comp.soul          # always-on survive
    assert comp.capabilities is None                # gated on T1
    assert comp.affordances is None                 # gated on T1
    assert sentinel["called"] is False              # introspection skipped (cost)
    # Refinement 1: operator rides T1 as the condensed stub (the marked region
    # of the seeded operator.md), NOT the full file.
    assert comp.operator is not None                # the stub is present
    assert "How I Work" in comp.operator            # the marked region
    assert "Who I Am" not in comp.operator          # full-file bio is NOT on T1


def test_load_identity_t3_loads_everything(fake_home, monkeypatch):
    import grove.affordances as aff
    monkeypatch.setattr(aff, "introspect_capabilities", lambda: "CAPS_LIVE")
    comp = load_identity(tier="T3")
    assert comp.capabilities == "CAPS_LIVE"
    assert comp.affordances is not None
    # T3 reads the FULL operator file (markers stripped, bio included).
    assert comp.operator is not None
    assert "Who I Am" in comp.operator
    assert "t1:start" not in comp.operator          # markers never leak


def test_operator_stub_extraction_helpers():
    from grove.identity import _extract_t1_stub, _strip_t1_markers
    text = (
        "# Operator\n\n<!-- t1:start -->\n## How I Work\nTerse by default.\n"
        "<!-- t1:end -->\n\n## Who I Am\nJim Calhoun.\n"
    )
    stub = _extract_t1_stub(text)
    assert "How I Work" in stub and "Terse by default" in stub
    assert "Who I Am" not in stub and "Jim Calhoun" not in stub
    full = _strip_t1_markers(text)
    assert "How I Work" in full and "Who I Am" in full       # T2/T3 read all
    assert "<!--" not in full and "t1:start" not in full     # markers stripped


def test_operator_stub_none_when_no_markers():
    from grove.identity import _extract_t1_stub
    assert _extract_t1_stub("# Operator\n\nNo marked region here.") is None
