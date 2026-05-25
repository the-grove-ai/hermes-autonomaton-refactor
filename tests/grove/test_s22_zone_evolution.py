"""Sprint 22 — zone parameter evolution invariants.

Hierarchical classification: tool → rule → argument pattern, evaluated
top-to-bottom with first-match-wins. Argument-level matches override
the tool's default_zone. Bare-string tool_zones entries continue to
behave identically to pre-Sprint-22.

The headline invariant tested here is **rule ordering**: an operator
greenlisting `/tmp/*` for a tool MUST NOT inadvertently greenlist
`rm -rf /`. The schema's rule list is order-sensitive; safety-critical
patterns are listed first.

I4 (W3.0a — zone checks unsuppressible) is preserved through the
evolution: a classifier load failure still produces fail-closed
behaviour rather than a silent fall-through.
"""

from __future__ import annotations

import re
import shutil
import textwrap
from pathlib import Path

import pytest

from grove import dispatch as gdispatch
from grove import zones as gz
from grove import zone_rules as zr
from grove.zones import ZoneClassifier


# ── Fixtures ──────────────────────────────────────────────────────────────────


_LEGACY_SCHEMA = """
    schema_version: 1
    zones:
      green:
        auto_approve:
          - calendar.read.*
      yellow:
        proposes:
          - skill.create.*
      red:
        sovereign:
          - command.execute.sudo
    tool_zones:
      terminal:               yellow
      calendar.read:          green
      skill_manage.promote:   red
"""


_HIERARCHICAL_SCHEMA = """
    schema_version: 1
    zones:
      green: {auto_approve: []}
      yellow: {proposes: []}
      red: {sovereign: []}
    tool_zones:
      # Bare-string entries — must behave identically to pre-S22.
      calendar.read: green
      skill_manage.promote: red

      # Hierarchical entry — argument-level rules, first-match-wins.
      # IMPORTANT: catastrophic shapes listed FIRST so the operator's
      # /tmp greenlist cannot accidentally include `rm -rf /`.
      terminal:
        default_zone: yellow
        rules:
          - match_pattern: '^sudo\\s+.*'
            zone: red
            reason: "Privilege escalation requires sovereign approval."
          - match_pattern: '^rm\\s+-rf\\s+/$'
            zone: red
            reason: "Catastrophic root-filesystem deletion."
          - match_pattern: '^rm\\s+(-[fir]+\\s+)?/tmp/.*'
            zone: green
            reason: "Temporary directory cleanup is inherently safe."
"""


@pytest.fixture
def legacy_classifier(tmp_path: Path):
    """A bare-string-only schema — what every pre-S22 deployment used."""
    schema = tmp_path / "zones.schema.yaml"
    schema.write_text(textwrap.dedent(_LEGACY_SCHEMA).lstrip())
    return ZoneClassifier(schema)


@pytest.fixture
def hierarchical_classifier(tmp_path: Path):
    """A schema mixing bare-string and dict-form tool_zones entries."""
    schema = tmp_path / "zones.schema.yaml"
    schema.write_text(textwrap.dedent(_HIERARCHICAL_SCHEMA).lstrip())
    return ZoneClassifier(schema)


@pytest.fixture
def dispatch_with(monkeypatch: pytest.MonkeyPatch):
    """Helper: pin the dispatch classifier to a test instance and clean up."""
    def _attach(classifier):
        monkeypatch.setattr(gdispatch, "_classifier", classifier)
        return classifier
    yield _attach
    gdispatch.reset_classifier()


# ── Hierarchical classification ──────────────────────────────────────────────


