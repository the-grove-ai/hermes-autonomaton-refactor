"""Kaizen emits the helper keg and its replay into the cellar; a signature is a
second commit (compiler-shapes-v1, Task 4)."""

from __future__ import annotations

import collections
import json
import textwrap

import pytest
import yaml

import grove.pattern_cache as pc
from grove import keg
from grove.eval import proposal_queue
from grove.eval.pattern_compiler import backtest_keg, propose_keg
from grove.flywheel_cli import _sign_keg
from grove.kaizen_ledger import KaizenLedger
from grove.keg_store import KegRepo, content_hash, replay_lines, seed_skill
from grove.pattern_cache import PatternCacheStore

from tests.grove.test_keg import HISTORY, INPUTS, OUTPUTS, REQUEST, RULES_V1, _case

GOAL = "message-triage"


def _history_with_ids():
    cases = []
    for i, c in enumerate(HISTORY, 1):
        cases.append({**c, "decided_id": f"d{i}", "proposed_id": f"p{i}",
                      "note": "2 values in channels.csv" if c["ref"] == "m3" else ""})
    return cases


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    db = tmp_path / "pattern_cache.db"
    monkeypatch.setattr(pc, "default_pattern_cache_path", lambda: db)
    (tmp_path / "dock").mkdir()
    src = tmp_path / "SKILL.md"
    src.write_text("---\nname: message-triage\n---\n# Triage\n")
    seed_skill("message-triage", source=src)
    (tmp_path / "dock" / "dock.yaml").write_text(textwrap.dedent("""\
        version: "1.0"
        goals:
          - id: message-triage
            name: Tag every message
            vector: operational
            status: accelerating
            definition_of_done: tagged
            context_sources: []
            keywords: [message]
            unlocked_skills: [message-triage]
        """))

    class Home:
        root = tmp_path
        store = PatternCacheStore(db)
        queue = tmp_path / "proposals.jsonl"
        ledger_dir = tmp_path / ".kaizen_ledger"

        def ledger(self):
            return KaizenLedger("s1", ledger_dir=self.ledger_dir)

        def events(self, kind=None):
            out = []
            for f in sorted(self.ledger_dir.glob("*.jsonl")):
                out += [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
            return [e for e in out if kind is None or e["event_type"] == kind]

        def propose(self, rules=RULES_V1, *, lineage="run-1", evidence=("t1", "t2", "t3"),
                    decisions=("d1", "d2", "d4"), history=None):
            return propose_keg(
                self.store, name="Message tagging", request=REQUEST,
                intent_class="analysis", tool_name="tag_message",
                tool_args={"verb": "apply_keg"}, inputs=INPUTS, outputs=OUTPUTS,
                conditions=rules, scope_text="Tags messages by channel.",
                reserve="Channels outside the table; anything ambiguous.",
                dock_goal=GOAL, scope="reserved", authority_level="green",
                flag=keg.FLAG_TIER_DOWN_PATTERN, flag_detail="three confirmed",
                evidence_turn_ids=evidence, evidence_decisions=list(decisions),
                history=history if history is not None else _history_with_ids(),
                lineage=lineage, andon_id="andon-1", queue_path=self.queue,
                ledger=self.ledger(), now_iso="2026-11-03T18:00:00+00:00",
            )

        def sign(self, result, now="2026-11-03T18:05:00+00:00"):
            [proposal] = [p for p in proposal_queue.read_all(path=self.queue)
                          if p.proposal_id == result.proposal_id]
            self.store.set_status(result.pattern_id, pc.STATUS_ACTIVE, promoted_at=now)
            return _sign_keg(proposal, self.store, result.pattern_id,
                             proposal.payload["keg"], now)

        def repo(self):
            return KegRepo(self.root / "cellar" / "kegs" / "message-tagging")

    return Home()


def test_replay_lines_map_every_case_and_match_the_tallies():
    spec = {"inputs": INPUTS, "conditions": [{"if": "channel == 'billing'", "then": {"tag": "finance"}}]}
    history = [
        {**_case("m1", "billing", "finance", "finance"), "decided_id": "d1"},
        {**_case("m2", "outage", "ops", "ops"), "decided_id": "d2", "served_by_keg": True},
        {**_case("m3", "social", "other", "other"), "decided_id": "d3",
         "note": "2 values in channels.csv"},
    ]
    bt = backtest_keg(spec, history)
    lines = replay_lines(bt)
    assert [l["status"] for l in lines] == ["would_change", "reserved", "held"]
    assert collections.Counter(l["status"] for l in lines) == {
        "held": bt["unchanged"], "would_change": bt["would_change"], "reserved": bt["not_covered"]}
    assert lines[1] == {"item_id": "m3", "decided_id": "d3", "expected": {"tag": "other"},
                        "got": None, "status": "reserved", "reserve_reason": "2 values in channels.csv"}
    assert lines[2] == {"item_id": "m1", "decided_id": "d1", "expected": {"tag": "finance"},
                        "got": {"tag": "finance"}, "status": "held", "reserve_reason": None}


def test_propose_keg_commits_the_document_and_the_replay(home):
    result = home.propose()
    assert result.status == "proposed"
    repo = home.repo()
    m = repo.manifest()
    assert m["kind"] == "helper" and m["status"] == "proposed" and m["version"] == 1
    assert m["keg_id"] == result.pattern_id
    assert m["keg_id"].endswith(m["content_hash"][7:19])
    assert m["content_hash"] == content_hash(repo.read("keg.yaml"))
    doc = yaml.safe_load(repo.read("keg.yaml"))
    assert doc["conditions"] == RULES_V1 and doc["lineage"] == "run-1"
    assert doc["evidence"] == ["t1", "t2", "t3"] and doc["protocol"] == "GRV-004"
    lines = [json.loads(l) for l in repo.read(m["replay"]["file"]).decode().splitlines()]
    assert m["replay"]["file"] == "replay/v1.jsonl" and len(lines) == len(HISTORY)
    [event] = home.events("kaizen_proposal")
    tallies = (event["replayed"], event["unchanged"], event["would_change"], event["not_covered"])
    assert tallies == (len(lines), sum(l["status"] == "held" for l in lines),
                       sum(l["status"] == "would_change" for l in lines),
                       sum(l["status"] == "reserved" for l in lines))
    assert m["replay"] == {"file": "replay/v1.jsonl", "replayed": tallies[0], "held": tallies[1],
                           "would_change": tallies[2], "reserved": tallies[3]}
    assert m["compiled_under"]["skill_keg_id"].startswith("keg:message-triage-skill:v1:")
    assert set(m["compiled_under"]) == {"skill_keg_id", "skill_version", "skill_content_hash", "skill_commit"}
    assert m["compiled_by"]["kaizen"] == "grove.eval.pattern_compiler"
    assert m["evidence"] == ["d1", "d2", "d4"] and m["evidence_hash"].startswith("sha256:")
    assert m["goal"] == GOAL and m["proposal_id"] == result.proposal_id and m["replaces"] == []
    assert m["approved_signature"] == json.loads(home.store.get(result.pattern_id).compiled_invocation)["approved_signature"]
    [commit] = repo.log()
    assert commit["message"].startswith(f"propose v1: proposal {result.proposal_id}; replay ")
    assert "supersedes nothing" in commit["message"]


def test_the_same_rules_in_the_same_run_are_the_same_keg_and_a_new_run_is_not(home):
    first = home.propose()
    assert home.propose().status == "skipped_known"
    again = home.propose(lineage="run-2")
    assert again.status == "proposed" and again.pattern_id != first.pattern_id
    assert again.pattern_id.startswith("keg:message-tagging:v1:")


def test_signing_commits_the_manifest_as_active(home):
    v1 = home.propose()
    home.sign(v1)
    v2 = home.propose(rules=RULES_V1 + [{"if": "channel == 'social'", "then": {"tag": "other"}}],
                      evidence=("t1", "t2", "t3", "t9"))
    label, applied = home.sign(v2)
    repo = home.repo()
    m = repo.manifest()
    assert m["status"] == "active" and m["signed_by"] == "operator"
    assert m["signed_at"] == "2026-11-03T18:05:00+00:00" and m["proposal_id"] == v2.proposal_id
    assert m["replaces"] == [v1.pattern_id] and m["keg_id"] == v2.pattern_id
    messages = [e["message"] for e in repo.log()]
    assert len(messages) == 4
    assert messages[3].startswith(f"sign v2: proposal {v2.proposal_id}; signed_by operator; replay ")
    assert messages[3].endswith(f"supersedes {v1.pattern_id}")
    assert applied["new_standard_work"]["keg_commit"] == repo.head()
    assert home.store.get(v1.pattern_id).status == "superseded"


def test_history_cases_carry_the_decision_ids(tmp_path, monkeypatch):
    from grove import decision_work as dw
    from grove.decision_work import DecisionWork
    from tests.grove.test_work_session import SESSION, _goal

    monkeypatch.setattr(pc, "default_pattern_cache_path", lambda: tmp_path / "pc.db")
    cfg = dw.load_config(_goal(tmp_path, SESSION))
    work = DecisionWork(cfg)
    (tmp_path / "queue" / "m01.txt").write_text("billing")
    prov = {"session_id": "s", "turn_id": "s#1", "turn_uid": "u1", "tier": "T1", "model": "m",
            "isolation_goal": "message-triage", "sections": [], "tools_yielded": [], "cellar_hits": 0}
    proposed = work.record(item_id="m01", inputs={"channel": "billing"}, output={"tag": "finance"},
                           reasoning="x", provenance=prov)
    decided = work.decide(decision=dw.DECISION_CONFIRM, provenance=prov)
    [case] = work.history()
    assert case["proposed_id"] == proposed["id"] and case["decided_id"] == decided["id"]
