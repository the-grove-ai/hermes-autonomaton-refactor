"""researcher-fleet-worker-v1 P2 — one_shot request lifecycle + staging clean-room.

Covers the DECLARATIVE ``lifecycle: one_shot`` file_source lane (claim →
``.processing/``, dispose → ``.done/`` / ``.failed/``, malformed →
``.rejected/`` + the worker-agnostic ``fleet_request_rejected`` event), the
refresh-default regression pin (absent flag = byte-identical pre-P2 behavior),
the stage_package clean-room (wipe-before-write, meta.json last, no
absorption), and the mesh-primitive invariant (no worker identity in the new
code constructs — asserted at AST level over identifiers + string constants,
so sprint-tag comments, which legitimately carry the sprint name, are not
false positives).

Runs entirely local: GROVE_HOME → tmp_path; no network, no model.
"""
from __future__ import annotations

import ast
import inspect
import json
import textwrap
from pathlib import Path

import pytest

from grove.fleet import resolvers, staging
from grove.fleet.errors import FleetWorkerAndon


@pytest.fixture
def grove_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    return tmp_path


def _input_state(**over):
    base = {
        "type": "file_source",
        "source_dir": "research-requests",
        "pattern": "*.json",
        "slug_regex": r"^(.+)\.json$",
        "lifecycle": "one_shot",
        "required_keys": ["operator_intent", "topic"],
        "select_one": True,
        "skip_already_staged": False,  # staged-skip is covered by c1b2 tests
    }
    base.update(over)
    return base


def _write_request(grove_home, name, data=None, raw=None):
    d = grove_home / "research-requests"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    if raw is not None:
        p.write_text(raw, encoding="utf-8")
    else:
        payload = {
            "operator_intent": {"angle": "build-on"},
            "topic": "https://example.com/article",
            "origin": "operator",
        }
        payload.update(data or {})
        p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def _rejected_events(grove_home):
    out = []
    ledger_dir = grove_home / ".kaizen_ledger"
    if not ledger_dir.is_dir():
        return out
    for f in ledger_dir.glob("*.jsonl"):
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("event_type") == "fleet_request_rejected":
                out.append(rec)
    return out


# ── refresh default — regression pin ─────────────────────────────────────────


def test_refresh_default_no_claim_no_move(grove_home):
    """Absent ``lifecycle`` flag → pre-P2 behavior byte-identical: no claim key
    in the payload, the source file never moves."""
    p = _write_request(grove_home, "topic-a.json")
    state = _input_state()
    del state["lifecycle"]
    payload = resolvers.resolve_file_source(state, "w1")
    assert payload is not None
    assert "request_claim" not in payload
    assert p.exists()
    assert payload["source_path"] == str(p)


# ── one_shot claim ───────────────────────────────────────────────────────────


def test_one_shot_claims_selected_request(grove_home):
    p = _write_request(grove_home, "topic-a.json")
    payload = resolvers.resolve_file_source(_input_state(), "w1")
    assert payload is not None
    claim = payload["request_claim"]
    claimed = Path(claim["path"])
    assert claimed.parent.name == ".processing"
    assert claimed.exists()
    assert not p.exists()  # left the glob surface at dispatch
    assert payload["source_path"] == str(claimed)
    assert claim["root"] == str(grove_home / "research-requests")


def test_one_shot_empty_or_absent_dir_is_no_work(grove_home):
    assert resolvers.resolve_file_source(_input_state(), "w1") is None  # absent
    (grove_home / "research-requests").mkdir(parents=True)
    assert resolvers.resolve_file_source(_input_state(), "w1") is None  # empty


def test_one_shot_claimed_file_invisible_to_next_resolve(grove_home):
    _write_request(grove_home, "topic-a.json")
    assert resolvers.resolve_file_source(_input_state(), "w1") is not None
    # the claimed request sits in .processing/ — the next resolve sees no work
    assert resolvers.resolve_file_source(_input_state(), "w1") is None