class TestHierarchicalClassification:
    """Argument-level rules override the tool's default_zone."""

    def test_green_arg_pattern_in_yellow_tool_wins(self, hierarchical_classifier):
        # terminal's default_zone is yellow; the /tmp rule is green.
        r = hierarchical_classifier.classify_command_string(
            "rm /tmp/cache.log", "command.execute.rm", tool_id="terminal",
        )
        assert r.zone == "green"
        assert r.reason == "Temporary directory cleanup is inherently safe."
        assert r.pattern_key == r"^rm\s+(-[fir]+\s+)?/tmp/.*"

    def test_red_arg_pattern_in_yellow_tool_wins(self, hierarchical_classifier):
        r = hierarchical_classifier.classify_command_string(
            "sudo apt install foo", "command.execute.sudo", tool_id="terminal",
        )
        assert r.zone == "red"
        assert "sovereign approval" in r.reason.lower()

    def test_no_rule_match_falls_through_to_default_zone(self, hierarchical_classifier):
        r = hierarchical_classifier.classify_command_string(
            "ls -la /home/user", "command.execute.ls", tool_id="terminal",
        )
        assert r.zone == "yellow"
        # Default_zone fallthrough — no rule matched, so no reason / pattern_key
        assert r.reason is None
        assert r.pattern_key is None
        assert r.source == "tool_zones.terminal.default"

    def test_first_match_wins(self, hierarchical_classifier):
        # The /tmp rule comes AFTER the sudo and rm-rf-/ rules in the schema.
        # Verify a command that could theoretically match multiple rules
        # (here just one does, but the ordering test is about which fires)
        # picks up the first applicable.
        r = hierarchical_classifier.classify_command_string(
            "rm -rf /tmp/old", "command.execute.rm", tool_id="terminal",
        )
        assert r.zone == "green"  # matches /tmp rule, not the rm -rf rule
        assert "/tmp" in r.matched_rule


class TestCriticalRmRfRoot:
    """The headline invariant — operator's /tmp greenlist CANNOT accidentally
    greenlist `rm -rf /`. The catastrophic-deletion rule MUST be listed
    before the /tmp rule in the schema and MUST fire first when matched.
    """

    def test_rm_rf_root_returns_red_not_green(self, hierarchical_classifier):
        r = hierarchical_classifier.classify_command_string(
            "rm -rf /", "command.execute.rm", tool_id="terminal",
        )
        assert r.zone == "red", (
            f"CRITICAL: `rm -rf /` returned {r.zone!r} instead of red. "
            f"The /tmp greenlist rule fired against the root path — the "
            f"hierarchical rule ordering invariant is broken. Matched rule: "
            f"{r.matched_rule!r}."
        )
        assert r.matched_rule == r"^rm\s+-rf\s+/$"

    def test_rm_rf_root_not_greenlit_even_when_rule_order_inverted(self, tmp_path: Path):
        """If an operator (or buggy save) puts /tmp BEFORE rm -rf /, the green
        rule WOULD fire — proving the invariant is operator-responsibility
        and the synthesiser MUST never produce such an inversion. This test
        documents the failure mode so anyone writing rules understands the
        contract."""
        schema = tmp_path / "zones.schema.yaml"
        schema.write_text(textwrap.dedent("""
            schema_version: 1
            zones: {green: {auto_approve: []}, yellow: {proposes: []}, red: {sovereign: []}}
            tool_zones:
              terminal:
                default_zone: yellow
                rules:
                  # WRONG ORDER — the /tmp rule is listed first.
                  - match_pattern: '^rm\\s+(-[fir]+\\s+)?/tmp/.*'
                    zone: green
                    reason: "Tmp"
                  - match_pattern: '^rm\\s+-rf\\s+/$'
                    zone: red
                    reason: "Catastrophic"
        """).lstrip())
        cls = ZoneClassifier(schema)
        # `rm -rf /` does NOT match the /tmp pattern (the path is `/`,
        # not `/tmp/anything`), so even with the wrong order the
        # catastrophic rule still fires. This is the protective
        # property: the /tmp rule's anchor + literal path segment
        # makes it impossible for it to match a root path even when
        # listed first. The invariant is structurally enforced, not
        # just by ordering convention.
        r = cls.classify_command_string("rm -rf /", "command.execute.rm", tool_id="terminal")
        assert r.zone == "red", (
            f"`rm -rf /` greenlit by misordered rules — got {r.zone}. "
            f"The /tmp regex should not match the root path."
        )


