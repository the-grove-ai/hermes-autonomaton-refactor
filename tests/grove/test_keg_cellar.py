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
            """What _approve_pattern_promotion does, at a fixed time."""
            from grove.keg_store import signing

            [proposal] = [p for p in proposal_queue.read_all(path=self.queue)
                          if p.proposal_id == result.proposal_id]
            with signing():
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
    assert "evidence" not in doc and doc["protocol"] == "GRV-004"
    assert m["evidence_turns"] == ["t1", "t2", "t3"]
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


# ── Task 5: pattern_cache.db is a derived cache ─────────────────────────

import os
from dataclasses import asdict

from grove.keg_store import import_cache_rows, sync_cache
from grove.pattern_cache import CompiledPattern


def _without_counters(row):
    d = dict(row)
    d.pop("hit_count", None)
    d.pop("last_hit_at", None)
    return d


def _keg_rows(store):
    return {r.pattern_id: _without_counters(asdict(r)) for r in store.all()
            if r.pattern_id.startswith("keg:")}


def _reopen(home):
    os.remove(home.store.path)
    for extra in ("-wal", "-shm"):
        p = home.store.path.with_name(home.store.path.name + extra)
        if p.exists():
            os.remove(p)
    home.store = PatternCacheStore(home.store.path)
    return home.store


def _legacy_keg_row(*, version=4, lineage="run-43", status="active",
                    conditions=None, created="2026-10-09T01:43:35.809997+00:00",
                    promoted="2026-10-09T01:43:40.340806+00:00"):
    """A row shaped like the Oct 9 V4 row on the live node: pre-cellar."""
    from grove.effect_signature import canonical_effect_signature
    from grove.pattern_cache import t0_key

    spec = {"protocol": "GRV-004", "protocolVersion": "1.1", "name": "Message tagging",
            "version": version, "scope": "reserved", "authority_level": "green",
            "dock_goal": GOAL, "reserve": "Channels outside the table.",
            "trigger": {"request": REQUEST, "intent_class": "analysis",
                        "match_threshold": 0.8, "sessions": "goal_isolated"},
            "inputs": INPUTS, "outputs": OUTPUTS,
            "conditions": conditions or RULES_V1}
    args = {"verb": "apply_keg", "keg": spec}
    invocation = {"tool": "tag_message", "args": args,
                  "approved_signature": canonical_effect_signature("tag_message", args)}
    record = {"keg": {"name": "Message tagging", "slug": "message-tagging", "version": version,
                      "lineage": lineage, "dock_goal": GOAL},
              "andon_id": "andon-old", "repetition_count": 4, "flag": "tier_down_pattern",
              "supersedes": f"keg:message-tagging:v{version-1}:000000000000", "feedback": []}
    if promoted:
        record["signed"] = {"by": "operator", "at": promoted, "proposal_id": "sha256:old"}
    return CompiledPattern(
        pattern_id=f"keg:message-tagging:v{version}:17e9800ec15d",
        t0_key=t0_key("analysis", REQUEST), intent_class="analysis",
        cacheable_type="executable", cached_response=None,
        compiled_invocation=json.dumps(invocation, sort_keys=True),
        evidence_hash="sha256:" + "e7" * 32, status=status, created_at=created,
        promoted_at=promoted, last_hit_at=None, hit_count=0,
        promotion_evidence=json.dumps(record, sort_keys=True),
    )


def test_deleting_the_cache_and_syncing_reproduces_every_keg_row(home):
    v1 = home.propose()
    home.sign(v1)
    v2 = home.propose(rules=RULES_V1 + [{"if": "channel == 'social'", "then": {"tag": "other"}}],
                      evidence=("t1", "t2", "t3", "t9"))
    home.sign(v2)
    v3 = home.propose(rules=RULES_V1 + [{"if": "channel == 'social'", "then": {"tag": "other"}},
                                         {"if": "channel == 'incident'", "then": {"tag": "ops"}}],
                      evidence=("t1", "t2", "t3", "t5", "t9"))
    home.store.set_status(v2.pattern_id, pc.STATUS_HALTED)          # an andon stopped the line
    before = _keg_rows(home.store)
    assert v3.status == "proposed", v3.detail
    assert {r["status"] for r in before.values()} == {"superseded", "halted", "suspended"}
    store = _reopen(home)
    assert _keg_rows(store) == {}
    assert sync_cache(store=store) == {"imported": 0, "rebuilt": 3}
    assert _keg_rows(store) == before
    assert sync_cache(store=store) == {"imported": 0, "rebuilt": 0}
    assert v3.pattern_id in before and before[v3.pattern_id]["status"] == "suspended"


