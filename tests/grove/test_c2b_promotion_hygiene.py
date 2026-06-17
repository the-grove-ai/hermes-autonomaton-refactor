"""GRV-010 C2b — promotion modeling consistency + minter provenance + §V ratchet.

Three cohesive threads:
1. .andon modeling consistency — literals replaced by ANDON_DIRNAME; _find_skill
   resolves quarantined skills via STRICT TWO-PASS PRECEDENCE (active first,
   .andon only on miss) so a quarantined copy never shadows the live executable.
2. Minter provenance — the operator-only minters write a bare-CLI-safe
   sovereignty_decision (actor=operator/CLI); dead ingest_pre_faucet_skill is gone.
3. §V ratchet — a quarantine TerminalGovernanceHalt carries the promote target;
   the surface offers an operator-only 1-tap promote (never agent-triggerable).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from grove.governance_halt import (
    GovernanceHaltContext,
    TerminalGovernanceHalt,
    operator_promote_quarantined,
    terminal_halt_result,
)
from grove.intents import ToolIntent
from grove.skills import ANDON_DIRNAME, ARCHIVE_DIRNAME


# ══════════════════════════════════════════════════════════════════════
# Thread 1 — .andon modeling consistency
# ══════════════════════════════════════════════════════════════════════


class TestAndonConsistency:
    def test_excluded_dirs_use_constants(self):
        from agent.skill_utils import EXCLUDED_SKILL_DIRS
        # The set still excludes quarantine + archive — sourced from constants.
        assert ANDON_DIRNAME in EXCLUDED_SKILL_DIRS
        assert ARCHIVE_DIRNAME in EXCLUDED_SKILL_DIRS

    def test_andon_skill_regex_matches_quarantine_path(self):
        from grove.dispatcher import _ANDON_SKILL_RE
        m = _ANDON_SKILL_RE.search(
            "bash ~/.grove/skills/.andon/my-skill/run.sh"
        )
        assert m is not None
        assert m.group("name") == "my-skill"
        assert m.group("path").endswith(".andon/my-skill")


# ══════════════════════════════════════════════════════════════════════
# Thread 1 — _find_skill strict two-pass precedence
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def skills_root(tmp_path, monkeypatch):
    root = tmp_path / ".grove" / "skills"
    root.mkdir(parents=True)
    import agent.skill_utils as su
    monkeypatch.setattr(su, "get_skills_dir", lambda: root)
    monkeypatch.setattr(su, "get_external_skills_dirs", lambda: [])
    return root


def _plant(root: Path, *parts: str, name: str) -> Path:
    d = root.joinpath(*parts, name)
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: x\n---\nbody\n")
    return d


class TestFindSkillTwoPass:
    def test_active_skill_resolves(self, skills_root):
        from tools.skill_manager_tool import _find_skill
        active = _plant(skills_root, name="alpha")
        got = _find_skill("alpha")
        assert got is not None and got["path"] == active

    def test_quarantined_skill_resolves_on_active_miss(self, skills_root):
        from tools.skill_manager_tool import _find_skill
        q = _plant(skills_root, ANDON_DIRNAME, name="beta")
        got = _find_skill("beta")
        assert got is not None and got["path"] == q

    def test_active_wins_no_shadowing(self, skills_root):
        """The decisive invariant: when BOTH an active and an .andon copy of the
        same name exist, the live active executable is returned — the quarantine
        copy being Kaizen-edited never shadows it."""
        from tools.skill_manager_tool import _find_skill
        active = _plant(skills_root, name="gamma")
        _plant(skills_root, ANDON_DIRNAME, name="gamma")  # quarantine copy too
        got = _find_skill("gamma")
        assert got is not None
        assert got["path"] == active
        assert ANDON_DIRNAME not in got["path"].parts

    def test_missing_skill_returns_none(self, skills_root):
        from tools.skill_manager_tool import _find_skill
        assert _find_skill("nope") is None


# ══════════════════════════════════════════════════════════════════════
# Thread 2 — minter provenance + ingest deletion
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def grove_home(tmp_path, monkeypatch):
    home = tmp_path / ".grove"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("GROVE_HOME", str(home))
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: home)
    return home


class TestMinterProvenance:
    def test_register_installed_skill_logs_operator_cli_provenance(
        self, grove_home, caplog,
    ):
        from grove import capability_registry as reg
        body = "---\nname: provtest\ndescription: x\n---\nbody\n"
        with caplog.at_level(logging.INFO, logger="grove.telemetry"):
            path = reg.register_installed_skill("provtest", "misc", body)
        assert path is not None
        # A bare CLI context (no SessionDB / turn) wrote a sovereignty_decision.
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "sovereignty_decision" in joined
        assert "operator/CLI" in joined
        assert "skill_record_minted" in joined

    def test_register_installed_skill_dedup_does_not_double_log(
        self, grove_home, caplog,
    ):
        from grove import capability_registry as reg
        body = "---\nname: dedupt\ndescription: x\n---\nbody\n"
        reg.register_installed_skill("dedupt", "misc", body)
        with caplog.at_level(logging.INFO, logger="grove.telemetry"):
            second = reg.register_installed_skill("dedupt", "misc", body)
        # Idempotent: a dedup no-op returns None and writes no new provenance.
        assert second is None
        assert "skill_record_minted" not in " ".join(
            r.getMessage() for r in caplog.records
        )

    def test_ingest_pre_faucet_skill_is_deleted(self):
        import grove.capability_registry as reg
        assert not hasattr(reg, "ingest_pre_faucet_skill")
        assert "ingest_pre_faucet_skill" not in getattr(reg, "__all__", [])


# ══════════════════════════════════════════════════════════════════════
# Thread 3 — §V ratchet: payload target + operator-only promote
# ══════════════════════════════════════════════════════════════════════


class TestRatchetPayload:
    def test_quarantine_halt_carries_skill_target(self):
        halt = TerminalGovernanceHalt(
            GovernanceHaltContext(
                trigger="quarantine",
                tool_name="invoke_skill",
                skill_name="my-skill",
                skill_path="/home/x/.grove/skills/.andon/my-skill",
            )
        )
        assert halt.context.skill_name == "my-skill"

    def test_quarantine_surface_text_offers_named_promote(self):
        halt = TerminalGovernanceHalt(
            GovernanceHaltContext(trigger="quarantine", skill_name="my-skill")
        )
        text = halt.surface_text().lower()
        assert "promote 'my-skill'" in text
        # Still operator-register: no governance internals parroted.
        assert "andon" not in text and "zone" not in text

    def test_terminal_result_carries_promote_target(self):
        halt = TerminalGovernanceHalt(
            GovernanceHaltContext(trigger="quarantine", skill_name="my-skill")
        )
        r = terminal_halt_result(halt)
        assert r["governance_promote_target"] == "my-skill"
        assert r["governance_trigger"] == "quarantine"

    def test_non_quarantine_has_no_promote_target(self):
        halt = TerminalGovernanceHalt(
            GovernanceHaltContext(trigger="red_sovereign", tool_name="terminal")
        )
        r = terminal_halt_result(halt)
        assert "governance_promote_target" not in r

    def test_operator_promote_wraps_sovereignty_promote(self, monkeypatch):
        """The §V 1-tap is a thin wrapper over the operator-approved, ledgered
        sovereignty.promote — the agent never reaches it."""
        called = {}

        def _fake_promote(name, replace=False):
            called["args"] = (name, replace)
            return {"ok": True}

        import grove.sovereignty as sov
        monkeypatch.setattr(sov, "promote", _fake_promote)
        out = operator_promote_quarantined("my-skill")
        assert called["args"] == ("my-skill", False)
        assert out == {"ok": True}


class TestExtractQuarantineTarget:
    def _dispatcher(self):
        from grove.dispatcher import Dispatcher
        return Dispatcher()

    def test_terminal_command_target(self):
        d = self._dispatcher()
        intent = ToolIntent(
            tool_name="terminal",
            arguments={"command": "bash ~/.grove/skills/.andon/q-skill/run.sh"},
            call_id="c1",
        )
        name, path = d._extract_quarantine_target(intent)
        assert name == "q-skill"
        assert path.endswith(".andon/q-skill")

    def test_invoke_skill_target(self, tmp_path, monkeypatch):
        import hermes_constants
        monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
        d = self._dispatcher()
        intent = ToolIntent(
            tool_name="invoke_skill", arguments={"name": "proc-skill"}, call_id="c1",
        )
        name, path = d._extract_quarantine_target(intent)
        assert name == "proc-skill"
        assert ANDON_DIRNAME in path

    def test_non_quarantine_intent_returns_none(self):
        d = self._dispatcher()
        intent = ToolIntent(tool_name="memory", arguments={"key": "k"}, call_id="c1")
        assert d._extract_quarantine_target(intent) == (None, None)
