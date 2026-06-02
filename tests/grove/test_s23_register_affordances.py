"""Tests for Sprint 23 (soul-affordances-register-v1).

Covers:
    * grove.register — canon presence, list/load precedence, soul
      frontmatter validation, the D8 backward-compat synonym table,
      and the Jidoka-tier unknown-name raise.
    * grove.affordances — operator/reference precedence, the Jidoka-
      tier reference-template check, introspect_capabilities prose
      structure, the secrets-discipline of _enumerate_mcps, and the
      defensive-helper asymmetry against governance failures.
    * grove.identity Sprint 23 extensions — register/affordances/
      capabilities layers in compose_stable, the new D5 order, the
      session_register override path, backward compat for soul.md
      without a register field, and the D6 explicit-pass precedence.
    * AIAgent.set_session_register — attribute mutation + cache
      invalidation contract documented in the setter docstring.
    * HermesCLI._handle_register_command — all four forms (bare,
      list, reset, name) plus invalid-name rejection and the D8
      synonym mapping through the verb surface.

Every test uses tmp_path; no test touches the real ~/.grove/.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import pytest

import cli
import hermes_constants
from grove import affordances as gaff
from grove import identity as gid
from grove import register as greg
from grove.identity import IdentityComposition, IdentityError, load_identity



# Sprint 53 — module-level Dispatcher-style registry for tests.
from tools.registry import ToolRegistry as _Sprint53_TR_top, register_builtin_tools as _Sprint53_RBT_top
_REGISTRY = _Sprint53_TR_top()
_Sprint53_RBT_top(_REGISTRY)

# ── canonical content fixtures ──────────────────────────────────────────────

_CONSTITUTION = "# Constitution\n\nThe operator controls the system.\n"
_AFFORDANCES = "# Affordances\n\nCapability landscape for tests.\n"
_STANDARDS = (
    "# Standards Register\n\n"
    "Broadcasts and bicameral nodes — no villain in plumbing.\n"
)
_OPERATOR_REG = (
    "# Operator Register\n\n"
    "Direct-exchange discipline. Terse executor mode.\n"
)
_EDITORIAL_REG = (
    "# Editorial Register\n\n"
    "Ledger entries: preserved commitment / canonical citation / "
    "structural consequence.\n"
)
_SOUL_OPERATOR = (
    "---\nname: test\nregister: operator\n---\n"
    "# Soul\n\nDirect.\n"
)
_SOUL_NO_REGISTER = "---\nname: test\n---\n# Soul\n\nDirect.\n"
_SOUL_LEGACY = (
    "---\nname: test\nregister: strategic-concise\n---\n"
    "# Soul\n\nDirect.\n"
)
_SOUL_UNKNOWN = (
    "---\nname: test\nregister: bogus-register-name\n---\n"
    "# Soul\n\nDirect.\n"
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ── shared fixture ──────────────────────────────────────────────────────────

@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Isolated identity env: empty ~/.grove/ + fake reference templates.

    Patches three resolution functions so no test reads the real repo:
        * grove.identity.get_hermes_home + _reference_dir
        * grove.register._reference_registers_dir
        * grove.affordances._reference_dir

    Returns a dict with `home`, `ref_id`, `ref_regs` paths for the test
    to write fixture files into.
    """
    home = tmp_path / "grove_home"
    home.mkdir()

    ref_id = tmp_path / "ref_identity"
    ref_id.mkdir()
    _write(ref_id / "constitution.md", _CONSTITUTION)
    _write(ref_id / "soul.md", _SOUL_OPERATOR)
    _write(ref_id / "affordances.md", _AFFORDANCES)

    ref_regs = ref_id / "registers"
    ref_regs.mkdir()
    _write(ref_regs / "standards.md", _STANDARDS)
    _write(ref_regs / "operator.md", _OPERATOR_REG)
    _write(ref_regs / "editorial.md", _EDITORIAL_REG)

    monkeypatch.setattr(gid, "get_hermes_home", lambda: home)
    monkeypatch.setattr(gid, "_reference_dir", lambda: ref_id)
    monkeypatch.setattr(greg, "_reference_registers_dir", lambda: ref_regs)
    monkeypatch.setattr(gaff, "_reference_dir", lambda: ref_id)
    # The /register verb imports get_hermes_home from hermes_constants
    # inside the handler; patch at the source so the local import resolves
    # to the fake home.
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: home)

    # Reset the process-scoped synonym log so each test gets a clean view.
    greg._synonym_logged.clear()

    return {"home": home, "ref_id": ref_id, "ref_regs": ref_regs}


