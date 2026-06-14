"""Tests for agent/skill_commands.py — skill slash command scanning and platform filtering."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

import tools.skills_tool as skills_tool_module
from agent.skill_commands import (
    build_preloaded_skills_prompt,
    build_skill_invocation_message,
    resolve_skill_command_key,
    scan_skill_commands,
)


def _make_skill(
    skills_dir, name, frontmatter_extra="", body="Do the thing.", category=None
):
    """Helper to create a minimal skill directory with SKILL.md."""
    if category:
        skill_dir = skills_dir / category / name
    else:
        skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    content = f"""\
---
name: {name}
description: Description for {name}.
{frontmatter_extra}---

# {name}

{body}
"""
    (skill_dir / "SKILL.md").write_text(content)
    return skill_dir


def _symlink_category(skills_dir: Path, linked_root: Path, category: str) -> Path:
    """Create a category symlink under skills_dir pointing outside the tree."""
    external_category = linked_root / category
    external_category.mkdir(parents=True, exist_ok=True)
    symlink_path = skills_dir / category
    try:
        symlink_path.symlink_to(external_category, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable in test environment: {exc}")
    return external_category


# GRV-009 E6a C4 — the /skill subsystem is record-driven. These helpers register
# synthetic kind=skill records (the SKILL.md inlined as the payload, matching
# _make_skill's exact bytes) in isolation from the real bundle, and — for the
# invocation tests — plant the on-disk files at the reconstructed
# SKILLS_DIR/<category>/<name> path so skill_dir content (scripts/templates) loads.


@pytest.fixture
def registry(monkeypatch):
    reg = {}
    monkeypatch.setattr(
        "grove.capability_registry.load_capabilities", lambda *a, **k: dict(reg)
    )
    monkeypatch.setattr("agent.skill_utils.get_external_skills_dirs", lambda: [])
    return reg


def _skill_content(name, frontmatter_extra="", body="Do the thing."):
    return (
        f"---\nname: {name}\ndescription: Description for {name}.\n"
        f"{frontmatter_extra}---\n\n# {name}\n\n{body}\n"
    )


def _record(name, *, frontmatter_name=None, frontmatter_extra="", body="Do the thing.",
            category="dom", payload=None):
    from grove.capability import (
        Capability, CapabilityKind, CircuitBreaker, Context, Disclosure,
        DockComposition, Failure, Lifecycle, LifecycleState, Provenance,
        SkillPresentation, Telemetry, TierRule, TierValidation, Trigger,
        TriggerDisclosure, Zone,
    )
    fm_name = frontmatter_name if frontmatter_name is not None else name
    if payload is None:
        payload = _skill_content(fm_name, frontmatter_extra, body)
    return Capability(
        id=f"skill.{category}.{name}", kind=CapabilityKind.SKILL,
        trigger=Trigger(always=True, disclosure=TriggerDisclosure.PROACTIVE),
        tier_rule=TierRule(eligible=[1, 2, 3], preferred=1,
                           validation=TierValidation(confidence_threshold=0.95, shadow_window=20)),
        zone=Zone.GREEN, telemetry=Telemetry(feed="intent_feed"),
        context=Context(disclosure=Disclosure.PULL, payload=payload,
                        dock_composition=DockComposition.NONE),
        lifecycle=Lifecycle(state=LifecycleState.ACTIVE, provenance=Provenance.MIGRATED),
        failure=Failure(circuit_breaker=CircuitBreaker(threshold=3, window_seconds=300)),
        skill=SkillPresentation(category=category),
    )


def _register(registry, name, *, frontmatter_name=None, frontmatter_extra="",
              body="Do the thing.", category="dom", payload=None):
    rec = _record(name, frontmatter_name=frontmatter_name, frontmatter_extra=frontmatter_extra,
                  body=body, category=category, payload=payload)
    registry[rec.id] = rec
    return rec


def _plant_and_register(registry, skills_dir, name, *, frontmatter_extra="",
                        body="Do the thing.", category="dom"):
    _make_skill(skills_dir, name, frontmatter_extra, body, category)
    _register(registry, name, frontmatter_extra=frontmatter_extra, body=body, category=category)
    return skills_dir / category / name


class TestScanSkillCommands:
    def test_finds_skills(self, tmp_path, registry):
        _register(registry, "my-skill")
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            result = scan_skill_commands()
        assert "/my-skill" in result
        assert result["/my-skill"]["name"] == "my-skill"

    def test_empty_dir(self, tmp_path, registry):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            result = scan_skill_commands()
        assert result == {}

    def test_excludes_incompatible_platform(self, tmp_path, registry):
        """macOS-only skills should not register slash commands on Linux."""
        _register(registry, "imessage", frontmatter_extra="platforms: [macos]\n")
        _register(registry, "web-search")
        with (
            patch("tools.skills_tool.SKILLS_DIR", tmp_path),
            patch("agent.skill_utils.sys") as mock_sys,
        ):
            mock_sys.platform = "linux"
            result = scan_skill_commands()
        assert "/web-search" in result
        assert "/imessage" not in result

    def test_includes_matching_platform(self, tmp_path, registry):
        """macOS-only skills should register slash commands on macOS."""
        _register(registry, "imessage", frontmatter_extra="platforms: [macos]\n")
        with (
            patch("tools.skills_tool.SKILLS_DIR", tmp_path),
            patch("agent.skill_utils.sys") as mock_sys,
        ):
            mock_sys.platform = "darwin"
            result = scan_skill_commands()
        assert "/imessage" in result

    def test_universal_skill_on_any_platform(self, tmp_path, registry):
        """Skills without platforms field should register on any platform."""
        _register(registry, "generic-tool")
        with (
            patch("tools.skills_tool.SKILLS_DIR", tmp_path),
            patch("agent.skill_utils.sys") as mock_sys,
        ):
            mock_sys.platform = "win32"
            result = scan_skill_commands()
        assert "/generic-tool" in result

    def test_excludes_disabled_skills(self, tmp_path, registry):
        """Disabled skills should not register slash commands."""
        _register(registry, "enabled-skill")
        _register(registry, "disabled-skill")
        with (
            patch("tools.skills_tool.SKILLS_DIR", tmp_path),
            patch(
                "tools.skills_tool._get_disabled_skill_names",
                return_value={"disabled-skill"},
            ),
        ):
            result = scan_skill_commands()
        assert "/enabled-skill" in result
        assert "/disabled-skill" not in result

    def test_finds_skills_in_symlinked_category_dir(self, tmp_path, monkeypatch):
        # GRV-009 E6a C4 — symlinked-category handling lives on the retained
        # external-dir scan; no bundled records here.
        monkeypatch.setattr(
            "grove.capability_registry.load_capabilities", lambda *a, **k: {}
        )
        external_root = tmp_path / "repo"
        ext_dir = tmp_path / "ext"
        ext_dir.mkdir()
        external_category = _symlink_category(ext_dir, external_root, "linked")
        _make_skill(external_category.parent, "knowledge-brain", category="linked")
        monkeypatch.setattr("agent.skill_utils.get_external_skills_dirs", lambda: [ext_dir])

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path / "_local"):
            result = scan_skill_commands()

        assert "/knowledge-brain" in result
        assert result["/knowledge-brain"]["name"] == "knowledge-brain"

    def test_get_skill_commands_rescans_when_platform_scope_changes(self, tmp_path, registry):
        """Platform-specific disabled-skill caches must not leak across platforms.

        Regression test for #14536: a gateway process serving Telegram
        and Discord concurrently would seed the process-global cache
        with whichever platform scanned first, and subsequent
        ``get_skill_commands()`` calls from the other platform silently
        inherited that filter.
        """
        import agent.skill_commands as sc_mod
        from agent.skill_commands import get_skill_commands

        def _disabled_skills():
            platform = os.getenv("GROVE_PLATFORM")
            if platform == "telegram":
                return {"telegram-only"}
            if platform == "discord":
                return {"discord-only"}
            return set()

        _register(registry, "shared")
        _register(registry, "telegram-only")
        _register(registry, "discord-only")
        with (
            patch("tools.skills_tool.SKILLS_DIR", tmp_path),
            patch("tools.skills_tool._get_disabled_skill_names", side_effect=_disabled_skills),
            patch.object(sc_mod, "_skill_commands", {}),
            patch.object(sc_mod, "_skill_commands_platform", None),
        ):

            with patch.dict(os.environ, {"GROVE_PLATFORM": "telegram"}):
                telegram_commands = dict(get_skill_commands())

            assert "/shared" in telegram_commands
            assert "/discord-only" in telegram_commands
            assert "/telegram-only" not in telegram_commands

            with patch.dict(os.environ, {"GROVE_PLATFORM": "discord"}):
                discord_commands = dict(get_skill_commands())

            assert "/shared" in discord_commands
            assert "/telegram-only" in discord_commands
            assert "/discord-only" not in discord_commands

            # Switching back to telegram must also rescan — not re-serve
            # the discord view that was just cached.
            with patch.dict(os.environ, {"GROVE_PLATFORM": "telegram"}):
                telegram_again = dict(get_skill_commands())

            assert "/telegram-only" not in telegram_again
            assert "/discord-only" in telegram_again

    def test_get_skill_commands_rescans_when_session_platform_changes(self, tmp_path, registry):
        """``GROVE_SESSION_PLATFORM`` from the gateway session context must
        also trigger a rescan, not just ``GROVE_PLATFORM`` (#14536).

        Exercises the real ContextVar path: the gateway sets the active
        adapter via ``set_session_vars(platform=...)`` and the resolver
        reads it via ``get_session_env``. Setting ``GROVE_SESSION_PLATFORM``
        in ``os.environ`` would only test ``get_session_env``'s legacy
        env-var fallback — a regression that swapped ``get_session_env``
        for plain ``os.getenv`` would still pass while breaking concurrent
        gateway sessions, which is the bug the ContextVar plumbing exists
        to prevent in the first place.
        """
        import agent.skill_commands as sc_mod
        from agent.skill_commands import get_skill_commands
        from gateway.session_context import (
            clear_session_vars,
            get_session_env,
            set_session_vars,
        )

        def _disabled_skills():
            platform = (
                os.getenv("GROVE_PLATFORM")
                or get_session_env("GROVE_SESSION_PLATFORM")
            )
            if platform == "telegram":
                return {"telegram-only"}
            if platform == "discord":
                return {"discord-only"}
            return set()

        _register(registry, "shared")
        _register(registry, "telegram-only")
        _register(registry, "discord-only")
        with (
            patch("tools.skills_tool.SKILLS_DIR", tmp_path),
            patch("tools.skills_tool._get_disabled_skill_names", side_effect=_disabled_skills),
            patch.object(sc_mod, "_skill_commands", {}),
            patch.object(sc_mod, "_skill_commands_platform", None),
        ):

            # First simulated gateway request: telegram handler.
            tokens = set_session_vars(platform="telegram")
            try:
                telegram_commands = dict(get_skill_commands())
            finally:
                clear_session_vars(tokens)

            assert "/shared" in telegram_commands
            assert "/discord-only" in telegram_commands
            assert "/telegram-only" not in telegram_commands

            # Second simulated gateway request: discord handler. The cache
            # was just populated for telegram; the rescan trigger must fire
            # off the ContextVar change, not just an env-var change.
            tokens = set_session_vars(platform="discord")
            try:
                discord_commands = dict(get_skill_commands())
            finally:
                clear_session_vars(tokens)

            assert "/shared" in discord_commands
            assert "/telegram-only" in discord_commands
            assert "/discord-only" not in discord_commands

    def test_get_skill_commands_rescans_when_leaving_platform_scope(self, tmp_path, monkeypatch, registry):
        """Returning to no-platform-scope (CLI / cron / RL) after a gateway
        session must rescan so the unfiltered view is repopulated (#14536).

        A long-lived process running both gateway sessions and bare CLI
        invocations would otherwise stay stuck on whichever platform's
        filter was last applied.
        """
        import agent.skill_commands as sc_mod
        from agent.skill_commands import get_skill_commands

        def _disabled_skills():
            if os.getenv("GROVE_PLATFORM") == "telegram":
                return {"telegram-only"}
            return set()

        with (
            patch("tools.skills_tool.SKILLS_DIR", tmp_path),
            patch("tools.skills_tool._get_disabled_skill_names", side_effect=_disabled_skills),
            patch.object(sc_mod, "_skill_commands", {}),
            patch.object(sc_mod, "_skill_commands_platform", None),
        ):
            _register(registry, "shared")
            _register(registry, "telegram-only")

            monkeypatch.setenv("GROVE_PLATFORM", "telegram")
            telegram_commands = dict(get_skill_commands())
            assert "/telegram-only" not in telegram_commands

            # Drop back to no platform scope — bare CLI / cron / RL rollouts.
            monkeypatch.delenv("GROVE_PLATFORM", raising=False)
            bare_commands = dict(get_skill_commands())

            assert "/telegram-only" in bare_commands
            assert sc_mod._skill_commands_platform is None

    def test_get_skill_commands_does_not_rescan_when_platform_unchanged(self, tmp_path, registry):
        """Same-platform back-to-back calls must hit the cache, not rescan.

        The rescan trigger is *change* in platform scope, not "always
        re-resolve." A gateway serving consecutive telegram requests must
        not pay the scan cost for each one.
        """
        import agent.skill_commands as sc_mod
        from agent.skill_commands import get_skill_commands

        with (
            patch("tools.skills_tool.SKILLS_DIR", tmp_path),
            patch.object(sc_mod, "_skill_commands", {}),
            patch.object(sc_mod, "_skill_commands_platform", None),
            patch.dict(os.environ, {"GROVE_PLATFORM": "telegram"}),
        ):
            _register(registry, "shared")
            # Prime the cache.
            get_skill_commands()
            # Spy on rescans during the subsequent same-platform calls.
            with patch(
                "agent.skill_commands.scan_skill_commands",
                wraps=sc_mod.scan_skill_commands,
            ) as scan_spy:
                get_skill_commands()
                get_skill_commands()
                get_skill_commands()
            assert scan_spy.call_count == 0


    def test_special_chars_stripped_from_cmd_key(self, tmp_path, registry):
        """Skill names with +, /, or other special chars produce clean cmd keys."""
        _register(registry, "jellyfin-plus",
                  frontmatter_name="Jellyfin + Jellystat 24h Summary")
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            result = scan_skill_commands()
        # The + should be stripped, not left as a literal character
        assert "/jellyfin-jellystat-24h-summary" in result
        # The old buggy key should NOT exist
        assert "/jellyfin-+-jellystat-24h-summary" not in result

    def test_allspecial_name_skipped(self, tmp_path, registry):
        """Skill with name consisting only of special chars is silently skipped."""
        _register(registry, "bad-name", frontmatter_name="+++")
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            result = scan_skill_commands()
        # Should not create a "/" key or any entry
        assert "/" not in result
        assert result == {}

    def test_slash_in_name_stripped_from_cmd_key(self, tmp_path, registry):
        """Skill names with / chars produce clean cmd keys."""
        _register(registry, "sonarr-api", frontmatter_name="Sonarr v3/v4 API")
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            result = scan_skill_commands()
        assert "/sonarr-v3v4-api" in result
        assert any("/" in k[1:] for k in result) is False  # no unescaped /


class TestResolveSkillCommandKey:
    """Telegram bot-command names disallow hyphens, so the menu registers
    skills with hyphens swapped for underscores. When Telegram autocomplete
    sends the underscored form back, we need to find the hyphenated key.
    """

    def test_hyphenated_form_matches_directly(self, tmp_path, registry):
        _register(registry, "claude-code")
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            scan_skill_commands()
            assert resolve_skill_command_key("claude-code") == "/claude-code"

    def test_underscore_form_resolves_to_hyphenated_skill(self, tmp_path, registry):
        """/claude_code from Telegram autocomplete must resolve to /claude-code."""
        _register(registry, "claude-code")
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            scan_skill_commands()
            assert resolve_skill_command_key("claude_code") == "/claude-code"

    def test_single_word_command_resolves(self, tmp_path, registry):
        _register(registry, "investigate")
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            scan_skill_commands()
            assert resolve_skill_command_key("investigate") == "/investigate"

    def test_unknown_command_returns_none(self, tmp_path, registry):
        _register(registry, "claude-code")
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            scan_skill_commands()
            assert resolve_skill_command_key("does_not_exist") is None
            assert resolve_skill_command_key("does-not-exist") is None

    def test_empty_command_returns_none(self, tmp_path, registry):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            scan_skill_commands()
            assert resolve_skill_command_key("") is None

    def test_hyphenated_command_is_not_mangled(self, tmp_path, registry):
        """A user-typed /foo-bar (hyphen) must not trigger the underscore fallback."""
        _register(registry, "foo-bar")
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            scan_skill_commands()
            assert resolve_skill_command_key("foo-bar") == "/foo-bar"
            # Underscore form also works (Telegram round-trip)
            assert resolve_skill_command_key("foo_bar") == "/foo-bar"


class TestBuildPreloadedSkillsPrompt:
    def test_builds_prompt_for_multiple_named_skills(self, tmp_path):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(tmp_path, "first-skill")
            _make_skill(tmp_path, "second-skill")
            prompt, loaded, missing = build_preloaded_skills_prompt(
                ["first-skill", "second-skill"]
            )

        assert missing == []
        assert loaded == ["first-skill", "second-skill"]
        assert "first-skill" in prompt
        assert "second-skill" in prompt
        assert "preloaded" in prompt.lower()

    def test_reports_missing_named_skills(self, tmp_path):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(tmp_path, "present-skill")
            prompt, loaded, missing = build_preloaded_skills_prompt(
                ["present-skill", "missing-skill"]
            )

        assert "present-skill" in prompt
        assert loaded == ["present-skill"]
        assert missing == ["missing-skill"]


class TestBuildSkillInvocationMessage:
    def test_loads_skill_by_stored_path_when_frontmatter_name_differs(self, tmp_path, registry):
        skill_dir = tmp_path / "mlops" / "audiocraft"
        skill_dir.mkdir(parents=True, exist_ok=True)
        content = (
            "---\nname: audiocraft-audio-generation\n"
            "description: Generate audio with AudioCraft.\n---\n\n"
            "# AudioCraft\n\nGenerate some audio.\n"
        )
        (skill_dir / "SKILL.md").write_text(content)
        # dir name "audiocraft" ≠ frontmatter slug — the record's id-tail/category
        # reconstruct skill_dir = SKILLS_DIR/mlops/audiocraft (the planted path).
        _register(registry, "audiocraft", category="mlops", payload=content)

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            scan_skill_commands()
            msg = build_skill_invocation_message("/audiocraft-audio-generation", "compose")

        assert msg is not None
        assert "AudioCraft" in msg
        assert "compose" in msg

    def test_builds_message(self, tmp_path, registry):
        _plant_and_register(registry, tmp_path, "test-skill")
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            scan_skill_commands()
            msg = build_skill_invocation_message("/test-skill", "do stuff")
        assert msg is not None
        assert "test-skill" in msg
        assert "do stuff" in msg

    def test_returns_none_for_unknown(self, tmp_path):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            scan_skill_commands()
            msg = build_skill_invocation_message("/nonexistent")
        assert msg is None

    def test_uses_shared_skill_loader_for_secure_setup(self, tmp_path, monkeypatch, registry):
        monkeypatch.delenv("TENOR_API_KEY", raising=False)
        calls = []

        def fake_secret_callback(var_name, prompt, metadata=None):
            calls.append((var_name, prompt, metadata))
            os.environ[var_name] = "stored-in-test"
            return {
                "success": True,
                "stored_as": var_name,
                "validated": False,
                "skipped": False,
            }

        monkeypatch.setattr(
            skills_tool_module,
            "_secret_capture_callback",
            fake_secret_callback,
            raising=False,
        )

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _plant_and_register(
                registry,
                tmp_path,
                "test-skill",
                frontmatter_extra=(
                    "required_environment_variables:\n"
                    "  - name: TENOR_API_KEY\n"
                    "    prompt: Tenor API key\n"
                ),
            )
            scan_skill_commands()
            msg = build_skill_invocation_message("/test-skill", "do stuff")

        assert msg is not None
        assert "test-skill" in msg
        assert len(calls) == 1
        assert calls[0][0] == "TENOR_API_KEY"

    def test_gateway_still_loads_skill_but_returns_setup_guidance(
        self, tmp_path, monkeypatch, registry
    ):
        monkeypatch.delenv("TENOR_API_KEY", raising=False)

        def fail_if_called(var_name, prompt, metadata=None):
            raise AssertionError(
                "gateway flow should not try secure in-band secret capture"
            )

        monkeypatch.setattr(
            skills_tool_module,
            "_secret_capture_callback",
            fail_if_called,
            raising=False,
        )

        with patch.dict(
            os.environ, {"GROVE_SESSION_PLATFORM": "telegram"}, clear=False
        ):
            with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
                _plant_and_register(
                    registry,
                    tmp_path,
                    "test-skill",
                    frontmatter_extra=(
                        "required_environment_variables:\n"
                        "  - name: TENOR_API_KEY\n"
                        "    prompt: Tenor API key\n"
                    ),
                )
                scan_skill_commands()
                msg = build_skill_invocation_message("/test-skill", "do stuff")

        assert msg is not None
        assert "local cli" in msg.lower()

    def test_preserves_remaining_remote_setup_warning(self, tmp_path, monkeypatch, registry):
        monkeypatch.setenv("TERMINAL_ENV", "ssh")
        monkeypatch.delenv("TENOR_API_KEY", raising=False)
        monkeypatch.setattr(
            skills_tool_module,
            "_secret_capture_callback",
            None,
            raising=False,
        )

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _plant_and_register(
                registry,
                tmp_path,
                "test-skill",
                frontmatter_extra=(
                    "required_environment_variables:\n"
                    "  - name: TENOR_API_KEY\n"
                    "    prompt: Tenor API key\n"
                ),
            )
            scan_skill_commands()
            msg = build_skill_invocation_message("/test-skill", "do stuff")

        assert msg is not None
        assert "remote environment" in msg.lower()

    def test_supporting_file_hint_uses_file_path_argument(self, tmp_path, registry):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            skill_dir = _plant_and_register(registry, tmp_path, "test-skill")
            references = skill_dir / "references"
            references.mkdir()
            (references / "api.md").write_text("reference")
            scan_skill_commands()
            msg = build_skill_invocation_message("/test-skill", "do stuff")

        assert msg is not None
        assert 'file_path="<path>"' in msg


class TestSkillDirectoryHeader:
    """The activation message must expose the absolute skill directory and
    explain how to resolve relative paths, so skills with bundled scripts
    don't force the agent into a second ``skill_view()`` round-trip."""

    def test_header_contains_absolute_skill_dir(self, tmp_path, registry):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            skill_dir = _plant_and_register(registry, tmp_path, "abs-dir-skill")
            scan_skill_commands()
            msg = build_skill_invocation_message("/abs-dir-skill", "go")

        assert msg is not None
        assert f"[Skill directory: {skill_dir}]" in msg
        assert "Resolve any relative paths" in msg

    def test_supporting_files_shown_with_absolute_paths(self, tmp_path, registry):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            skill_dir = _plant_and_register(registry, tmp_path, "scripted-skill")
            (skill_dir / "scripts").mkdir()
            (skill_dir / "scripts" / "run.js").write_text("console.log('hi')")
            scan_skill_commands()
            msg = build_skill_invocation_message("/scripted-skill")

        assert msg is not None
        # The supporting-files block must emit both the relative form (so the
        # agent can call skill_view on it) and the absolute form (so it can
        # run the script directly via terminal).
        assert "scripts/run.js" in msg
        assert str(skill_dir / "scripts" / "run.js") in msg
        assert f"node {skill_dir}/scripts/foo.js" in msg


class TestTemplateVarSubstitution:
    """``${GROVE_SKILL_DIR}`` and ``${GROVE_SESSION_ID}`` in SKILL.md body
    are replaced before the agent sees the content."""

    def test_substitutes_skill_dir(self, tmp_path, registry):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            skill_dir = _plant_and_register(
                registry,
                tmp_path,
                "templated",
                body="Run: node ${GROVE_SKILL_DIR}/scripts/foo.js",
            )
            scan_skill_commands()
            msg = build_skill_invocation_message("/templated")

        assert msg is not None
        assert f"node {skill_dir}/scripts/foo.js" in msg
        # The literal template token must not leak through.
        assert "${GROVE_SKILL_DIR}" not in msg.split("[Skill directory:")[0]

    def test_substitutes_session_id_when_available(self, tmp_path, registry):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _plant_and_register(
                registry,
                tmp_path,
                "sess-templated",
                body="Session: ${GROVE_SESSION_ID}",
            )
            scan_skill_commands()
            msg = build_skill_invocation_message(
                "/sess-templated", task_id="abc-123"
            )

        assert msg is not None
        assert "Session: abc-123" in msg

    def test_leaves_session_id_token_when_missing(self, tmp_path, registry):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _plant_and_register(
                registry,
                tmp_path,
                "sess-missing",
                body="Session: ${GROVE_SESSION_ID}",
            )
            scan_skill_commands()
            msg = build_skill_invocation_message("/sess-missing", task_id=None)

        assert msg is not None
        # No session — token left intact so the author can spot it.
        assert "Session: ${GROVE_SESSION_ID}" in msg

    def test_disable_template_vars_via_config(self, tmp_path, registry):
        with (
            patch("tools.skills_tool.SKILLS_DIR", tmp_path),
            patch(
                "agent.skill_commands._load_skills_config",
                return_value={"template_vars": False},
            ),
        ):
            _plant_and_register(
                registry,
                tmp_path,
                "no-sub",
                body="Run: node ${GROVE_SKILL_DIR}/scripts/foo.js",
            )
            scan_skill_commands()
            msg = build_skill_invocation_message("/no-sub")

        assert msg is not None
        # Template token must survive when substitution is disabled.
        assert "${GROVE_SKILL_DIR}/scripts/foo.js" in msg


class TestInlineShellExpansion:
    """Inline ``!`cmd`` snippets in SKILL.md run before the agent sees the
    content — but only when the user has opted in via config."""

    def test_inline_shell_is_off_by_default(self, tmp_path, registry):
        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _plant_and_register(
                registry,
                tmp_path,
                "dyn-default-off",
                body="Today is !`echo INLINE_RAN`.",
            )
            scan_skill_commands()
            msg = build_skill_invocation_message("/dyn-default-off")

        assert msg is not None
        # Default config has inline_shell=False — snippet must stay literal.
        assert "!`echo INLINE_RAN`" in msg
        assert "Today is INLINE_RAN." not in msg

    def test_inline_shell_runs_when_enabled(self, tmp_path, registry):
        with (
            patch("tools.skills_tool.SKILLS_DIR", tmp_path),
            patch(
                "agent.skill_commands._load_skills_config",
                return_value={"template_vars": True, "inline_shell": True,
                              "inline_shell_timeout": 5},
            ),
        ):
            _plant_and_register(
                registry,
                tmp_path,
                "dyn-on",
                body="Marker: !`echo INLINE_RAN`.",
            )
            scan_skill_commands()
            msg = build_skill_invocation_message("/dyn-on")

        assert msg is not None
        assert "Marker: INLINE_RAN." in msg
        assert "!`echo INLINE_RAN`" not in msg

    def test_inline_shell_runs_in_skill_directory(self, tmp_path, registry):
        """Inline snippets get the skill dir as CWD so relative paths work."""
        with (
            patch("tools.skills_tool.SKILLS_DIR", tmp_path),
            patch(
                "agent.skill_commands._load_skills_config",
                return_value={"template_vars": True, "inline_shell": True,
                              "inline_shell_timeout": 5},
            ),
        ):
            skill_dir = _plant_and_register(
                registry,
                tmp_path,
                "dyn-cwd",
                body="Here: !`pwd`",
            )
            scan_skill_commands()
            msg = build_skill_invocation_message("/dyn-cwd")

        assert msg is not None
        assert f"Here: {skill_dir}" in msg

    def test_inline_shell_timeout_does_not_break_message(self, tmp_path, registry):
        with (
            patch("tools.skills_tool.SKILLS_DIR", tmp_path),
            patch(
                "agent.skill_commands._load_skills_config",
                return_value={"template_vars": True, "inline_shell": True,
                              "inline_shell_timeout": 1},
            ),
        ):
            _plant_and_register(
                registry,
                tmp_path,
                "dyn-slow",
                body="Slow: !`sleep 5 && printf DYN_MARKER`",
            )
            scan_skill_commands()
            msg = build_skill_invocation_message("/dyn-slow")

        assert msg is not None
        # Timeout is surfaced as a marker instead of propagating as an error,
        # and the rest of the skill message still renders.
        assert "inline-shell timeout" in msg
        # The command's intended stdout never made it through — only the
        # timeout marker (which echoes the command text) survives.
        assert "DYN_MARKER" not in msg.replace("sleep 5 && printf DYN_MARKER", "")
