"""kaizen-ledger-retention-v1 P2 — retention engine safety pins.

Real files in tmp dirs; the only fake is the clock (injectable ``now``).
Each safety property from the module docstring gets its own pin.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from grove.ledger_retention import (
    PRESERVE_EVENT_TYPES,
    run_retention,
)

_NOW = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
_CUTOFF = _NOW - timedelta(days=30)


def _event(event_type="tool_selection", *, age_days=0.0, **fields):
    ts = (_NOW - timedelta(days=age_days)).isoformat()
    return json.dumps({
        "event_type": event_type, "session_id": "s1", "timestamp": ts,
        **fields,
    })


def _write(path: Path, lines, *, mtime_age_days=40.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(l + "\n" for l in lines), encoding="utf-8")
    mtime = (_NOW - timedelta(days=mtime_age_days)).timestamp()
    os.utime(path, (mtime, mtime))


def _dirs(tmp_path):
    return (tmp_path / "ledger", tmp_path / "archive",
            tmp_path / "state.json")


def _run(tmp_path, **kw):
    ledger, archive, state = _dirs(tmp_path)
    return run_retention(
        ledger_dir=ledger, archive_dir=archive, state_path=state,
        retention_days=30, cold_buffer_hours=24, now=_NOW, **kw,
    )


def _lines(path: Path):
    if not path.exists():
        return []
    return [l for l in path.read_text(encoding="utf-8").splitlines() if l]


def test_preserve_rules_never_prune_disposition_types(tmp_path):
    """kaizen_disposition + quarantine_skill_disposition survive at ANY age;
    aged window-bounded lines around them prune."""
    ledger, archive, state = _dirs(tmp_path)
    lines = [
        _event("kaizen_disposition", age_days=300, proposal_id="p1"),
        _event("quarantine_skill_disposition", age_days=300, skill_name="s"),
        _event("tool_selection", age_days=300),
        _event("andon_halt", age_days=300),
    ]
    _write(ledger / "old-session.jsonl", lines, mtime_age_days=299)

    report = _run(tmp_path)
    kept = _lines(ledger / "old-session.jsonl")
    assert {json.loads(l)["event_type"] for l in kept} == set(
        PRESERVE_EVENT_TYPES
    )
    archived = _lines(archive / "old-session.jsonl")
    assert {json.loads(l)["event_type"] for l in archived} == {
        "tool_selection", "andon_halt",
    }
    assert report.lines_pruned == 2 and report.lines_kept == 2


def test_cutoff_boundary_exact(tmp_path):
    """ts == cutoff keeps (>= cutoff); one second older prunes."""
    ledger, archive, _ = _dirs(tmp_path)
    at_cutoff = json.dumps({
        "event_type": "tool_selection", "session_id": "s",
        "timestamp": _CUTOFF.isoformat(),
    })
    just_older = json.dumps({
        "event_type": "tool_selection", "session_id": "s",
        "timestamp": (_CUTOFF - timedelta(seconds=1)).isoformat(),
    })
    _write(ledger / "b.jsonl", [at_cutoff, just_older])

    report = _run(tmp_path)
    assert _lines(ledger / "b.jsonl") == [at_cutoff]
    assert _lines(archive / "b.jsonl") == [just_older]
    assert report.lines_pruned == 1 and report.lines_kept == 1


def test_cold_file_stricture_hot_file_untouched(tmp_path):
    """A file with recent mtime is never read or rewritten, even if it
    contains aged prunable lines."""
    ledger, archive, _ = _dirs(tmp_path)
    lines = [_event("tool_selection", age_days=300)]
    _write(ledger / "hot.jsonl", lines, mtime_age_days=1.0)  # inside buffer
    before = (ledger / "hot.jsonl").read_text()

    report = _run(tmp_path)
    assert (ledger / "hot.jsonl").read_text() == before
    assert not (archive / "hot.jsonl").exists()
    assert report.files_hot == 1 and report.files_scanned == 0


def test_archive_before_replace_ordering(tmp_path, monkeypatch):
    """A crash at the source rewrite (os.replace) must find the pruned
    lines ALREADY fsync'd in the archive — duplicate on crash, never lose."""
    ledger, archive, _ = _dirs(tmp_path)
    keep_line = _event("tool_selection", age_days=1)
    prune_line = _event("tool_selection", age_days=300)
    _write(ledger / "c.jsonl", [keep_line, prune_line])

    real_replace = os.replace

    def _crash_on_source_replace(src, dst):
        if str(dst).endswith("c.jsonl") and ".tmp" in str(src):
            raise OSError("simulated crash at rewrite")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _crash_on_source_replace)
    with pytest.raises(OSError, match="simulated crash"):
        _run(tmp_path)

    # Pruned line durably archived BEFORE the failed rewrite; source intact.
    assert _lines(archive / "c.jsonl") == [prune_line]
    assert set(_lines(ledger / "c.jsonl")) == {keep_line, prune_line}