# ════════════════════════════════════════════════════════════════════════════
# grove.register
# ════════════════════════════════════════════════════════════════════════════

# --- validate_canon_present ------------------------------------------------

def test_canon_present_passes_when_standards_template_exists(env: dict) -> None:
    """The fixture seeds standards.md — canon check is a no-op."""
    greg.validate_canon_present()


def test_canon_present_raises_when_standards_template_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D4 Jidoka — missing canon template fails install-time check."""
    empty = tmp_path / "empty_regs"
    empty.mkdir()
    monkeypatch.setattr(greg, "_reference_registers_dir", lambda: empty)
    with pytest.raises(IdentityError, match="Standards Register reference"):
        greg.validate_canon_present()


# --- list_registers --------------------------------------------------------

def test_list_registers_returns_canon_trio(env: dict) -> None:
    assert greg.list_registers(env["home"]) == ["editorial", "operator", "standards"]


def test_list_registers_includes_operator_additions(env: dict) -> None:
    """An operator-only register (no reference template) appears in the list."""
    op_dir = env["home"] / "registers"
    _write(op_dir / "custom.md", "# Custom\n\nOperator-defined.\n")
    assert "custom" in greg.list_registers(env["home"])


def test_list_registers_dedupes_overlap(env: dict) -> None:
    """A register present in BOTH operator and reference dirs appears once."""
    op_dir = env["home"] / "registers"
    _write(op_dir / "operator.md", "# Operator (operator override)\n")
    names = greg.list_registers(env["home"])
    assert names.count("operator") == 1


# --- load_register ---------------------------------------------------------

def test_load_register_operator_copy_wins(env: dict) -> None:
    """D4 precedence: operator copy at ~/.grove/registers/X.md beats template."""
    _write(env["home"] / "registers" / "operator.md", "# Operator (custom)\n")
    content = greg.load_register("operator", env["home"])
    assert "(custom)" in content


def test_load_register_falls_back_to_reference(env: dict) -> None:
    """No operator copy → reference template content returned."""
    assert "# Operator Register" in greg.load_register("operator", env["home"])


def test_load_register_unknown_raises(env: dict) -> None:
    """D4 Jidoka — unknown name resolves to nothing → IdentityError."""
    with pytest.raises(IdentityError, match="resolves to no file"):
        greg.load_register("nonexistent", env["home"])


# --- validate_soul_register (the heart of D4 + D8) -------------------------

def test_validate_soul_register_none_returns_none(env: dict) -> None:
    """Soul omits the field entirely → graceful (no register layer)."""
    assert greg.validate_soul_register(None, env["home"]) is None


def test_validate_soul_register_empty_returns_none(env: dict) -> None:
    assert greg.validate_soul_register("", env["home"]) is None


def test_validate_soul_register_whitespace_returns_none(env: dict) -> None:
    assert greg.validate_soul_register("   ", env["home"]) is None


def test_validate_soul_register_canonical_round_trips(env: dict) -> None:
    """Canonical names pass through unchanged."""
    for name in ("standards", "operator", "editorial"):
        assert greg.validate_soul_register(name, env["home"]) == name


def test_validate_soul_register_synonym_maps_strategic_concise(env: dict) -> None:
    """GATE-A explicit: the one D8 synonym entry must resolve."""
    assert greg.validate_soul_register("strategic-concise", env["home"]) == "operator"


def test_validate_soul_register_unknown_raises_not_silently_mapped(
    env: dict,
) -> None:
    """GATE-A explicit: ANY OTHER unknown value raises — the synonym table
    is NOT a fallback for unknowns."""
    with pytest.raises(IdentityError, match="resolves to no template"):
        greg.validate_soul_register("not-a-real-register", env["home"])


def test_validate_soul_register_synonym_to_operator_then_fallback_fails(
    env: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the canonical target of a synonym itself becomes unresolvable,
    the synonym still raises — silent degradation is the explicit anti-pattern."""
    # Remove the operator.md reference template
    (env["ref_regs"] / "operator.md").unlink()
    with pytest.raises(IdentityError):
        greg.validate_soul_register("strategic-concise", env["home"])