# ── one_shot dead-letter (malformed at resolve) ──────────────────────────────


def test_one_shot_malformed_json_dead_letters(grove_home):
    _write_request(grove_home, "bad.json", raw="{not json")
    assert resolvers.resolve_file_source(_input_state(), "w1") is None
    rejected = grove_home / "research-requests" / ".rejected" / "bad.json"
    assert rejected.exists()
    events = _rejected_events(grove_home)
    assert len(events) == 1
    ev = events[0]
    assert ev["worker_id"] == "w1"
    assert ev["source_dir"] == "research-requests"
    assert ev["request"] == "bad.json"
    assert "JSON" in ev["reason"]


def test_one_shot_bad_origin_rejected(grove_home):
    _write_request(grove_home, "bad-origin.json", data={"origin": "robot"})
    assert resolvers.resolve_file_source(_input_state(), "w1") is None
    assert (grove_home / "research-requests" / ".rejected" / "bad-origin.json").exists()
    assert any("origin" in e["reason"] for e in _rejected_events(grove_home))


def test_one_shot_missing_origin_rejected(grove_home):
    p = _write_request(grove_home, "no-origin.json")
    data = json.loads(p.read_text())
    del data["origin"]
    p.write_text(json.dumps(data), encoding="utf-8")
    assert resolvers.resolve_file_source(_input_state(), "w1") is None
    assert (grove_home / "research-requests" / ".rejected" / "no-origin.json").exists()


def test_one_shot_missing_required_key_rejected(grove_home):
    p = _write_request(grove_home, "no-topic.json")
    data = json.loads(p.read_text())
    del data["topic"]
    p.write_text(json.dumps(data), encoding="utf-8")
    assert resolvers.resolve_file_source(_input_state(), "w1") is None
    assert any("topic" in e["reason"] for e in _rejected_events(grove_home))


def test_one_shot_bad_filename_dead_letters_not_andon(grove_home):
    """A one_shot file that fails slug_regex is dead-lettered — never the
    refresh lane's loud Andon (which would crash-loop every tick)."""
    d = grove_home / "research-requests"
    d.mkdir(parents=True)
    (d / "noext.json").write_text("{}", encoding="utf-8")
    state = _input_state(slug_regex=r"^req-(.+)\.json$")
    # must not raise
    assert resolvers.resolve_file_source(state, "w1") is None
    assert (d / ".rejected" / "noext.json").exists()


def test_one_shot_valid_survives_malformed_sibling(grove_home):
    _write_request(grove_home, "aaa-bad.json", raw="{torn")
    _write_request(grove_home, "bbb-good.json")
    payload = resolvers.resolve_file_source(_input_state(), "w1")
    assert payload is not None
    assert payload["unit_id"] == "bbb-good"
    assert (grove_home / "research-requests" / ".rejected" / "aaa-bad.json").exists()


def test_one_shot_rejected_file_not_reseen_next_tick(grove_home):
    """Dead-letter means the next resolve is quiet — no crash loop."""
    _write_request(grove_home, "bad.json", raw="{torn")
    assert resolvers.resolve_file_source(_input_state(), "w1") is None
    assert resolvers.resolve_file_source(_input_state(), "w1") is None
    assert len(_rejected_events(grove_home)) == 1  # exactly one event, one reject


# ── one_shot disposition (reap-side) ─────────────────────────────────────────


def _claim_for(grove_home):
    _write_request(grove_home, "topic-a.json")
    payload = resolvers.resolve_file_source(_input_state(), "w1")
    return payload["request_claim"]


def test_dispose_success_moves_to_done(grove_home):
    claim = _claim_for(grove_home)
    resolvers.dispose_request_claim(claim, success=True)
    assert (grove_home / "research-requests" / ".done" / "topic-a.json").exists()
    assert not Path(claim["path"]).exists()


