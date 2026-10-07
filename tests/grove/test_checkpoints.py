"""Checkpoints: save the node's records at one moment; put that moment back.

MESSAGE-TAGGING fixtures. The home is reached THROUGH A SYMLINK in every test,
as it is on the deployed node. Invariants pinned here:

  * save → more work → restore leaves the state byte-identical to the
    checkpoint, with the chain intact and the next backlog stage releasable
    again; and a second restore does the same;
  * nothing is deleted: what a restore replaces is in a dated archive folder;
  * nothing is saved or restored while work is in flight;
  * saves and restores are logged outside the goal's records, and add nothing
    to the ledger or the chain.
"""

from __future__ import annotations

import hashlib
import json

import pytest

import grove.grants as grants_mod
import grove.pattern_cache as pc
from grove import audit, checkpoints
from grove import decision_work as dw
from grove import reissue, turn_provenance
from grove.decision_work import DecisionWork
from grove.intent_store import record_digest
from tests.grove.test_work_session import BATCH, _Grants, _goal, _read, _serve_keg


class Node:
    """A node home with one goal's staged work, driven the way a session is."""

    def __init__(self, home, monkeypatch):
        self.home, self.turn, self.prev = home, 0, None
        grants = _Grants()
        monkeypatch.setattr(grants_mod, "get_grant_store", lambda: grants)
        cfg = dw.load_config(_goal(home, BATCH))
        stages = []
        for label, items in (("Month 2", ((21, "billing"), (22, "legal"))),
                             ("Month 3", ((31, "outage"), (32, "billing"), (33, "press")))):
            folder = home / label.replace(" ", "").lower()
            folder.mkdir()
            for n, channel in items:
                (folder / f"m{n}.txt").write_text(channel)
            stages.append((folder, label))
        self.cfg = cfg.__class__(**{**cfg.__dict__, "backlog": stages[0][0],
                                    "backlog_stages": tuple(stages)})
        grants.sign(self.cfg)
        monkeypatch.setattr(dw, "config_for_goal", lambda goal_id, dock=None: self.cfg)
        monkeypatch.setattr(checkpoints, "_work_goals", lambda: [DecisionWork(self.cfg)])
        (home / "dock").mkdir()
        (home / "dock" / "dock.yaml").write_text("goals: []\n")
        (home / "grants.yaml").write_text("grants: []\n")
        _serve_keg()

    @property
    def work(self):
        return DecisionWork(self.cfg)

    def prov(self, tier="T1"):
        """One turn: its record goes on the chain, as a real turn's does."""
        self.turn += 1
        rec = {"session_id": "sess", "turn_id": f"sess#{self.turn}",
               "turn_uid": f"u{self.turn}", "tier_selected": tier, "model_used": "m",
               "api_calls": 1, "duration_ms": 10.0, "outcome": "success",
               "prev_hash": self.prev, "stages": {"execution": {"model_calls": 1, "tokens": {}}}}
        rec["record_hash"] = self.prev = record_digest(rec)
        with open(self.home / "intent_records.jsonl", "a") as fh:
            fh.write(json.dumps(rec) + "\n")
        return {"session_id": "sess", "turn_id": rec["turn_id"], "turn_uid": rec["turn_uid"],
                "tier": tier, "model": "m", "request": "tag the next message",
                "cellar_hits": 0, "sections": [], "tools_yielded": ["tag_message"],
                "isolation_goal": self.cfg.goal_id}

    def month(self):
        """Release the next stage and work it to the end: the keg pass, then
        the one item it hands back, proposed by a model and confirmed."""
        work = self.work
        added = dw.release_backlog(self.cfg)
        work.session_step({"action": "batch", "inputs_for": _read}, self.prov("T0"))
        reissue.take("sess")
        item = work.next_item()
        work.record(item_id=item.stem, inputs={"channel": item.read_text()},
                    output={"tag": "other"}, reasoning="no rule covers it",
                    provenance=self.prov())
        work.decide(decision="confirm", provenance=self.prov())
        reissue.take("sess")
        return added

    def rewind_chain(self):
        """What a gateway start does after a restore: read the chain's head
        from the file, not from memory."""
        lines = (self.home / "intent_records.jsonl").read_text().splitlines()
        self.prev = json.loads(lines[-1])["record_hash"] if lines else None
        self.turn = len(lines)

    def decided(self):
        return [r["item_id"] for r in self.work.log.run_records() if r["kind"] == "decided"]

    def fingerprint(self, skip=()):
        """Every file of the node but the checkpoints folder: path → sha256."""
        out = {}
        for path in sorted(self.home.resolve().rglob("*")):
            rel = str(path.relative_to(self.home.resolve()))
            if path.is_file() and not rel.startswith("checkpoints") and rel not in skip:
                out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        return out