def test_synonym_logs_once_per_process(
    env: dict, caplog: pytest.LogCaptureFixture,
) -> None:
    """A long session shouldn't see the synonym log on every turn."""
    with caplog.at_level(logging.DEBUG, logger="grove.register"):
        greg.validate_soul_register("strategic-concise", env["home"])
        greg.validate_soul_register("strategic-concise", env["home"])
        greg.validate_soul_register("strategic-concise", env["home"])
    msgs = [r for r in caplog.records if "strategic-concise" in r.getMessage()]
    assert len(msgs) == 1


# ════════════════════════════════════════════════════════════════════════════
# grove.affordances
# ════════════════════════════════════════════════════════════════════════════

# --- load_affordances ------------------------------------------------------

def test_load_affordances_seeds_from_reference(env: dict) -> None:
    """First-run: operator copy missing → seed from reference template."""
    assert not (env["home"] / "affordances.md").exists()
    content = gaff.load_affordances(env["home"])
    assert content is not None
    assert "# Affordances" in content
    assert (env["home"] / "affordances.md").exists()


def test_load_affordances_operator_copy_wins(env: dict) -> None:
    """Operator copy at ~/.grove/affordances.md beats the reference template."""
    _write(env["home"] / "affordances.md", "# Affordances (operator-curated)\n")
    assert "operator-curated" in (gaff.load_affordances(env["home"]) or "")


def test_load_affordances_missing_reference_template_raises(
    env: dict,
) -> None:
    """D1 Jidoka — reference template missing AND no operator copy → raise."""
    (env["ref_id"] / "affordances.md").unlink()
    assert not (env["home"] / "affordances.md").exists()
    with pytest.raises(IdentityError, match="Affordances reference template"):
        gaff.load_affordances(env["home"])


def test_load_affordances_empty_operator_copy_returns_none(
    env: dict, caplog: pytest.LogCaptureFixture,
) -> None:
    """Operator copy that's deliberately empty → warn + None (graceful).

    Do NOT overwrite the empty file with the reference template — the
    operator may have emptied it on purpose.
    """
    _write(env["home"] / "affordances.md", "   \n")
    with caplog.at_level(logging.WARNING, logger="grove.affordances"):
        result = gaff.load_affordances(env["home"])
    assert result is None
    assert (env["home"] / "affordances.md").exists()
    assert "   \n" == (env["home"] / "affordances.md").read_text(encoding="utf-8")


# --- introspect_capabilities ----------------------------------------------

def test_introspect_capabilities_returns_structured_prose() -> None:
    """The live introspection block has the four expected section headers."""
    block = gaff.introspect_capabilities()
    assert "## Connected MCP servers" in block
    assert "## Cognitive Router tiers" in block
    assert "## Available slash commands" in block
    assert "## Cellar" in block