def test_sync_imports_pre_cellar_rows_once_and_rebuilds_them(home):
    legacy = _legacy_keg_row()
    home.store.upsert(legacy)
    assert sync_cache(store=home.store) == {"imported": 1, "rebuilt": 0}
    assert sync_cache(store=home.store) == {"imported": 0, "rebuilt": 0}
    repo = home.repo()
    m = repo.manifest()
    assert m["imported"] is True and m["keg_id"] == legacy.pattern_id and m["status"] == "active"
    [commit] = repo.log()
    assert commit["message"] == f"import {legacy.pattern_id} from pattern_cache.db (pre-cellar, status active)"
    doc = yaml.safe_load(repo.read("keg.yaml"))
    assert doc["lineage"] == "run-43" and "evidence" not in doc and doc["version"] == 4
    store = _reopen(home)
    sync_cache(store=store)
    assert _without_counters(asdict(store.get(legacy.pattern_id))) == _without_counters(asdict(legacy))


def test_a_proposal_after_an_import_diffs_against_the_imported_version(home):
    home.store.upsert(_legacy_keg_row())
    sync_cache(store=home.store)
    v5 = home.propose(rules=[{"if": "channel == 'social'", "then": {"tag": "other"}}] + RULES_V1,
                      lineage="run-43", evidence=("t7",))
    assert v5.pattern_id.startswith("keg:message-tagging:v5:")
    repo = home.repo()
    log = repo.log()
    assert len(log) == 2
    d = repo.diff(log[0]["sha"], log[1]["sha"])
    keg_diff = d[d.index("diff --git a/keg.yaml"):]
    keg_diff = keg_diff[:keg_diff.index("diff --git", 10)] if "diff --git" in keg_diff[10:] else keg_diff
    assert keg_diff.count("\n@@") == 2                      # the condition, and the version
    assert "+- if: channel == 'social'" in keg_diff
    assert "-version: 4" in keg_diff and "+version: 5" in keg_diff
    assert "lineage" not in keg_diff and "evidence" not in keg_diff
    # After signing, the imported version is superseded on rebuild too.
    home.sign(v5)
    assert home.store.get("keg:message-tagging:v4:17e9800ec15d").status == "superseded"
    before = _keg_rows(home.store)
    store = _reopen(home)
    sync_cache(store=store)
    assert _keg_rows(store) == before


def test_non_keg_rows_are_left_alone(home):
    static = CompiledPattern(
        pattern_id="sha256:" + "ab" * 32, t0_key="sha256:" + "ab" * 32,
        intent_class="conversation", cacheable_type="static", cached_response="hi",
        compiled_invocation=None, evidence_hash="sha256:" + "cd" * 32, status="active",
        created_at="2026-10-01T00:00:00+00:00")
    home.store.upsert(static)
    assert sync_cache(store=home.store) == {"imported": 0, "rebuilt": 0}
    assert not (home.root / "cellar" / "kegs").exists() or not list((home.root / "cellar" / "kegs").glob("sha256*"))
    assert home.store.get(static.pattern_id) == static


def test_feedback_and_rejection_survive_a_rebuild(home):
    from grove.flywheel_cli import _keg_feedback

    v1 = home.propose()
    [proposal] = proposal_queue.read_all(path=home.queue)
    _keg_feedback(proposal, "wrong account for social")
    home.store.set_status(v1.pattern_id, pc.STATUS_REJECTED)
    before = _keg_rows(home.store)
    assert json.loads(before[v1.pattern_id]["promotion_evidence"])["rejected"]["reason"] == "wrong account for social"
    store = _reopen(home)
    sync_cache(store=store)
    assert _keg_rows(store) == before