@pytest.fixture
def node(tmp_path, monkeypatch):
    real = tmp_path / "data" / "grove-home"
    real.mkdir(parents=True)
    home = tmp_path / "home"
    home.symlink_to(real, target_is_directory=True)        # the deployed node's shape
    monkeypatch.setenv("GROVE_HOME", str(home))
    monkeypatch.setattr(pc, "default_pattern_cache_path", lambda: home / "pattern_cache.db")
    token = turn_provenance.set_current(None)
    yield Node(home, monkeypatch)
    turn_provenance.reset(token)


def _restore(node, name):
    checkpoints.request_restore(name, surface="test")
    result = checkpoints.apply_pending()                   # what gateway start-up runs
    node.rewind_chain()
    return result


# ── the round trip ────────────────────────────────────────────────────


def test_save_then_a_month_of_work_then_restore_is_the_checkpoint_exactly(node):
    assert node.month() == 2 and node.decided() == ["m21", "m22"]
    assert checkpoints.in_flight() == []
    saved = checkpoints.save("before-month-3", "months 1 and 2 done", surface="test")
    assert saved["goals"][0]["decided"] == 2 and saved["goals"][0]["queued"] == 2
    assert saved["goals"][0]["backlog"] == {"items": 5, "released": 2}
    # The live SQLite stores are copied through SQLite, so the checkpoint's own
    # bytes are the reference for them; every other file is compared with the
    # node as it stood.
    at_save = node.fingerprint(skip=("pattern_cache.db",))
    chain_at_save = (node.home / "intent_records.jsonl").read_bytes()
    ledger_at_save = sorted(p.name for p in (node.home / ".kaizen_ledger").iterdir())

    assert node.month() == 3                                   # month 3, on real records
    (node.home / "proposals.jsonl").write_text('{"id": "p1"}\n')   # a store that did not exist
    assert node.decided() == ["m21", "m22", "m31", "m32", "m33"]
    assert dw.next_backlog_stage(node.cfg) is None
    assert node.fingerprint(skip=("pattern_cache.db",)) != at_save

    result = _restore(node, "before-month-3")
    assert result["identical"] is True and result["mismatches"] == []
    assert result["audit_check"]["result"] == "intact"
    assert result["audit_check"]["records"] == result["audit_check"]["chained"] == 3
    assert node.fingerprint(skip=("pattern_cache.db",)) == at_save       # byte for byte
    assert checkpoints.verify("before-month-3") == []
    manifest = json.loads(
        (checkpoints.root() / "before-month-3" / "manifest.json").read_text())
    keg = next(e for e in manifest["entries"] if e["path"] == "pattern_cache.db")
    assert hashlib.sha256((node.home / "pattern_cache.db").read_bytes()).hexdigest() == (
        keg["files"][""])
    assert not (node.home / "proposals.jsonl").exists()       # absent then, absent again
    # The chain is the chain as it stood: no record added, altered or re-timed.
    assert (node.home / "intent_records.jsonl").read_bytes() == chain_at_save
    assert sorted(p.name for p in (node.home / ".kaizen_ledger").iterdir()) == ledger_at_save
    assert audit.chain_report(node.home)["result"] == "intact"

    # Month 3 is releasable again, and works again on the restored records.
    assert node.decided() == ["m21", "m22"]
    assert dw.next_backlog_stage(node.cfg)["label"] == "Month 3"
    assert node.month() == 3
    assert node.decided() == ["m21", "m22", "m31", "m32", "m33"]
    assert audit.chain_report(node.home)["result"] == "intact"