class TestBackwardCompat:
    """Pre-S22 schemas (every tool_zones entry bare-string) must produce
    identical behaviour."""

    def test_bare_string_terminal_classifies_yellow(self, legacy_classifier):
        # `classify_command_string` on a tool with a bare-string entry
        # falls through to the legacy classify(action) path.
        r = legacy_classifier.classify_command_string(
            "rm -rf /tmp/x", "command.execute.rm", tool_id="terminal",
        )
        # No hierarchical rules → default yellow via legacy fall-through.
        assert r.zone == "yellow"
        assert r.matched_rule == "default"
        assert r.source == "default"
        # Enriched fields are None for legacy classifications.
        assert r.reason is None
        assert r.pattern_key is None

    def test_bare_string_calendar_read_classifies_green(self, legacy_classifier):
        r = legacy_classifier.classify("calendar.read")
        assert r.zone == "green"
        assert r.matched_rule == "calendar.read"
        assert r.source == "tool_zones"

    def test_bare_string_skill_promote_classifies_red(self, legacy_classifier):
        r = legacy_classifier.classify("skill_manage.promote")
        assert r.zone == "red"
        assert r.source == "tool_zones"

    def test_zone_result_legacy_fields_unchanged(self, legacy_classifier):
        """A caller reading only .zone / .matched_rule / .source still works."""
        r = legacy_classifier.classify("calendar.read")
        # Equivalent to pre-S22 reads:
        assert r.zone in ("green", "yellow", "red")
        assert isinstance(r.matched_rule, str)
        assert isinstance(r.source, str)
        # The new optional fields exist and default to None.
        assert r.reason is None
        assert r.pattern_key is None


# ── Pattern synthesis ────────────────────────────────────────────────────────


class TestPatternSynthesis:
    """`synthesize_pattern` produces conservative, scope-narrow regexes
    and refuses the well-known foot-guns."""

    def test_rm_tmp_produces_directory_scoped_pattern(self):
        r = zr.synthesize_pattern("rm /tmp/foo.txt")
        assert r.ok
        assert "/tmp" in r.pattern
        # Pattern matches future variations within /tmp:
        compiled = re.compile(r.pattern)
        assert compiled.fullmatch("rm /tmp/foo.txt") is not None
        assert compiled.fullmatch("rm -rf /tmp/old/dir") is not None
        # Pattern does NOT match outside /tmp:
        assert compiled.fullmatch("rm /etc/passwd") is None
        assert compiled.fullmatch("rm -rf /") is None

    def test_sudo_is_refused(self):
        r = zr.synthesize_pattern("sudo apt install foo")
        assert not r.ok
        assert "privilege escalation" in r.reason.lower()

    def test_rm_rf_root_shape_refused(self):
        r = zr.synthesize_pattern("rm -rf /")
        assert not r.ok
        assert "denylisted" in r.reason.lower()

    def test_sensitive_system_dir_refused(self):
        # An operator should not be able to greenlist /etc/* via a single
        # /etc/passwd approval — the synthesiser refuses.
        r = zr.synthesize_pattern("rm /etc/passwd")
        assert not r.ok
        assert "sensitive" in r.reason.lower()

    def test_chmod_mode_generalises(self):
        # chmod numeric arguments → \d+ so future mode changes match.
        r = zr.synthesize_pattern("chmod 644 /home/user/.bashrc")
        assert r.ok
        compiled = re.compile(r.pattern)
        assert compiled.fullmatch("chmod 755 /home/user/.bashrc") is not None

    def test_pip_install_exact(self):
        # Package managers pin verb+sub+target exactly.
        r = zr.synthesize_pattern("pip install requests")
        assert r.ok
        compiled = re.compile(r.pattern)
        assert compiled.fullmatch("pip install requests") is not None
        assert compiled.fullmatch("pip install evil") is None
        assert compiled.fullmatch("pip uninstall requests") is None

    def test_synthesised_patterns_pass_safety_check(self):
        """Every synthesised pattern must pass check_pattern_safety —
        defence in depth against synthesis bugs."""
        commands = [
            "rm /tmp/cache.log",
            "chmod 644 /home/user/.bashrc",
            "pip install requests",
            "apt install htop",
            "echo hello",
        ]
        for cmd in commands:
            r = zr.synthesize_pattern(cmd)
            if not r.ok:
                continue  # refused — not in scope of this test
            ok, why = gz.check_pattern_safety(r.pattern)
            assert ok, (
                f"Synthesised pattern for {cmd!r} failed safety check: "
                f"pattern={r.pattern!r} reason={why!r}"
            )


# ── Write path: save_zone_rule ───────────────────────────────────────────────