def test_zero_kept_moves_whole_file(tmp_path):
    """Every line prunable → the file MOVES to the archive; no empty stub."""
    ledger, archive, _ = _dirs(tmp_path)
    lines = [_event("tool_selection", age_days=300) for _ in range(3)]
    _write(ledger / "allold.jsonl", lines)

    report = _run(tmp_path)
    assert not (ledger / "allold.jsonl").exists()
    assert _lines(archive / "allold.jsonl") == lines
    assert report.files_moved == 1 and report.files_rewritten == 0


def test_unparseable_lines_kept_counted(tmp_path):
    """No JSON / unknown event type / no recognizable ts ⇒ KEEP + count."""
    ledger, archive, _ = _dirs(tmp_path)
    garbage = "{not json"
    unknown = json.dumps({
        "event_type": "future_event_type", "session_id": "s",
        "timestamp": (_NOW - timedelta(days=300)).isoformat(),
    })
    no_ts = json.dumps({"event_type": "tool_selection", "session_id": "s"})
    prunable = _event("tool_selection", age_days=300)
    _write(ledger / "d.jsonl", [garbage, unknown, no_ts, prunable])

    report = _run(tmp_path)
    assert _lines(ledger / "d.jsonl") == [garbage, unknown, no_ts]
    assert _lines(archive / "d.jsonl") == [prunable]
    assert report.lines_unparseable == 3
    assert report.lines_kept == 3 and report.lines_pruned == 1


def test_scan_state_skips_fully_retained_on_second_run(tmp_path):
    """A file with nothing to prune is verdicted fully-retained; the second
    run skips it via the (mtime, size) key without reading it."""
    ledger, archive, state = _dirs(tmp_path)
    # Eligible (old mtime) but all lines preserved-type → fully retained.
    lines = [_event("kaizen_disposition", age_days=300, proposal_id="p")]
    _write(ledger / "e.jsonl", lines)

    r1 = _run(tmp_path)
    assert r1.files_scanned == 1 and r1.files_skipped == 0
    assert json.loads(state.read_text())[str(ledger / "e.jsonl")][
        "verdict"] == "fully-retained"

    r2 = _run(tmp_path)
    assert r2.files_scanned == 0 and r2.files_skipped == 1
    assert _lines(ledger / "e.jsonl") == lines  # untouched throughout


def test_batch_bound_limits_files_per_run(tmp_path):
    """At most batch_max_files eligible files are processed per run."""
    ledger, archive, _ = _dirs(tmp_path)
    for i in range(5):
        _write(ledger / f"f{i}.jsonl",
               [_event("tool_selection", age_days=300)])

    report = _run(tmp_path, batch_max_files=2)
    assert report.files_scanned == 2
    assert report.files_moved == 2
    remaining = sorted(p.name for p in ledger.glob("*.jsonl"))
    assert len(remaining) == 3  # the rest wait for the next run


def test_dry_run_writes_nothing(tmp_path):
    """--dry-run substrate: full plan computed, zero writes — no archive,
    no rewrite, no scan-state."""
    ledger, archive, state = _dirs(tmp_path)
    _write(ledger / "g.jsonl", [
        _event("tool_selection", age_days=300),
        _event("kaizen_disposition", age_days=300, proposal_id="p"),
    ])
    before = (ledger / "g.jsonl").read_text()

    report = _run(tmp_path, dry_run=True)
    assert report.dry_run is True
    assert report.lines_pruned == 1 and report.lines_kept == 1
    assert report.files_rewritten == 1  # planned, not performed
    assert (ledger / "g.jsonl").read_text() == before
    assert not archive.exists()
    assert not state.exists()


# ── P3: config loader (fault_triage precedent) ───────────────────────────