def test_restore_twice_in_a_row_and_nothing_is_ever_deleted(node):
    node.month()
    checkpoints.save("before-month-3")
    at_save = node.fingerprint(skip=("pattern_cache.db",))
    node.month()
    first = _restore(node, "before-month-3")
    second = _restore(node, "before-month-3")                  # straight after, no work between
    assert first["identical"] and second["identical"]
    assert second["audit_check"]["result"] == "intact"
    assert node.fingerprint(skip=("pattern_cache.db",)) == at_save
    node.month()
    third = _restore(node, "before-month-3")
    assert third["identical"] and node.fingerprint(skip=("pattern_cache.db",)) == at_save
    # Each restore kept what it replaced. The month of work is in the archive.
    archives = sorted((checkpoints.root() / "_archive").iterdir())
    assert len(archives) == 3 and first["archive"] == str(archives[0])
    kept = [(a / "decisions").glob("*.jsonl").__next__().read_text().count('"decided"')
            for a in archives]
    assert kept == [5, 2, 5]
    assert checkpoints.last_restore()["restored_at"] == third["restored_at"]


def test_saves_and_restores_are_logged_outside_the_goals_records(node):
    node.month()
    checkpoints.save("before-month-3", surface="portal")
    chain = (node.home / "intent_records.jsonl").read_bytes()
    ledger = {p.name: p.read_bytes() for p in (node.home / ".kaizen_ledger").iterdir()}
    decisions = {p.name: p.read_bytes() for p in (node.home / "decisions").iterdir()}
    _restore(node, "before-month-3")
    assert [(e["action"], e["name"]) for e in checkpoints.admin_log()][::-1] == [
        ("saved", "before-month-3"), ("restore_requested", "before-month-3"),
        ("restored", "before-month-3")]
    # The log is beside the checkpoints, is in no checkpoint, and the goal's
    # own records carry no trace of either action.
    assert checkpoints.admin_log_path().parent == checkpoints.root()
    assert not list((checkpoints.root() / "before-month-3").rglob("admin-log.jsonl"))
    assert (node.home / "intent_records.jsonl").read_bytes() == chain
    assert {p.name: p.read_bytes() for p in (node.home / ".kaizen_ledger").iterdir()} == ledger
    assert {p.name: p.read_bytes() for p in (node.home / "decisions").iterdir()} == decisions
    for blob in list(ledger.values()) + list(decisions.values()) + [chain]:
        assert b"checkpoint" not in blob and b"restore" not in blob


# ── refusals ──────────────────────────────────────────────────────────


def test_nothing_is_saved_or_restored_while_work_is_in_flight(node):
    node.month()
    checkpoints.save("clean")
    # An item waiting for the operator's decision.
    dw.release_backlog(node.cfg)
    work = node.work
    item = work.next_item()
    work.record(item_id=item.stem, inputs={"channel": item.read_text()},
                output={"tag": "ops"}, reasoning="r", provenance=node.prov())
    before = node.fingerprint()
    with pytest.raises(checkpoints.CheckpointRefused, match="waiting for your decision"):
        checkpoints.save("mid-item")
    with pytest.raises(checkpoints.CheckpointRefused, match="Nothing was restored"):
        checkpoints.request_restore("clean")
    assert checkpoints.pending_restore() is None and checkpoints.apply_pending() is None
    assert node.fingerprint() == before and not (checkpoints.root() / "mid-item").exists()
    assert [e["action"] for e in checkpoints.admin_log(limit=2)] == [
        "restore_refused", "save_refused"]
    # Decided, but the next request is already armed: still under way.
    work.decide(decision="confirm", provenance=node.prov())
    assert (node.home / ".reissue" / "sess.json").exists() or reissue.take("sess") is None
    reissue.arm({"request": "tag the next message"}, session_id="sess")
    with pytest.raises(checkpoints.CheckpointRefused, match="re-issued"):
        checkpoints.save("mid-batch")
    reissue.take("sess")
    # A session held for a signature is under way too.
    reissue.hold("sess", {"proposal": "p1"})
    with pytest.raises(checkpoints.CheckpointRefused, match="under way"):
        checkpoints.save("held")
    reissue.release_hold("sess")
    assert checkpoints.in_flight() == []
    assert checkpoints.save("after")["name"] == "after"


