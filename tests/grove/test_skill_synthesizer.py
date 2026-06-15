"""Sprint 63 — kaizen-pattern-synthesizer.

Covers the three deliverables:

* §1 invoke_skill — the tool handler's skill resolution, and the Dispatcher
  hooks that gate an invoke_skill of a quarantined skill (Yellow zone +
  the post-execution quarantine flag) and materialize an accepted synthesized
  skill into the quarantine before classification.
* §2 synthesis engine — ``detect_skill_candidates`` (tool-sequence grouping,
  stem-match prompt recovery, correction/threshold filters) and the
  synthesizer's structural check + staging.
* §3 quiet append — the concierge-register offer surfaced once per session.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from grove.intent_store import IntentRecord, IntentStore, normalize_message_stem


# ── shared helpers ───────────────────────────────────────────────────────


def _rec(
    *,
    session_id: str,
    turn_id: str,
    tools_yielded: tuple,
    stem: str,
    outcome: str = "success",
    timestamp: str | None = None,
) -> IntentRecord:
    return IntentRecord(
        timestamp=timestamp or datetime.now(timezone.utc).isoformat(),
        session_id=session_id,
        turn_id=turn_id,
        user_message_stem=stem,
        pattern_hash="ph",
        intent_class="code_generation",
        register_class="technical",
        complexity_signal="moderate",
        confidence=0.9,
        outcome=outcome,
        tools_yielded=tools_yielded,
    )


class _FakeSessionDB:
    """Minimal stand-in exposing get_messages(session_id)."""

    def __init__(self, by_session: dict) -> None:
        self._by_session = by_session

    def get_messages(self, session_id: str):
        return self._by_session.get(session_id, [])


def _user_msgs(*texts: str) -> list:
    return [{"role": "user", "content": t} for t in texts]


@pytest.fixture
def tmp_store(tmp_path: Path) -> IntentStore:
    return IntentStore(store_path=tmp_path / "records.jsonl")


_VALID_SKILL_MD = (
    "---\n"
    "name: prep-meeting-brief\n"
    "description: Pull a GitHub repo's recent activity before a meeting.\n"
    "---\n"
    "## When to use\n"
    "Before a meeting about {repo}.\n\n"
    "## Procedure\n"
    "1. Fetch {repo} activity.\n"
    "2. Summarize it.\n"
)


# ── §2 detect_skill_candidates ───────────────────────────────────────────


class TestDetectSkillCandidates:
    def test_groups_sequence_and_recovers_prompt(self, tmp_store):
        from grove.kaizen.detector import IntentPatternDetector

        prompts = {
            "s1": "check the acme/widgets repo before my standup",
            "s2": "look at acme/gizmos repo before the review",
            "s3": "pull acme/sprockets repo before the sync",
        }
        for i, (sid, text) in enumerate(prompts.items()):
            tmp_store.append(_rec(
                session_id=sid, turn_id=f"{sid}#1",
                tools_yielded=("github_fetch", "summarize"),
                stem=normalize_message_stem(text),
            ))
        db = _FakeSessionDB({sid: _user_msgs(t) for sid, t in prompts.items()})

        out = IntentPatternDetector(tmp_store).detect_skill_candidates(
            n=3, m=2, session_db=db,
        )
        assert len(out) == 1
        cand = out[0]
        assert cand["tool_sequence"] == ("github_fetch", "summarize")
        assert cand["count"] == 3
        assert cand["session_count"] == 3
        assert set(cand["prompts"]) == set(prompts.values())

    def test_single_tool_sequence_ignored(self, tmp_store):
        from grove.kaizen.detector import IntentPatternDetector

        for i in range(3):
            tmp_store.append(_rec(
                session_id=f"s{i}", turn_id=f"s{i}#1",
                tools_yielded=("only_one",), stem="x",
            ))
        db = _FakeSessionDB({})
        assert IntentPatternDetector(tmp_store).detect_skill_candidates(
            session_db=db,
        ) == []

    def test_correction_disqualifies_sequence(self, tmp_store):
        from grove.kaizen.detector import IntentPatternDetector

        texts = {"s0": "do the thing now", "s1": "do the thing again",
                 "s2": "do the thing once more"}
        for i, (sid, t) in enumerate(texts.items()):
            tmp_store.append(_rec(
                session_id=sid, turn_id=f"{sid}#1",
                tools_yielded=("a", "b"), stem=normalize_message_stem(t),
                outcome="correction" if i == 0 else "success",
            ))
        db = _FakeSessionDB({sid: _user_msgs(t) for sid, t in texts.items()})
        assert IntentPatternDetector(tmp_store).detect_skill_candidates(
            n=3, m=2, session_db=db,
        ) == []

    def test_unrecoverable_prompt_drops_candidate(self, tmp_store):
        from grove.kaizen.detector import IntentPatternDetector

        for i in range(3):
            tmp_store.append(_rec(
                session_id=f"s{i}", turn_id=f"s{i}#1",
                tools_yielded=("a", "b"), stem="a stem that matches nothing",
            ))
        # Session DB has messages, but none whose stem matches.
        db = _FakeSessionDB({f"s{i}": _user_msgs("unrelated text") for i in range(3)})
        assert IntentPatternDetector(tmp_store).detect_skill_candidates(
            n=3, m=2, session_db=db,
        ) == []

    def test_below_session_threshold_dropped(self, tmp_store):
        from grove.kaizen.detector import IntentPatternDetector

        # 3 occurrences but all in ONE session → session_count 1 < m=2.
        for i in range(3):
            t = f"same single session prompt {i}"
            tmp_store.append(_rec(
                session_id="solo", turn_id=f"solo#{i}",
                tools_yielded=("a", "b"), stem=normalize_message_stem(t),
            ))
        db = _FakeSessionDB({"solo": _user_msgs(
            *[f"same single session prompt {i}" for i in range(3)]
        )})
        assert IntentPatternDetector(tmp_store).detect_skill_candidates(
            n=3, m=2, session_db=db,
        ) == []


# ── §2 synthesizer structural check + staging ────────────────────────────


class TestSynthesizerValidationAndStaging:
    def test_structural_check_accepts_valid(self):
        from grove.kaizen.synthesizer import _structural_check
        ok, _ = _structural_check(_VALID_SKILL_MD)
        assert ok

    def test_structural_check_rejects_missing_when_to_use(self):
        from grove.kaizen.synthesizer import _structural_check
        bad = (
            "---\nname: x\ndescription: y\n---\n## Procedure\n1. do it\n"
        )
        ok, reason = _structural_check(bad)
        assert not ok and "when to use" in reason.lower()

    def test_structural_check_rejects_unparseable_frontmatter(self):
        from grove.kaizen.synthesizer import _structural_check
        ok, _ = _structural_check("no frontmatter here")
        assert not ok

    def test_stage_proposal_appends_skill_synthesis(self):
        from grove.kaizen.synthesizer import stage_proposal
        from grove.eval.proposal_queue import (
            PROPOSAL_TYPE_SKILL_SYNTHESIS, read_all,
        )

        candidate = {
            "tool_sequence": ("github_fetch", "summarize"),
            "evidence_turns": ["s1#1", "s2#1"],
        }
        pid = stage_proposal(candidate, _VALID_SKILL_MD)
        assert pid is not None
        queued = read_all()
        assert len(queued) == 1
        p = queued[0]
        assert p.type == PROPOSAL_TYPE_SKILL_SYNTHESIS
        assert p.payload["skill_name"] == "prep-meeting-brief"
        assert p.payload["skill_md"] == _VALID_SKILL_MD
        assert p.payload["goal"]
        assert p.payload["tool_sequence"] == ["github_fetch", "summarize"]

    def test_stage_proposal_is_idempotent(self):
        from grove.kaizen.synthesizer import stage_proposal
        from grove.eval.proposal_queue import read_all

        candidate = {"tool_sequence": ("a", "b"), "evidence_turns": ["t#1"]}
        first = stage_proposal(candidate, _VALID_SKILL_MD)
        second = stage_proposal(candidate, _VALID_SKILL_MD)
        assert first is not None and second is None
        assert len(read_all()) == 1

    def test_run_synthesis_pass_stages_validated_candidate(self, monkeypatch):
        import grove.kaizen.synthesizer as syn
        from grove.eval.proposal_queue import read_all

        candidate = {
            "tool_sequence": ("a", "b"), "evidence_turns": ["t#1"],
            "prompts": ["do a then b"],
        }
        fake_detector = SimpleNamespace(
            detect_skill_candidates=lambda **kw: [candidate]
        )
        monkeypatch.setattr(syn, "_resolve_t3_runtime", lambda: {"model": "m"})
        monkeypatch.setattr(
            syn, "synthesize_skill_md", lambda c, runtime=None: _VALID_SKILL_MD
        )
        monkeypatch.setattr(
            syn, "validate_skill_md", lambda md, runtime=None: (True, "ok")
        )
        staged = syn.run_synthesis_pass(detector=fake_detector)
        assert staged == 1
        assert len(read_all()) == 1

    def test_run_synthesis_pass_skips_invalid(self, monkeypatch):
        import grove.kaizen.synthesizer as syn
        from grove.eval.proposal_queue import read_all

        candidate = {"tool_sequence": ("a", "b"), "evidence_turns": ["t#1"],
                     "prompts": ["p"]}
        fake_detector = SimpleNamespace(
            detect_skill_candidates=lambda **kw: [candidate]
        )
        monkeypatch.setattr(syn, "_resolve_t3_runtime", lambda: {"model": "m"})
        monkeypatch.setattr(
            syn, "synthesize_skill_md", lambda c, runtime=None: _VALID_SKILL_MD
        )
        monkeypatch.setattr(
            syn, "validate_skill_md",
            lambda md, runtime=None: (False, "unsafe"),
        )
        assert syn.run_synthesis_pass(detector=fake_detector) == 0
        assert read_all() == []


# ── §1 invoke_skill tool handler ─────────────────────────────────────────


class TestInvokeSkillTool:
    def _write_active(self, name: str, body: str = "# body") -> None:
        from grove.skills import active_path
        d = active_path(name)
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(body, encoding="utf-8")

    def test_resolves_active_skill_green(self):
        import json
        from tools.invoke_skill_tool import invoke_skill
        self._write_active("promoted-one", "# promoted body")
        out = json.loads(invoke_skill("promoted-one"))
        assert out["success"] and out["zone"] == "green"
        assert "promoted body" in out["content"]

    def test_resolves_quarantined_skill_yellow(self):
        import json
        from grove.skills import write_proposal
        from tools.invoke_skill_tool import invoke_skill
        write_proposal("draft-one", _VALID_SKILL_MD)
        out = json.loads(invoke_skill("draft-one"))
        assert out["success"] and out["zone"] == "yellow"

    def test_unknown_skill_fails_loud(self):
        import json
        from tools.invoke_skill_tool import invoke_skill
        out = json.loads(invoke_skill("nope-not-here"))
        assert out["success"] is False

    def test_registered_under_skills_toolset(self):
        from tools.invoke_skill_tool import register, INVOKE_SKILL_SCHEMA
        captured = {}

        class _Reg:
            def register(self, **kw):
                captured.update(kw)

        register(_Reg())
        assert captured["name"] == "invoke_skill"
        assert captured["toolset"] == "skills"
        assert INVOKE_SKILL_SCHEMA["name"] == "invoke_skill"


# ── §1 Dispatcher hooks ──────────────────────────────────────────────────


class TestDispatcherInvokeSkillHooks:
    def test_classify_invoke_skill_quarantined_yellow(self):
        from grove import dispatch as _grove_dispatch
        from grove.dispatcher import Dispatcher
        from grove.intents import ToolIntent
        from grove.skills import write_proposal

        write_proposal("gated-skill", _VALID_SKILL_MD)
        intent = ToolIntent(
            tool_name="invoke_skill", arguments={"name": "gated-skill"},
            call_id="c1",
        )
        zr = Dispatcher._classify_one_intent(intent, _grove_dispatch)
        assert zr.zone == "yellow"
        assert zr.source == "invoke_skill_quarantine"
        assert ".andon" in zr.matched_rule

    def test_classify_invoke_skill_unknown_not_yellow(self):
        import grove.zones as _zones
        from grove import dispatch as _grove_dispatch
        from grove.dispatcher import Dispatcher
        from grove.intents import ToolIntent

        # Unknown (non-quarantined) invoke_skill falls through to the generic
        # classifier, which needs the zones singleton initialized.
        _zones.initialize()
        intent = ToolIntent(
            tool_name="invoke_skill", arguments={"name": "ghost"},
            call_id="c1",
        )
        zr = Dispatcher._classify_one_intent(intent, _grove_dispatch)
        # Not quarantined → falls through to generic classification (not the
        # invoke_skill_quarantine source).
        assert zr.source != "invoke_skill_quarantine"

    def test_flag_quarantine_execution_for_invoke_skill(self):
        from grove.dispatcher import AndonHalt, Dispatcher
        from grove.intents import ToolIntent
        from grove.zones import ZoneResult

        d = Dispatcher()
        d._current_turn_id = "s#1"
        intent = ToolIntent(
            tool_name="invoke_skill", arguments={"name": "promo-me"},
            call_id="c1",
        )
        halt = AndonHalt(
            intents=[intent],
            zone_results=[ZoneResult(
                zone="yellow",
                matched_rule="skill.quarantine.andon (.grove/skills/.andon/promo-me)",
                source="invoke_skill_quarantine",
            )],
            triggering_index=0,
        )
        d._maybe_flag_quarantine_execution(
            intent, halt, "once", ("invoke_skill", "h"), None,
        )
        flag = d._quarantine_skill_executed_this_turn
        assert flag is not None and flag["skill_name"] == "promo-me"

    def test_skill_synthesis_materializes_through_flywheel_approve(self):
        """B1 (Fork B) — the ONE door. A staged skill_synthesis draft becomes a
        proposed (.andon/) record + a minted capability record ONLY by the
        operator approving its proposal through the flywheel gate. The retired
        invoke_skill-triggered dispatcher materialization no longer exists."""
        from grove import flywheel_cli
        from grove.capability_registry import skill_record_id_for_name
        from grove.eval.proposal_queue import read_all
        from grove.kaizen.synthesizer import stage_proposal
        from grove.skills import proposal_path

        # Stage a synthesized proposal whose skill is NOT yet on disk.
        pid = stage_proposal(
            {"tool_sequence": ("a", "b"), "evidence_turns": ["t#1"]},
            _VALID_SKILL_MD,
        )
        skill_name = "prep-meeting-brief"
        assert not proposal_path(skill_name).exists()

        # The one door: flywheel approve.
        rc = flywheel_cli.cli_approve(pid)
        assert rc == 0

        # Materialized into quarantine with the exact draft body.
        assert proposal_path(skill_name).exists()
        assert (proposal_path(skill_name) / "SKILL.md").read_text(
            encoding="utf-8"
        ) == _VALID_SKILL_MD
        # The state:proposed (non-executable) record was minted.
        assert skill_record_id_for_name(skill_name) is not None
        # The proposal is consumed from the queue — one approval, one effect.
        assert read_all() == []


# ── §3 quiet append ──────────────────────────────────────────────────────


class TestQuietAppend:
    def test_appends_offer_once_per_session(self):
        from run_agent import AIAgent
        from grove.kaizen.synthesizer import stage_proposal

        stage_proposal(
            {"tool_sequence": ("a", "b"), "evidence_turns": ["t#1"]},
            _VALID_SKILL_MD,
        )
        fake_self = SimpleNamespace()
        first = AIAgent._append_pending_skill_proposal(fake_self, "Done.")
        assert "want to try it?" in first
        assert first.startswith("Done.")
        # Same proposal is not re-offered within the session.
        second = AIAgent._append_pending_skill_proposal(fake_self, "Done.")
        assert second == "Done."

    def test_no_pending_leaves_response_untouched(self):
        from run_agent import AIAgent
        fake_self = SimpleNamespace()
        assert AIAgent._append_pending_skill_proposal(fake_self, "Hi.") == "Hi."