def test_loader_absent_file_and_block_defaults(tmp_path):
    from grove.ledger_retention import RetentionConfig, load_retention_config

    cfg = load_retention_config(tmp_path / "nope.yaml")  # absent file
    assert cfg == RetentionConfig()

    p = tmp_path / "flywheel.config.yaml"
    p.write_text("fault_triage:\n  min_events: 5\n")  # absent block
    assert load_retention_config(p) == RetentionConfig()

    p.write_text(
        "ledger_retention:\n  retention_days: 45\n  enabled: false\n"
    )
    cfg = load_retention_config(p)
    assert cfg.retention_days == 45 and cfg.enabled is False
    assert cfg.cold_buffer_hours == 24  # absent key → default


def test_loader_invalid_values_fail_loud(tmp_path):
    from grove.ledger_retention import load_retention_config

    p = tmp_path / "flywheel.config.yaml"
    p.write_text("ledger_retention:\n  retention_days: 0\n")
    with pytest.raises(ValueError, match="retention_days must be >= 1"):
        load_retention_config(p)
    p.write_text("ledger_retention:\n  enabled: 'yes'\n")
    with pytest.raises(ValueError, match="enabled must be a boolean"):
        load_retention_config(p)
    p.write_text("ledger_retention:\n  sidecar_max_bytes: true\n")
    with pytest.raises(ValueError, match="sidecar_max_bytes must be an integer"):
        load_retention_config(p)


# ── P3: CLI (flywheel maintain --retention) ──────────────────────────────


