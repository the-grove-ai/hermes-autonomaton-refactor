"""fleet-hygiene-sweep P3 — scripted dirty-tree rehearsal of the deploy guard.

check-capability-drift.sh runs on the VM BEFORE `git reset --hard`. It must
halt loud (exit 1 + offending paths + ledger event) on any TRACKED change to
config/capabilities/, and pass clean (exit 0) otherwise — ignoring untracked
writer litter (.bak/.lock/.tmp). Driven here as a real bash subprocess against
a throwaway git repo.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[2] / "scripts" / "check-capability-drift.sh"


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "config" / "capabilities").mkdir(parents=True)
    (r / "config" / "capabilities" / "skill__demo__x.yaml").write_text(
        "id: skill.demo.x\nkind: skill\n", encoding="utf-8"
    )
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "seed")
    return r


def _run_guard(repo, grove_home):
    """Source the guard + invoke the function; return (rc, stderr)."""
    proc = subprocess.run(
        ["bash", "-c", f'source "{GUARD}"; check_capability_drift "{repo}"'],
        capture_output=True, text=True,
        env={"HOME": str(grove_home), "GROVE_HOME": str(grove_home), "PATH": _path()},
    )
    return proc.returncode, proc.stderr


def _path():
    import os
    return os.environ.get("PATH", "/usr/bin:/bin")


def _ledger_events(grove_home):
    f = grove_home / ".kaizen_ledger" / "deploy.jsonl"
    if not f.exists():
        return []
    return [json.loads(ln) for ln in f.read_text().splitlines() if ln.strip()]


def test_clean_tree_passes(repo, tmp_path):
    rc, _ = _run_guard(repo, tmp_path / "grove")
    assert rc == 0
    assert _ledger_events(tmp_path / "grove") == []


def test_tracked_modification_halts_with_ledger(repo, tmp_path):
    (repo / "config" / "capabilities" / "skill__demo__x.yaml").write_text(
        "id: skill.demo.x\nkind: skill\nmodel_binding:\n  type: model\n  model: prov/x\n",
        encoding="utf-8",
    )
    grove = tmp_path / "grove"
    rc, err = _run_guard(repo, grove)
    assert rc == 1
    assert "DEPLOY HALT" in err
    assert "skill__demo__x.yaml" in err
    events = _ledger_events(grove)
    assert len(events) == 1
    assert events[0]["event_type"] == "deploy_drift_halt"
    assert any("skill__demo__x.yaml" in p for p in events[0]["paths"])


def test_untracked_litter_ignored(repo, tmp_path):
    # writer litter that git reset would clean anyway — not operator state
    d = repo / "config" / "capabilities"
    (d / "skill__demo__x.yaml.bak").write_text("stale", encoding="utf-8")
    (d / "skill__demo__x.yaml.lock").write_text("", encoding="utf-8")
    (d / ".cap_abc.tmp").write_text("", encoding="utf-8")
    rc, _ = _run_guard(repo, tmp_path / "grove")
    assert rc == 0


def test_new_untracked_record_halts(repo, tmp_path):
    # a stray pin file (real capability yaml) IS operator state — halt
    (repo / "config" / "capabilities" / "skill__demo__stray.yaml").write_text(
        "id: skill.demo.stray\nkind: skill\n", encoding="utf-8"
    )
    rc, err = _run_guard(repo, tmp_path / "grove")
    assert rc == 1
    assert "skill__demo__stray.yaml" in err


def test_untracked_litter_beside_real_drift_still_halts(repo, tmp_path):
    d = repo / "config" / "capabilities"
    (d / "skill__demo__x.yaml.bak").write_text("stale", encoding="utf-8")
    (d / "skill__demo__x.yaml").write_text(
        "id: skill.demo.x\nkind: skill\npinned: true\n", encoding="utf-8"
    )
    rc, err = _run_guard(repo, tmp_path / "grove")
    assert rc == 1
    assert "skill__demo__x.yaml" in err
