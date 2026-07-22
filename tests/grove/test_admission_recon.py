"""capability-mutation-surface-v1 P6 — admission recon + effective-state pins.

(a) fragment render: an overlay-mutated record shows ``overlay · approval
    <id>`` provenance on intents/tiers and DERIVED on a re-anchored
    preferred (the D1 marker); files still carrying ``added_intents`` get a
    legacy flag.
(b) orphan-alert: an overlay slug absent from the definitions surfaces as an
    ALERT in the deploy-recon output (and an orphan card in the fragment).
(c) no-flush pin (F6): the drift-guard/recon code path contains NO deletion
    primitive for state files — deploy never crosses the sovereignty line.
    Source-level assertion, same idiom as the T5 wiring pin.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]

_APPROVAL_ID = "1193ec46e4a5deadbeef"


def _mk_defs(tmp_path: Path) -> Path:
    # retrieval-ambient-class-v1 P3 — the fragment now renders through the
    # SHARED composed path (effective_admission_state -> Capability.from_dict
    # -> _compose_state), so the fixture must be a FULL valid record: the one
    # derivation validates like the load path, by design.
    d = tmp_path / "defs"
    d.mkdir()
    (d / "browser_read.yaml").write_text(
        yaml.safe_dump({
            "id": "browser_read",
            "kind": "verb",
            "trigger": {
                "intents": ["research_request", "code_analysis"],
                "keywords": [], "dock_affinity": [],
                "always": False, "disclosure": "proactive",
            },
            "bindings": {"tools": ["browser_probe"], "credentials": None,
                         "toolset_key": None},
            "tier_rule": {
                "eligible": [3], "preferred": 3, "promotion_criteria": {},
                "validation": {"strategy": "shadow_compare",
                               "confidence_threshold": 0.95,
                               "shadow_window": 20},
            },
            "zone": "green",
            "telemetry": {"feed": "intent_feed", "track": ["invocation"]},
            "context": {"disclosure": "eager", "payload": "probe",
                        "dock_composition": "none"},
            "lifecycle": {"state": "approved",
                          "provenance": "operator_authored",
                          "created_at": "2026-07-21T00:00:00+00:00",
                          "last_used": None, "use_count": 0,
                          "flywheel_eligible": True},
            "lineage": {"source_patterns": [], "parent_id": None,
                        "decision_log": []},
            "failure": {"fallback": "halt_and_surface",
                        "diagnostic_context": [],
                        "circuit_breaker": {"threshold": 3,
                                            "window_seconds": 300}},
        }, sort_keys=False),
        encoding="utf-8",
    )
    return d


def _mk_state(tmp_path: Path, extra=None) -> Path:
    sd = tmp_path / "state"
    sd.mkdir()
    doc = {
        "id": "browser_read",
        "intents": ["memory_operation"],
        "tiers": [1, 2],
        "provenance": {
            "approval_id": _APPROVAL_ID,
            "timestamp": "2026-07-21T12:00:00+00:00",
            "surface": "red_approval",
            "write_class": "capability_admission",
        },
    }
    if extra:
        doc.update(extra)
    (sd / "browser_read.yaml").write_text(
        yaml.safe_dump(doc, sort_keys=False), encoding="utf-8"
    )
    return sd


# ── (a) fragment render ──────────────────────────────────────────────────


def test_fragment_shows_overlay_provenance_and_derived_preferred(tmp_path):
    from grove.api.fragments import render_admission_state_html

    html_out = render_admission_state_html(
        definitions_dirs=[_mk_defs(tmp_path)], state_dir=_mk_state(tmp_path)
    )
    # intents + tiers carry the overlay approval id.
    assert html_out.count(f"overlay · approval {_APPROVAL_ID}") >= 2
    # D1 marker: preferred (3) excluded by tiers [1,2] renders DERIVED —
    # never as operator-set.
    assert "derived — re-anchored by merge" in html_out
    # base and effective values are both visible (creep is legible).
    assert "research_request" in html_out and "memory_operation" in html_out


def test_fragment_flags_legacy_added_intents(tmp_path):
    from grove.api.fragments import render_admission_state_html

    html_out = render_admission_state_html(
        definitions_dirs=[_mk_defs(tmp_path)],
        state_dir=_mk_state(tmp_path, extra={"added_intents": ["old_intent"]}),
    )
    assert "LEGACY: added_intents present" in html_out


def test_fragment_untouched_record_reads_definition_source(tmp_path):
    from grove.api.fragments import render_admission_state_html

    defs = _mk_defs(tmp_path)
    sd = tmp_path / "state"
    sd.mkdir()
    # Overlay carrying only non-admission state (a model pin) — admission
    # fields must render as definition-sourced.
    (sd / "browser_read.yaml").write_text(
        yaml.safe_dump({"id": "browser_read", "model_binding": None}),
        encoding="utf-8",
    )
    html_out = render_admission_state_html(
        definitions_dirs=[defs], state_dir=sd
    )
    assert html_out.count(">definition<") >= 2  # intents + tiers rows


# ── (b) orphan alert ─────────────────────────────────────────────────────


def test_orphan_slug_alerts_in_recon_output(tmp_path):
    from grove.capability_recon import render_admission_recon

    sd = _mk_state(tmp_path)
    (sd / "ghost_record.yaml").write_text(
        yaml.safe_dump({"id": "ghost_record", "intents": ["research_request"]}),
        encoding="utf-8",
    )
    out = render_admission_recon(
        definitions_dirs=[_mk_defs(tmp_path)], state_dir=sd
    )
    assert "ALERT" in out and "ghost_record" in out
    # F6 stated inline: the alert names the no-flush contract.
    assert "never flushes overlay state" in out.lower() or "F6" in out
    # The reconciled record renders its diff too.
    assert "browser_read" in out and _APPROVAL_ID in out


def test_orphan_slug_renders_card_in_fragment(tmp_path):
    from grove.api.fragments import render_admission_state_html

    sd = _mk_state(tmp_path)
    (sd / "ghost_record.yaml").write_text(
        yaml.safe_dump({"id": "ghost_record", "intents": ["research_request"]}),
        encoding="utf-8",
    )
    html_out = render_admission_state_html(
        definitions_dirs=[_mk_defs(tmp_path)], state_dir=sd
    )
    assert "ghost_record" in html_out and "ALERT" in html_out


# ── (c) no-flush pin (F6) ────────────────────────────────────────────────


def test_deploy_recon_path_contains_no_state_flush():
    """Source-level pin: nothing on the deploy/recon path can delete overlay
    state. Any deletion primitive appearing in these sources is a reviewed-
    diff event, not a drive-by."""
    forbidden = ("unlink", "os.remove", "rmtree", "shutil.rm", "rm -rf")
    for rel in (
        "grove/capability_recon.py",
        "scripts/check-capability-drift.sh",
    ):
        src = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in src, (
                f"F6 violation: deletion primitive {token!r} in {rel} — the "
                "deploy recon path must never flush overlay state"
            )
    # deploy.sh: no deletion may target the state overlay tree.
    deploy_src = (_REPO_ROOT / "scripts" / "deploy.sh").read_text(
        encoding="utf-8"
    )
    for line in deploy_src.splitlines():
        if "capabilities/state" in line:
            assert not any(t in line for t in ("rm ", "unlink", "rmtree")), (
                f"F6 violation in deploy.sh line: {line!r}"
            )
    # The recon module is read-only end to end: no write primitives either.
    recon_src = (_REPO_ROOT / "grove" / "capability_recon.py").read_text(
        encoding="utf-8"
    )
    for token in ("write_text", "write_bytes", "yaml.safe_dump", "os.replace"):
        assert token not in recon_src, (
            f"recon module must be pure-read; found {token!r}"
        )