def test_introspect_capabilities_defensive_against_primitive_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive-helper asymmetry: introspection is reporting, not governance.

    The contract documented in the module docstring is that each ENUMERATION
    HELPER is internally defensive — when its underlying primitive (config
    reader, sqlite, COMMAND_REGISTRY import) fails, the helper catches the
    failure and returns an empty/sentinel value rather than propagating.
    introspect_capabilities then renders prose around whatever the helpers
    return, including the empty/sentinel cases.

    This test breaks each primitive and verifies introspect_capabilities
    still returns structured prose with the four section headers — proving
    a broken routing.config.yaml or unreadable cellar doesn't prevent the
    operator from starting a session to fix the problem.
    """
    # Break _enumerate_mcps primitive: load_config raises
    import hermes_cli.config as hcfg
    monkeypatch.setattr(hcfg, "load_config", lambda: (_ for _ in ()).throw(RuntimeError("config broken")))
    # Break _enumerate_slash_commands primitive: pretend COMMAND_REGISTRY can't import
    import sys
    monkeypatch.setitem(sys.modules, "hermes_cli.commands", None)
    # _enumerate_tiers is already exercised against a real (or absent) config — both
    # branches return a list, never raise; covered by the OSError/YAMLError try/except.
    # _cellar_status — already defensive against missing db file at sqlite layer.

    block = gaff.introspect_capabilities()
    assert isinstance(block, str)
    assert "## Connected MCP servers" in block
    assert "## Cognitive Router tiers" in block
    assert "## Available slash commands" in block
    assert "## Cellar" in block


# --- _enumerate_mcps secrets discipline ------------------------------------

def test_enumerate_mcps_excludes_env_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NOTION_TOKEN / API keys live in mcp_servers.*.env and MUST NOT leak
    into the system prompt prose. Verify the brief never contains env values."""
    fake_config = {
        "mcp_servers": {
            "test-server": {
                "command": "npx",
                "args": ["-y", "@test/pkg"],
                "env": {
                    "NOTION_TOKEN": "ntn_supersecret_should_never_appear",
                    "API_KEY": "sk-ant-also-never",
                },
            },
        },
    }
    import hermes_cli.config as hcfg
    monkeypatch.setattr(hcfg, "load_config", lambda: fake_config)
    results = gaff._enumerate_mcps()
    assert results == [
        ("test-server", "`npx` '-y' '@test/pkg'"),
    ]
    for _name, brief in results:
        assert "ntn_supersecret" not in brief
        assert "sk-ant" not in brief
        assert "NOTION_TOKEN" not in brief
        assert "API_KEY" not in brief


def test_enumerate_mcps_returns_empty_when_config_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive helper: raise inside load_config → empty list, no propagation."""
    import hermes_cli.config as hcfg

    def boom():
        raise RuntimeError("config broken")

    monkeypatch.setattr(hcfg, "load_config", boom)
    assert gaff._enumerate_mcps() == []


# ════════════════════════════════════════════════════════════════════════════
# grove.identity — Sprint 23 extensions
# ════════════════════════════════════════════════════════════════════════════

# --- IdentityComposition dataclass extensions ------------------------------

def test_composition_has_sprint23_fields_with_defaults() -> None:
    c = IdentityComposition()
    assert c.active_register is None
    assert c.register_overlay is None
    assert c.affordances is None
    assert c.capabilities is None


def test_compose_stable_d5_order() -> None:
    """D5 order: constitution → soul → register → affordances → capabilities
    → operator → goals."""
    c = IdentityComposition(
        constitution="C", soul="S", operator="O", goals="G",
        register_overlay="R", affordances="A", capabilities="K",
    )
    assert c.compose_stable() == "C\n\nS\n\nR\n\nA\n\nK\n\nO\n\nG"


def test_compose_stable_skips_absent_sprint23_layers() -> None:
    """No register/affordances/capabilities → composition matches Sprint 07."""
    c = IdentityComposition(
        constitution="C", soul="S", operator="O", goals="G",
    )
    assert c.compose_stable() == "C\n\nS\n\nO\n\nG"


def test_compose_full_d5_order_includes_memory_and_agents() -> None:
    c = IdentityComposition(
        constitution="C", soul="S", operator="O", goals="G",
        memory="M", agents="A",
        register_overlay="R", affordances="AF", capabilities="K",
    )
    assert c.compose() == "C\n\nS\n\nR\n\nAF\n\nK\n\nO\n\nG\n\nM\n\nA"


# --- load_identity Sprint 23 integration -----------------------------------

def test_load_identity_default_uses_soul_frontmatter(env: dict) -> None:
    """No session override → soul.md frontmatter wins."""
    c = load_identity()
    assert c.active_register == "operator"
    assert c.register_overlay is not None
    assert "# Operator Register" in c.register_overlay


def test_load_identity_session_register_overrides_soul(env: dict) -> None:
    """D6: explicit session_register beats soul.md frontmatter."""
    c = load_identity(session_register="editorial")
    assert c.active_register == "editorial"
    assert "# Editorial Register" in (c.register_overlay or "")


def test_load_identity_session_synonym_resolves(env: dict) -> None:
    """D8 synonym fires on the session-override path too, not just soul."""
    c = load_identity(session_register="strategic-concise")
    assert c.active_register == "operator"


def test_load_identity_session_unknown_raises(env: dict) -> None:
    """Unknown session register name → IdentityError (D4 Jidoka)."""
    with pytest.raises(IdentityError):
        load_identity(session_register="bogus-register")


def test_load_identity_soul_without_register_composes_gracefully(
    env: dict,
) -> None:
    """GATE-E backward-compat criterion: soul.md without `register:` field
    composes without the register layer — Sprint 07 installs keep working."""
    # Override the seeded soul with one that has no register field.
    _write(env["home"] / "soul.md", _SOUL_NO_REGISTER)
    c = load_identity()
    assert c.active_register is None
    assert c.register_overlay is None
    # The other Sprint 23 layers still load — only register is conditional.
    assert c.affordances is not None
    assert c.capabilities is not None


def test_load_identity_soul_with_unknown_register_raises(env: dict) -> None:
    """Soul declaring an unresolvable register → IdentityError at load."""
    _write(env["home"] / "soul.md", _SOUL_UNKNOWN)
    with pytest.raises(IdentityError):
        load_identity()


def test_load_identity_soul_with_legacy_synonym_works(env: dict) -> None:
    """Operator-installed-pre-Sprint-23 soul.md still loads cleanly."""
    _write(env["home"] / "soul.md", _SOUL_LEGACY)
    c = load_identity()
    assert c.active_register == "operator"


def test_compose_stable_includes_capabilities_block(env: dict) -> None:
    """GATE-E criterion: capabilities introspection appears in compose_stable."""
    composed = load_identity().compose_stable()
    assert "## Connected MCP servers" in composed
    assert "## Cognitive Router tiers" in composed


def test_compose_stable_order_real_load(env: dict) -> None:
    """End-to-end: positions of section markers are monotonically increasing."""
    composed = load_identity().compose_stable()
    pos_constitution = composed.index("# Constitution")
    pos_soul = composed.index("# Soul")
    pos_register = composed.index("# Operator Register")
    pos_affordances = composed.index("# Affordances")
    pos_capabilities = composed.index("## Connected MCP servers")
    assert (
        pos_constitution < pos_soul < pos_register
        < pos_affordances < pos_capabilities
    )


def test_load_identity_canon_check_fires_even_with_no_register_field(
    env: dict,
) -> None:
    """D4 install-time canon check is unconditional — Standards must exist
    regardless of whether soul references it."""
    _write(env["home"] / "soul.md", _SOUL_NO_REGISTER)
    (env["ref_regs"] / "standards.md").unlink()
    with pytest.raises(IdentityError, match="Standards Register reference"):
        load_identity()


# ════════════════════════════════════════════════════════════════════════════
# AIAgent.set_session_register
# ════════════════════════════════════════════════════════════════════════════

def test_set_session_register_updates_attribute_and_invalidates_cache() -> None:
    """The setter's documented contract: attr mutation + cache None."""
    import run_agent
    agent = object.__new__(run_agent.AIAgent)
    agent.session_register = None
    agent._composed_system_prompt = "previously-cached-prompt-value"
    agent.set_session_register("editorial")
    assert agent.session_register == "editorial"
    assert agent._composed_system_prompt is None