def test_a_name_is_a_plain_slug_and_a_taken_name_is_kept_not_overwritten(node):
    for bad in ("", "Before Month 3", "../etc", "_archive", ".hidden", "a/b"):
        with pytest.raises(checkpoints.CheckpointRefused):
            checkpoints.save(bad)
    first = checkpoints.save("point")
    with pytest.raises(checkpoints.CheckpointRefused, match="already exists"):
        checkpoints.save("point")
    node.month()
    second = checkpoints.save("point", replace=True)
    assert second["saved_at"] > first["saved_at"]
    kept = list((checkpoints.root() / "_archive").iterdir())
    assert len(kept) == 1 and json.loads(
        (kept[0] / "manifest.json").read_text())["saved_at"] == first["saved_at"]
    with pytest.raises(checkpoints.CheckpointRefused, match="no checkpoint named"):
        checkpoints.request_restore("never-saved")


def test_a_checkpoint_that_was_changed_after_saving_is_not_restored(node):
    node.month()
    checkpoints.save("point")
    copy = next((checkpoints.root() / "point" / "state" / "decisions").iterdir())
    copy.write_text(copy.read_text().replace("other", "ops"))
    before = node.fingerprint()
    with pytest.raises(checkpoints.CheckpointRefused, match="not as it was saved"):
        checkpoints.request_restore("point")
    assert checkpoints.pending_restore() is None and node.fingerprint() == before


def test_a_restore_that_fails_is_loud_runs_once_and_the_gateway_still_starts(node, caplog):
    node.month()
    checkpoints.save("point")
    checkpoints.request_restore("point")
    (checkpoints.root() / "point" / "manifest.json").unlink()     # lost between ask and start
    with caplog.at_level("CRITICAL", logger="grove.checkpoints"):
        failed = checkpoints.apply_pending()
    assert failed["identical"] is False and "stopped part-way" in failed["failed"]
    assert any("FAILED" in r.getMessage() for r in caplog.records)
    assert checkpoints.admin_log(limit=1)[0]["action"] == "restore_failed"
    assert checkpoints.last_restore()["failed"]
    assert checkpoints.apply_pending() is None                    # consumed: never a loop
    assert node.decided() == ["m21", "m22"]                       # the records are untouched


# ── the surfaces ──────────────────────────────────────────────────────


def test_the_page_exists_in_demo_mode_only_and_restore_takes_a_second_press(node, monkeypatch):
    from grove.api import checkpoint_fragments as page

    node.month()
    checkpoints.save("before-month-3", "months 1 and 2 done")
    monkeypatch.setattr(page, "_demo", lambda: False)
    off = page.checkpoints_page_html()
    assert "demo mode only" in off and "before-month-3" not in off and "<form" not in off
    monkeypatch.setattr(page, "_demo", lambda: True)
    on = page.checkpoints_page_html()
    assert "before-month-3" in on and "months 1 and 2 done" in on
    assert "2 decided" in on and "backlog 2 of 5 released" in on
    assert "Nothing is in flight" in on
    # The list offers the question; only the confirm step carries the action.
    assert "/portal/actions/checkpoints/restore" not in on
    assert "fragments/checkpoints/?confirm=before-month-3" in on
    asked = page.checkpoints_page_html(confirm="before-month-3")
    assert "Restore before-month-3?" in asked and "not deleted" in asked
    assert asked.count("/portal/actions/checkpoints/restore") == 1
    # After a restore the page shows the result and the audit check.
    _restore(node, "before-month-3")
    done = page.checkpoints_page_html()
    assert "RESTORED · CHAIN INTACT" in done and "intact · 3 of 3 records" in done
    assert "restore requested" in done and "_archive" in done