def test_dispose_failure_moves_to_failed(grove_home):
    claim = _claim_for(grove_home)
    resolvers.dispose_request_claim(claim, success=False)
    assert (grove_home / "research-requests" / ".failed" / "topic-a.json").exists()


def test_restore_claim_returns_file_for_retry(grove_home):
    claim = _claim_for(grove_home)
    resolvers.restore_request_claim(claim)
    restored = grove_home / "research-requests" / "topic-a.json"
    assert restored.exists()
    # and it is claimable again
    assert resolvers.resolve_file_source(_input_state(), "w1") is not None


def test_dispose_missing_file_never_raises(grove_home):
    claim = _claim_for(grove_home)
    Path(claim["path"]).unlink()
    resolvers.dispose_request_claim(claim, success=True)  # must not raise
    resolvers.restore_request_claim(claim)  # must not raise


# ── staging clean-room ───────────────────────────────────────────────────────


def test_stage_package_wipes_prior_slug_dir(tmp_path):
    sink = tmp_path / "pending_review"
    stray = sink / "slug-a" / "stray-from-killed-run.md"
    stray.parent.mkdir(parents=True)
    stray.write_text("partial", encoding="utf-8")
    staging.stage_package(sink, "slug-a", {"fresh.md": "x", "meta.json": "{}"})
    assert not stray.exists()  # clean room: prior partial erased
    assert (sink / "slug-a" / "fresh.md").exists()
    assert (sink / "slug-a" / "meta.json").exists()


def test_stage_package_never_absorbs_prior_package(tmp_path):
    sink = tmp_path / "pending_review"
    staging.stage_package(sink, "slug-a", {"one.md": "1", "meta.json": "{}"})
    staging.stage_package(sink, "slug-a", {"two.md": "2", "meta.json": "{}"})
    names = sorted(p.name for p in (sink / "slug-a").iterdir())
    assert names == ["meta.json", "two.md"]  # one.md did NOT survive


def test_stage_package_writes_meta_json_last(tmp_path, monkeypatch):
    order = []
    real = staging._atomic_write_bytes

    def _spy(dest, data):
        order.append(dest.name)
        real(dest, data)

    monkeypatch.setattr(staging, "_atomic_write_bytes", _spy)
    sink = tmp_path / "pending_review"
    # meta.json FIRST in emission order — must still be written last
    staging.stage_package(
        sink, "slug-a", {"meta.json": "{}", "a.md": "x", "b.md": "y"}
    )
    assert order[-1] == "meta.json"
    assert order[:2] == ["a.md", "b.md"]  # stable order for the rest


# ── mesh-primitive invariant + event registration ────────────────────────────

_WORKER_NAMES = ("researcher", "drafter", "cultivator", "forge", "scout")

_MESH_FUNCS = [
    resolvers._screen_request_files,
    resolvers._reject_request,
    resolvers._record_request_rejected,
    resolvers._claim_request,
    resolvers.dispose_request_claim,
    resolvers.restore_request_claim,
    resolvers.resolve_file_source,
    staging.stage_package,
]


def test_mesh_primitives_carry_no_worker_identity():
    """A-addendum pin: the one_shot lifecycle + clean-room code is worker-blind.

    Asserted over AST identifiers and string constants of each new/touched
    function (comments — which carry the sprint tag as provenance, per repo
    convention — are not code and are excluded by construction)."""
    for fn in _MESH_FUNCS:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        tokens = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                tokens.add(node.id.lower())
            elif isinstance(node, ast.Attribute):
                tokens.add(node.attr.lower())
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                tokens.add(node.value.lower())
        for name in _WORKER_NAMES:
            hits = [t for t in tokens if name in t]
            assert not hits, (
                f"mesh primitive {fn.__qualname__} carries worker identity "
                f"{name!r}: {hits}"
            )


def test_fleet_request_rejected_is_registered():
    from grove.kaizen_ledger import KaizenLedger

    assert "fleet_request_rejected" in KaizenLedger.EVENT_TYPES