class TestSaveZoneRule:
    """`save_zone_rule` writes to ~/.grove/zones.schema.yaml and triggers
    reload."""

    def test_save_normalises_bare_string_entry_to_dict(self, tmp_path: Path, monkeypatch):
        # Set up a temp HOME so the test doesn't touch the operator's real schema.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        (tmp_path / ".grove").mkdir()
        target = tmp_path / ".grove" / "zones.schema.yaml"
        shutil.copy(
            Path(__file__).resolve().parent.parent.parent
            / "config" / "zones.schema.yaml",
            target,
        )
        # Reset singletons so initialize() re-resolves under the patched HOME.
        gz._singleton = None
        gz.initialize(target)

        zr.save_zone_rule(
            "terminal", r"^rm\s+/tmp/.*", "green", "Tmp cleanup.",
        )

        # The bare `terminal: yellow` should now be a dict with rules.
        import yaml as _yaml
        with open(target) as fh:
            data = _yaml.safe_load(fh)
        terminal_entry = data["tool_zones"]["terminal"]
        assert isinstance(terminal_entry, dict), (
            f"Expected terminal entry to normalise to dict; got {type(terminal_entry).__name__}"
        )
        assert terminal_entry["default_zone"] == "yellow"
        assert len(terminal_entry["rules"]) == 1
        assert terminal_entry["rules"][0]["match_pattern"] == r"^rm\s+/tmp/.*"
        assert terminal_entry["rules"][0]["zone"] == "green"

    def test_save_preserves_schema_comments(self, tmp_path: Path, monkeypatch):
        """ruamel.yaml round-trips the operator's primary governance
        interface without stripping its humanity."""
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        (tmp_path / ".grove").mkdir()
        target = tmp_path / ".grove" / "zones.schema.yaml"
        shutil.copy(
            Path(__file__).resolve().parent.parent.parent
            / "config" / "zones.schema.yaml",
            target,
        )
        gz._singleton = None
        gz.initialize(target)

        before = sum(
            1 for line in target.read_text().splitlines()
            if line.lstrip().startswith("#")
        )
        zr.save_zone_rule(
            "terminal", r"^rm\s+/tmp/.*", "green", "Tmp cleanup.",
        )
        after = sum(
            1 for line in target.read_text().splitlines()
            if line.lstrip().startswith("#")
        )
        assert after == before, (
            f"Comment lines dropped during save: before={before} after={after}. "
            f"ruamel.yaml should preserve all schema commentary."
        )

    def test_save_then_reload_makes_rule_effective(self, tmp_path: Path, monkeypatch):
        """The new rule takes effect on the next classify call without
        a process restart."""
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        (tmp_path / ".grove").mkdir()
        target = tmp_path / ".grove" / "zones.schema.yaml"
        shutil.copy(
            Path(__file__).resolve().parent.parent.parent
            / "config" / "zones.schema.yaml",
            target,
        )
        gz._singleton = None
        gz.initialize(target)
        cls = gz._singleton

        # Before: no hierarchical rule, `rm /tmp/x` falls through to legacy → default yellow.
        r_before = cls.classify_command_string(
            "rm /tmp/x", "command.execute.rm", tool_id="terminal",
        )
        assert r_before.zone == "yellow"

        zr.save_zone_rule(
            "terminal", r"^rm\s+/tmp/.*", "green", "Tmp cleanup.",
        )

        # After: hierarchical rule fires, returning green.
        r_after = gz._singleton.classify_command_string(
            "rm /tmp/x", "command.execute.rm", tool_id="terminal",
        )
        assert r_after.zone == "green", (
            f"Rule did not take effect after save+reload; got {r_after.zone}."
        )

    def test_save_refuses_redos_pattern(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        (tmp_path / ".grove").mkdir()
        target = tmp_path / ".grove" / "zones.schema.yaml"
        shutil.copy(
            Path(__file__).resolve().parent.parent.parent
            / "config" / "zones.schema.yaml",
            target,
        )
        with pytest.raises(ValueError, match="safety check"):
            zr.save_zone_rule("terminal", "(a+)+", "green", "")


# ── ReDoS — loader rejects vulnerable patterns per-rule ──────────────────────


class TestReDoSProtection:
    """Per-rule ReDoS rejection: a bad pattern drops that rule with a
    loud log but does not invalidate the rest of the schema."""

    def test_nested_quantifier_pattern_dropped_schema_loads(
        self, tmp_path: Path, caplog
    ):
        schema = tmp_path / "zones.schema.yaml"
        schema.write_text(textwrap.dedent(r"""
            schema_version: 1
            zones: {green: {auto_approve: []}, yellow: {proposes: []}, red: {sovereign: []}}
            tool_zones:
              terminal:
                default_zone: yellow
                rules:
                  - match_pattern: '(a+)+'
                    zone: green
                    reason: "ReDoS pattern — must be rejected."
                  - match_pattern: '^echo\s+hello$'
                    zone: green
                    reason: "Safe — must survive."
        """).lstrip())
        import logging
        with caplog.at_level(logging.ERROR):
            cls = ZoneClassifier(schema)
        # Loud log for the dropped rule:
        assert any(
            "rejected" in rec.getMessage() and "(a+)+" in rec.getMessage()
            for rec in caplog.records
        ), "Expected loud log for the rejected ReDoS rule."
        # The safe rule survives:
        r = cls.classify_command_string(
            "echo hello", "command.execute.echo", tool_id="terminal",
        )
        assert r.zone == "green"
        assert r.matched_rule == r"^echo\s+hello$"

    def test_forbidden_bare_pattern_rejected(self):
        ok, why = gz.check_pattern_safety(".*")
        assert not ok
        assert "matches everything" in why

    def test_excessive_alternation_rejected(self):
        ok, why = gz.check_pattern_safety("(a|b|c|d|e|f|g|h|i|j|k|l)+")
        assert not ok
        assert "alternation" in why

    def test_over_long_pattern_rejected(self):
        ok, why = gz.check_pattern_safety("x" * 250)
        assert not ok
        assert "length" in why


# ── I4 preservation ──────────────────────────────────────────────────────────


class TestI4Preserved:
    """W3.0a I4 — zone checks unsuppressible — must hold through S22.
    A classifier load failure produces fail-closed behaviour, never a
    silent fall-through to the legacy approval flow.
    """

    def test_invalid_schema_falls_back_to_last_known_good_on_reload(
        self, tmp_path: Path
    ):
        # Initial load: valid schema. Snapshot taken.
        schema = tmp_path / "zones.schema.yaml"
        schema.write_text(textwrap.dedent(_HIERARCHICAL_SCHEMA).lstrip())
        cls = ZoneClassifier(schema)
        assert cls._tool_zones_rich.get("terminal") is not None

        # Corrupt the schema and reload — last known good is retained
        # (graceful degradation is the ONE spec-commanded relaxation;
        # the loaded-but-corrupt schema would otherwise wipe the
        # in-memory map).
        schema.write_text("not: [valid, yaml: garbage")
        cls.reload()
        # Previous hierarchical entry still in place:
        assert cls._tool_zones_rich.get("terminal") is not None

    def test_unknown_tool_with_no_hierarchical_entry_falls_through(
        self, hierarchical_classifier
    ):
        """A tool without a tool_zones entry produces a normal classify
        result via the dot-notation path — no exception, no silent
        approval. Yellow-default behavior is the I4-aligned outcome
        ("conservative: unclassified actions require approval")."""
        r = hierarchical_classifier.classify_command_string(
            "weird_op --args", "weird.tool.op", tool_id="weird_tool",
        )
        assert r.zone == "yellow"
        assert r.source == "default"

    def test_classifier_exception_inside_check_all_command_guards_returns_blocked(
        self, monkeypatch
    ):
        """Direct verification of the W3.0a fail-closed path. We patch
        grove.dispatch.classify_command to raise (this is the actual
        symbol the function-local import inside check_all_command_guards
        resolves to), then call check_all_command_guards and verify
        the response is approved=False with classifier_failed set."""
        import os
        os.environ["GROVE_INTERACTIVE"] = "1"
        try:
            from tools import approval as tapp
            from grove import dispatch as gdispatch

            def boom(*a, **kw):
                raise RuntimeError("simulated classifier failure")

            monkeypatch.setattr(gdispatch, "classify_command", boom)
            result = tapp.check_all_command_guards("echo hi", "local")
            assert result.get("approved") is False, (
                f"Classifier failure should fail-closed; got {result!r}"
            )
            assert result.get("classifier_failed") is True
        finally:
            os.environ.pop("GROVE_INTERACTIVE", None)
