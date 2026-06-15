"""Tests for `hermes curator archive` and `hermes curator prune`.

GRV-009 E6b C2-bridge — archive/prune are now record-backed: pinned + state
come from the capability record (lifecycle.pinned / lifecycle.state) and the
archive action is transition_record(id, DEPRECATED). Telemetry (idle) still
comes from the .usage.json row via agent_created_report.

Covers:
- archive refuses pinned skills with an `unpin` hint
- archive deprecates the record (returns 0); no-record / non-active → 1
- prune filters pinned and already-deprecated, applies --days threshold
- prune falls back to created_at when last_activity_at is null
- prune --dry-run makes no changes; --yes skips confirmation; --days validation
"""

from __future__ import annotations

from types import SimpleNamespace

from grove.capability import LifecycleState
from grove.capability_registry import (
    TRANSITION_APPLIED,
    TRANSITION_SKIPPED,
    TransitionResult,
)


def _ns(**kwargs):
    return SimpleNamespace(**kwargs)


def _rec(name, *, pinned=False, state=LifecycleState.ACTIVE):
    """A minimal stand-in capability record (id + lifecycle.pinned/state)."""
    return SimpleNamespace(
        id=f"skill.test.{name}",
        lifecycle=SimpleNamespace(pinned=pinned, state=state),
    )


def _patch_registry(monkeypatch, *, records, applied):
    """Wire skill_record_for_name + transition_record on the registry module."""
    import grove.capability_registry as reg

    monkeypatch.setattr(reg, "skill_record_for_name", lambda n: records.get(n))

    def _transition(cap_id, to_state, **_k):
        applied.append((cap_id, to_state))
        return TransitionResult(TRANSITION_APPLIED, None)

    monkeypatch.setattr(reg, "transition_record", _transition)


# ─── archive ────────────────────────────────────────────────────────────────


def test_archive_refuses_pinned(monkeypatch, capsys):
    import hermes_cli.curator as curator_cli

    applied = []
    _patch_registry(
        monkeypatch, records={"pinned-skill": _rec("pinned-skill", pinned=True)},
        applied=applied,
    )
    rc = curator_cli._cmd_archive(_ns(skill="pinned-skill"))
    assert rc == 1
    assert applied == []  # never transitioned
    out = capsys.readouterr().out
    assert "pinned" in out.lower()
    assert "hermes curator unpin" in out


def test_archive_deprecates_record(monkeypatch, capsys):
    import hermes_cli.curator as curator_cli

    applied = []
    _patch_registry(
        monkeypatch, records={"my-skill": _rec("my-skill")}, applied=applied,
    )
    rc = curator_cli._cmd_archive(_ns(skill="my-skill"))
    assert rc == 0
    assert applied == [("skill.test.my-skill", LifecycleState.DEPRECATED)]
    assert "archived" in capsys.readouterr().out.lower()


def test_archive_no_record_reports_failure(monkeypatch, capsys):
    import hermes_cli.curator as curator_cli

    applied = []
    _patch_registry(monkeypatch, records={}, applied=applied)  # no record
    rc = curator_cli._cmd_archive(_ns(skill="hub-slug"))
    assert rc == 1
    assert applied == []
    assert "no capability record" in capsys.readouterr().out.lower()


def test_archive_non_active_reports_state(monkeypatch, capsys):
    import grove.capability_registry as reg
    import hermes_cli.curator as curator_cli

    monkeypatch.setattr(
        reg, "skill_record_for_name",
        lambda n: _rec(n, state=LifecycleState.PROPOSED),
    )
    monkeypatch.setattr(
        reg, "transition_record",
        lambda *a, **k: TransitionResult(TRANSITION_SKIPPED, None),
    )
    rc = curator_cli._cmd_archive(_ns(skill="proposed-skill"))
    assert rc == 1
    assert "not in an archivable" in capsys.readouterr().out.lower()


# ─── prune ──────────────────────────────────────────────────────────────────


def _mk_row(name, *, idle_days=0, created_idle_days=None):
    """A telemetry row (name + activity timestamps); state/pinned live on the record."""
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    last_activity = (now - _dt.timedelta(days=idle_days)).isoformat() if idle_days else None
    created_delta = created_idle_days if created_idle_days is not None else idle_days
    created = (now - _dt.timedelta(days=created_delta)).isoformat()
    return {
        "name": name,
        "last_activity_at": last_activity,
        "created_at": created,
        "activity_count": 0 if idle_days == 0 and last_activity is None else 1,
    }


def _patch_prune(monkeypatch, rows, records):
    import tools.skill_usage as skill_usage
    monkeypatch.setattr(skill_usage, "agent_created_report", lambda: rows)
    applied = []
    _patch_registry(monkeypatch, records=records, applied=applied)
    return applied


def test_prune_days_validation(capsys):
    import hermes_cli.curator as curator_cli
    rc = curator_cli._cmd_prune(_ns(days=0, yes=True, dry_run=False))
    assert rc == 2
    assert "--days must be >= 1" in capsys.readouterr().err


def test_prune_nothing_to_do(monkeypatch, capsys):
    import hermes_cli.curator as curator_cli
    _patch_prune(monkeypatch, [], {})
    rc = curator_cli._cmd_prune(_ns(days=30, yes=True, dry_run=False))
    assert rc == 0
    assert "nothing to prune" in capsys.readouterr().out