def _grove_home():
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def test_cli_dry_run_prints_plan_writes_nothing(capsys):
    from grove.flywheel_cli import cli_maintain_retention
    from grove.ledger_retention import default_archive_dir, default_state_path

    ledger = _grove_home() / ".kaizen_ledger"
    _write(ledger / "cli-old.jsonl", [_event("tool_selection", age_days=300)])
    before = (ledger / "cli-old.jsonl").read_text()

    # now is real wall-clock here; a 300-day-old event + mtime is cold and
    # prunable under the default 30d window regardless of today's date.
    rc = cli_maintain_retention(dry_run=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert "PRUNE PLAN (dry run — nothing written)" in out
    assert "cli-old.jsonl" in out and "prune 1" in out
    assert (ledger / "cli-old.jsonl").read_text() == before
    assert not default_archive_dir().exists()
    assert not default_state_path().exists()


def test_cli_live_run_prunes_and_reports(capsys):
    from grove.flywheel_cli import cli_maintain_retention
    from grove.ledger_retention import default_archive_dir

    ledger = _grove_home() / ".kaizen_ledger"
    _write(ledger / "cli-live.jsonl", [_event("tool_selection", age_days=300)])

    rc = cli_maintain_retention()
    out = capsys.readouterr().out
    assert rc == 0
    assert "RETENTION RUN" in out and "moved=1" in out
    assert not (ledger / "cli-live.jsonl").exists()
    assert len(_lines(default_archive_dir() / "cli-live.jsonl")) == 1


def test_cli_disabled_exits_zero(capsys):
    from grove.flywheel_cli import cli_maintain_retention

    (_grove_home() / "flywheel.config.yaml").write_text(
        "ledger_retention:\n  enabled: false\n"
    )
    rc = cli_maintain_retention()
    assert rc == 0
    assert "disabled" in capsys.readouterr().out


def test_cli_failure_files_andon_and_exits_nonzero(monkeypatch, capsys):
    from grove import ledger_retention as lr
    from grove.flywheel_cli import cli_maintain_retention

    def _boom(**kw):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(lr, "run_retention", _boom)
    rc = cli_maintain_retention()
    assert rc == 1
    assert "ledger retention FAILED" in capsys.readouterr().err

    halts = []
    for f in (_grove_home() / ".kaizen_ledger").glob("cli-*.jsonl"):
        for line in f.read_text().splitlines():
            e = json.loads(line)
            if e.get("event_type") == "andon_halt":
                halts.append(e)
    assert len(halts) == 1
    assert halts[0]["source"] == "ledger_retention"
    assert halts[0]["check"] == "retention_run"
    assert "engine exploded" in halts[0]["detail"]


# ── P4: quarantine sidecar rotation (proposal_queue) ─────────────────────


def _sidecar_cap(cap: int):
    (_grove_home() / "flywheel.config.yaml").write_text(
        f"ledger_retention:\n  sidecar_max_bytes: {cap}\n"
    )


def test_sidecar_rotation_trigger(tmp_path):
    """Sidecar over the cap rotates to .quarantine.1 before the append;
    the fresh sidecar holds only the new lines."""
    from grove.eval.proposal_queue import _quarantine_lines

    _sidecar_cap(50)
    target = tmp_path / "proposals.jsonl"
    qpath = tmp_path / "proposals.jsonl.quarantine"
    qpath.write_text("OLD-" * 20 + "\n")  # 81 B > 50 B cap

    _quarantine_lines(target, ["fresh-evidence-row"])

    rotated = tmp_path / "proposals.jsonl.quarantine.1"
    assert rotated.read_text().startswith("OLD-")
    assert _lines(qpath) == ["fresh-evidence-row"]


def test_sidecar_rotation_failure_never_blocks_append(tmp_path, monkeypatch):
    """A rotation failure logs and the append still lands — corruption
    evidence is never dropped for a housekeeping error."""
    import os as os_mod
    from grove.eval.proposal_queue import _quarantine_lines

    _sidecar_cap(10)
    target = tmp_path / "proposals.jsonl"
    qpath = tmp_path / "proposals.jsonl.quarantine"
    qpath.write_text("X" * 100 + "\n")  # over cap → rotation attempted

    def _boom(src, dst):
        raise OSError("rotation denied")

    monkeypatch.setattr(os_mod, "replace", _boom)
    _quarantine_lines(target, ["must-survive"])

    assert not (tmp_path / "proposals.jsonl.quarantine.1").exists()
    content = _lines(qpath)
    assert content[0].startswith("X")  # old content still there (no rotate)
    assert content[-1] == "must-survive"  # append landed anyway


def test_sidecar_dot1_single_generation_overwrite(tmp_path):
    """A second rotation clobbers the prior .1 — exactly one generation."""
    from grove.eval.proposal_queue import _quarantine_lines

    _sidecar_cap(30)
    target = tmp_path / "proposals.jsonl"
    qpath = tmp_path / "proposals.jsonl.quarantine"
    rotated = tmp_path / "proposals.jsonl.quarantine.1"

    qpath.write_text("GEN1-" * 10 + "\n")            # over cap
    _quarantine_lines(target, ["row-a"])             # rotation 1
    assert rotated.read_text().startswith("GEN1-")

    qpath.write_text("GEN2-" * 10 + "\n")            # over cap again
    _quarantine_lines(target, ["row-b"])             # rotation 2 clobbers .1
    assert rotated.read_text().startswith("GEN2-")
    assert "GEN1-" not in rotated.read_text()
    assert _lines(qpath) == ["row-b"]


def test_sidecar_under_cap_no_rotation(tmp_path):
    """Under the cap: plain append, no .1 created."""
    from grove.eval.proposal_queue import _quarantine_lines

    _sidecar_cap(1000)
    target = tmp_path / "proposals.jsonl"
    qpath = tmp_path / "proposals.jsonl.quarantine"
    qpath.write_text("small\n")

    _quarantine_lines(target, ["row"])
    assert not (tmp_path / "proposals.jsonl.quarantine.1").exists()
    assert _lines(qpath) == ["small", "row"]


# ── P5: parity --since completeness advisory ─────────────────────────────


def test_parity_warns_when_since_predates_cutoff(capsys):
    """--since older than now - retention_days ⇒ stderr WARNING naming the
    cutoff and the archive; the parity run itself proceeds unchanged."""
    from datetime import datetime, timedelta, timezone as _tz
    from grove.capability_feed_parity import main

    old_since = (datetime.now(_tz.utc) - timedelta(days=90)).isoformat()
    until = datetime.now(_tz.utc).isoformat()
    rc = main(["--since", old_since, "--until", until])
    captured = capsys.readouterr()
    assert rc == 0  # empty stores → no real mismatches; behavior unchanged
    assert "WARNING" in captured.err
    assert "live ledger may be incomplete" in captured.err
    assert ".kaizen_ledger_archive" in captured.err
    assert "Parity window" in captured.out  # summary still prints


def test_parity_no_warning_inside_window(capsys):
    from datetime import datetime, timedelta, timezone as _tz
    from grove.capability_feed_parity import main

    since = (datetime.now(_tz.utc) - timedelta(days=2)).isoformat()
    until = datetime.now(_tz.utc).isoformat()
    rc = main(["--since", since, "--until", until])
    captured = capsys.readouterr()
    assert rc == 0
    assert "WARNING" not in captured.err