def test_set_session_register_accepts_none_to_clear() -> None:
    import run_agent
    agent = object.__new__(run_agent.AIAgent)
    agent.session_register = "editorial"
    agent._composed_system_prompt = "cached"
    agent.set_session_register(None)
    assert agent.session_register is None
    assert agent._composed_system_prompt is None


# ════════════════════════════════════════════════════════════════════════════
# /register slash command — HermesCLI._handle_register_command
# ════════════════════════════════════════════════════════════════════════════

class _FakeAgent:
    """Minimal stand-in for AIAgent — records setter calls for assertions."""
    def __init__(self, initial: Optional[str] = None) -> None:
        self.session_register = initial
        self.set_calls: list[Optional[str]] = []
        self._composed_system_prompt = "cached"

    def set_session_register(self, name: Optional[str]) -> None:
        self.session_register = name
        self.set_calls.append(name)
        self._composed_system_prompt = None


def _bare_cli(agent: Optional[_FakeAgent] = None) -> cli.HermesCLI:
    """A bare HermesCLI with only the state /register reads."""
    obj = object.__new__(cli.HermesCLI)
    obj.agent = agent if agent is not None else _FakeAgent()
    return obj


def _capture_io(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture both cli._cprint and builtins.print into one ordered list."""
    lines: list[str] = []
    monkeypatch.setattr(cli, "_cprint", lambda *a, **k: lines.append(a[0] if a else ""))
    monkeypatch.setattr("builtins.print", lambda *a, **k: lines.append(" ".join(str(x) for x in a)))
    return lines


def _seed_operator_soul(env: dict) -> None:
    """Seed an operator soul.md so the bare-display path has a source to read."""
    _write(env["home"] / "soul.md", _SOUL_OPERATOR)


# --- bare ---

def test_register_bare_shows_soul_default(
    env: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Form 1: /register → 'Active: operator' + 'Source: soul.md frontmatter'."""
    _seed_operator_soul(env)
    obj = _bare_cli()
    lines = _capture_io(monkeypatch)
    obj._handle_register_command("/register")
    out = "\n".join(lines)
    assert "operator" in out
    assert "soul.md" in out


def test_register_bare_shows_session_override_when_present(
    env: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Form 1 variant: when a session override is set, display it + source label."""
    _seed_operator_soul(env)
    obj = _bare_cli(agent=_FakeAgent(initial="editorial"))
    lines = _capture_io(monkeypatch)
    obj._handle_register_command("/register")
    out = "\n".join(lines)
    assert "editorial" in out
    assert "session overlay" in out


def test_register_bare_handles_soul_without_register_field(
    env: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backward-compat: soul.md without `register:` → bare prints '(none)'."""
    _write(env["home"] / "soul.md", _SOUL_NO_REGISTER)
    obj = _bare_cli()
    lines = _capture_io(monkeypatch)
    obj._handle_register_command("/register")
    out = "\n".join(lines)
    assert "(none)" in out


# --- list ---

def test_register_list_enumerates_canon_trio(
    env: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    obj = _bare_cli()
    lines = _capture_io(monkeypatch)
    obj._handle_register_command("/register list")
    out = "\n".join(lines)
    assert "standards" in out
    assert "operator" in out
    assert "editorial" in out


# --- reset ---

def test_register_reset_clears_override_and_calls_agent_setter(
    env: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _FakeAgent(initial="editorial")
    obj = _bare_cli(agent=agent)
    _capture_io(monkeypatch)
    obj._handle_register_command("/register reset")
    assert agent.set_calls == [None]
    assert agent.session_register is None


def test_register_reset_noop_when_no_override_active(
    env: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reset is informational when there's nothing to clear — don't churn the cache."""
    agent = _FakeAgent(initial=None)
    obj = _bare_cli(agent=agent)
    lines = _capture_io(monkeypatch)
    obj._handle_register_command("/register reset")
    assert agent.set_calls == []
    assert any("No session override" in line for line in lines)


# --- /register <name> ---

def test_register_set_canonical_name_calls_agent_setter(
    env: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _FakeAgent(initial=None)
    obj = _bare_cli(agent=agent)
    _capture_io(monkeypatch)
    obj._handle_register_command("/register editorial")
    assert agent.set_calls == ["editorial"]


def test_register_set_synonym_maps_to_canonical_and_reports_mapping(
    env: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D8 synonym fires through the verb too — the agent gets the canonical
    name and the operator sees the mapping in the confirmation line."""
    agent = _FakeAgent(initial=None)
    obj = _bare_cli(agent=agent)
    lines = _capture_io(monkeypatch)
    obj._handle_register_command("/register strategic-concise")
    assert agent.set_calls == ["operator"]
    assert any("mapped from" in line for line in lines)


def test_register_set_unknown_name_rejects_without_calling_setter(
    env: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defense-in-depth: the verb validates before invoking the agent setter,
    so a typo doesn't invalidate the prompt cache mid-session."""
    agent = _FakeAgent(initial=None)
    obj = _bare_cli(agent=agent)
    lines = _capture_io(monkeypatch)
    obj._handle_register_command("/register not-a-real-register")
    assert agent.set_calls == []
    # The IdentityError prose is what gets printed.
    assert any("not-a-real-register" in line for line in lines)


def test_register_no_agent_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    """When there's no active agent (early session lifecycle), the verb
    prints a notice and returns without raising."""
    obj = object.__new__(cli.HermesCLI)
    obj.agent = None
    lines = _capture_io(monkeypatch)
    obj._handle_register_command("/register")
    assert any("requires an active agent" in line for line in lines)