def test_the_portal_actions_refuse_outside_demo_mode_and_never_restart(node, monkeypatch):
    import asyncio

    from grove.api import checkpoint_fragments as page

    restarts = []
    monkeypatch.setattr(page, "_restart_gateway", lambda: restarts.append(1))

    class Request:
        query = {}

        def __init__(self, **form):
            self.form = form

        async def post(self):
            return self.form

    def run(handler, **form):
        return asyncio.run(handler(Request(**form)))

    monkeypatch.setattr(page, "_demo", lambda: False)
    assert run(page.handle_checkpoint_save, name="x").status == 403
    assert run(page.handle_checkpoint_restore, name="x").status == 403
    assert checkpoints.listing() == [] and restarts == []
    monkeypatch.setattr(page, "_demo", lambda: True)
    node.month()
    assert "Saved checkpoint point" in run(page.handle_checkpoint_save, name="point").text
    # A refusal says why and restarts nothing.
    reissue.hold("sess", {"proposal": "p1"})
    refused = run(page.handle_checkpoint_restore, name="point")
    assert "Nothing was restored" in refused.text and restarts == []
    reissue.release_hold("sess")
    asked = run(page.handle_checkpoint_restore, name="point")
    assert restarts == [1] and "The gateway is restarting" in asked.text
    assert checkpoints.pending_restore()["name"] == "point"


def test_the_nav_shows_checkpoints_in_demo_mode_only():
    from grove.api.portal_nav import load_nav, render_nav

    nav = load_nav()
    kw = dict(goals=[], skills=[], live={"to_sign": 0, "suggestions": 0})
    assert "fragments/checkpoints/" not in render_nav(nav, **kw)
    assert "fragments/checkpoints/" in render_nav(nav, demo=True, **kw)


def test_the_command_line_does_the_same_two_things(node, capsys):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "checkpoint.py"
    spec = importlib.util.spec_from_file_location("checkpoint_cli", path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    node.month()
    assert cli.main(["save", "before-month-3", "--note", "ready"]) == 0
    assert "SAVED before-month-3" in capsys.readouterr().out
    assert cli.main(["save", "before-month-3"]) == 2              # taken: refused, exit 2
    assert "REFUSED" in capsys.readouterr().err
    assert cli.main(["list"]) == 0 and "ready" in capsys.readouterr().out
    node.month()
    assert cli.main(["restore", "before-month-3"]) == 0           # asked for; start-up applies
    assert "restart the gateway" in capsys.readouterr().out
    assert checkpoints.apply_pending()["identical"] is True
    node.rewind_chain()
    node.month()
    assert cli.main(["restore", "before-month-3", "--now"]) == 0  # gateway stopped
    assert node.decided() == ["m21", "m22"]
    assert checkpoints.admin_log(limit=1)[0]["surface"] == "cli"


def test_a_checkpoint_can_be_renamed_and_still_restores_exactly(node):
    node.month()
    checkpoints.save("before-month-3")
    at_save = node.fingerprint(skip=("pattern_cache.db",))
    renamed = checkpoints.rename("before-month-3", "run1-before-month-3", surface="test")
    assert (renamed["name"], renamed["renamed_from"]) == ("run1-before-month-3", "before-month-3")
    assert [m["name"] for m in checkpoints.listing()] == ["run1-before-month-3"]
    assert checkpoints.verify("run1-before-month-3") == []        # its files were not touched
    assert checkpoints.admin_log(limit=1)[0]["action"] == "renamed"
    # The old name is free again, and the renamed one restores exactly.
    node.month()
    checkpoints.save("before-month-3")
    assert _restore(node, "run1-before-month-3")["identical"] is True
    assert node.fingerprint(skip=("pattern_cache.db",)) == at_save
    for old, new in (("nope", "x"), ("run1-before-month-3", "before-month-3"),
                     ("run1-before-month-3", "Bad Name")):
        with pytest.raises(checkpoints.CheckpointRefused):
            checkpoints.rename(old, new)


def test_the_model_call_count_includes_the_call_that_writes_the_reply():
    """Found live, 2026-10-07: the count was taken when the model asked for a
    tool, so the last call of every turn was never counted."""
    from types import SimpleNamespace

    from grove.dispatcher import _completed_model_calls
    assert _completed_model_calls(SimpleNamespace(_turn_call_ms=[7886, 2043, 2038]), 2) == 3
    assert _completed_model_calls(SimpleNamespace(_turn_call_ms=[900]), 0) == 1   # a plain reply
    assert _completed_model_calls(SimpleNamespace(_turn_call_ms=[]), 0) == 0      # no model ran
    assert _completed_model_calls(SimpleNamespace(), 2) == 2                      # no list kept
    assert _completed_model_calls(None, None) == 0