def test_prune_filters_pinned_and_archived(monkeypatch, capsys):
    import hermes_cli.curator as curator_cli

    rows = [
        _mk_row("old-pinned", idle_days=200),
        _mk_row("old-archived", idle_days=200),
        _mk_row("recent", idle_days=10),
        _mk_row("old-active", idle_days=200),
    ]
    records = {
        "old-pinned": _rec("old-pinned", pinned=True),
        "old-archived": _rec("old-archived", state=LifecycleState.DEPRECATED),
        "recent": _rec("recent"),
        "old-active": _rec("old-active"),
    }
    applied = _patch_prune(monkeypatch, rows, records)
    rc = curator_cli._cmd_prune(_ns(days=30, yes=True, dry_run=False))
    assert rc == 0
    assert applied == [("skill.test.old-active", LifecycleState.DEPRECATED)]
    out = capsys.readouterr().out
    assert "old-active" in out and "old-pinned" not in out
    assert "old-archived" not in out and "recent" not in out
    assert "archived 1/1" in out


def test_prune_falls_back_to_created_at_when_never_used(monkeypatch, capsys):
    import hermes_cli.curator as curator_cli
    rows = [_mk_row("never-used", idle_days=0, created_idle_days=200)]
    rows[0]["last_activity_at"] = None
    applied = _patch_prune(monkeypatch, rows, {"never-used": _rec("never-used")})
    rc = curator_cli._cmd_prune(_ns(days=90, yes=True, dry_run=False))
    assert rc == 0
    assert applied == [("skill.test.never-used", LifecycleState.DEPRECATED)]


def test_prune_dry_run_makes_no_changes(monkeypatch, capsys):
    import hermes_cli.curator as curator_cli
    rows = [_mk_row("old-skill", idle_days=200)]
    applied = _patch_prune(monkeypatch, rows, {"old-skill": _rec("old-skill")})
    rc = curator_cli._cmd_prune(_ns(days=30, yes=True, dry_run=True))
    assert rc == 0
    assert applied == []
    out = capsys.readouterr().out
    assert "old-skill" in out and "dry run" in out


def test_prune_prompts_without_yes(monkeypatch, capsys):
    import hermes_cli.curator as curator_cli
    rows = [_mk_row("old-skill", idle_days=200)]
    applied = _patch_prune(monkeypatch, rows, {"old-skill": _rec("old-skill")})
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    rc = curator_cli._cmd_prune(_ns(days=30, yes=False, dry_run=False))
    assert rc == 1
    assert applied == []
    assert "aborted" in capsys.readouterr().out


def test_prune_confirms_with_y(monkeypatch, capsys):
    import hermes_cli.curator as curator_cli
    rows = [_mk_row("old-skill", idle_days=200)]
    applied = _patch_prune(monkeypatch, rows, {"old-skill": _rec("old-skill")})
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    rc = curator_cli._cmd_prune(_ns(days=30, yes=False, dry_run=False))
    assert rc == 0
    assert applied == [("skill.test.old-skill", LifecycleState.DEPRECATED)]


def test_prune_reports_partial_failure(monkeypatch, capsys):
    import grove.capability_registry as reg
    import hermes_cli.curator as curator_cli
    import tools.skill_usage as skill_usage

    rows = [_mk_row("ok-skill", idle_days=200), _mk_row("bad-skill", idle_days=200)]
    records = {"ok-skill": _rec("ok-skill"), "bad-skill": _rec("bad-skill")}
    monkeypatch.setattr(skill_usage, "agent_created_report", lambda: rows)
    monkeypatch.setattr(reg, "skill_record_for_name", lambda n: records.get(n))

    def _transition(cap_id, to_state, **_k):
        if cap_id == "skill.test.bad-skill":
            return TransitionResult(TRANSITION_SKIPPED, None)
        return TransitionResult(TRANSITION_APPLIED, None)

    monkeypatch.setattr(reg, "transition_record", _transition)
    rc = curator_cli._cmd_prune(_ns(days=30, yes=True, dry_run=False))
    assert rc == 1
    out = capsys.readouterr().out
    assert "archived 1/2" in out
    assert "bad-skill" in out


# ─── argparse wiring ────────────────────────────────────────────────────────


def test_archive_and_prune_registered():
    import argparse
    import hermes_cli.curator as curator_cli

    parser = argparse.ArgumentParser(prog="hermes curator")
    curator_cli.register_cli(parser)

    args = parser.parse_args(["archive", "my-skill"])
    assert args.skill == "my-skill"
    assert args.func.__name__ == "_cmd_archive"

    args = parser.parse_args(["prune", "--days", "45", "--yes", "--dry-run"])
    assert args.days == 45
    assert args.yes is True
    assert args.dry_run is True
    assert args.func.__name__ == "_cmd_prune"


def test_prune_defaults():
    import argparse
    import hermes_cli.curator as curator_cli

    parser = argparse.ArgumentParser(prog="hermes curator")
    curator_cli.register_cli(parser)
    args = parser.parse_args(["prune"])
    assert args.days == 90
    assert args.yes is False
    assert args.dry_run is False
