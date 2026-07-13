"""binding-governance-surfaces-v1 Phase 2 — model_binding proposal type.

Follows the dock_mutation landing pattern (test_dock_mutation.py test 16).
Proves:

* RENDERERS — summary reads pin / re-pin / unpin naturally; diff carries
  before/after; push_body uses the governance-recommendation frame.
* REGISTRY — _handler_for dispatches the ninth row automatically.
* END-TO-END — queue → dispatch → apply_callback → record file mutated
  through set_model_binding (the ONE sanctioned writer).
* AUDIT JOIN — cli_approve records the kaizen_disposition AND the writer
  files its own capability_binding_mutation with surface="proposal_apply"
  and the SAME proposal_id (the R5 join).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import grove.capability_registry as reg
from grove.capability import Capability, ModelBinding
from grove.eval import proposal_queue as pq
from grove.eval.proposal_queue import (
    PROPOSAL_TYPE_MODEL_BINDING,
    RoutingProposal,
    compute_proposal_id,
)

from .test_capability_binding_writer import _mint, _skill_cap

_CATALOG = [{"slug": "z-ai/glm-5.2"}]


@pytest.fixture
def caps_env(tmp_path, monkeypatch):
    """Hermetic registry + queue + ledger home (Phase 1 fixture shape)."""
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    repo_caps = tmp_path / "repo_caps"
    repo_caps.mkdir()
    monkeypatch.setattr(reg, "default_capabilities_dir", lambda: repo_caps)
    monkeypatch.setattr(
        reg, "grove_home_capabilities_dir", lambda: tmp_path / "capabilities"
    )
    monkeypatch.setattr(
        "grove.config.model_catalog.load_catalog", lambda: list(_CATALOG)
    )
    return repo_caps


def _proposal(
    skill: str = "bindprop-alpha",
    proposed: dict | None = None,
    previous: dict | None = None,
) -> RoutingProposal:
    payload = {
        "skill": skill,
        "proposed_binding": proposed,
        "previous_binding": previous,
    }
    return RoutingProposal(
        proposal_id=compute_proposal_id(
            type=PROPOSAL_TYPE_MODEL_BINDING, payload=payload, evidence=(),
        ),
        type=PROPOSAL_TYPE_MODEL_BINDING,
        payload=payload,
        evidence=(),
        eval_hash="",
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def _ledger_events(tmp_path: Path, event_type: str) -> list[dict]:
    events = []
    ledger_dir = tmp_path / ".kaizen_ledger"
    if not ledger_dir.is_dir():
        return events
    for f in sorted(ledger_dir.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            ev = json.loads(line)
            if ev.get("event_type") == event_type:
                events.append(ev)
    return events


# ── renderers ─────────────────────────────────────────────────────────────────


def test_summary_pin_reads_naturally():
    from grove.flywheel_cli import _summary_model_binding
    s = _summary_model_binding(
        _proposal(proposed={"type": "model", "model": "z-ai/glm-5.2"})
    )
    assert "Pin 'bindprop-alpha' to z-ai/glm-5.2" in s
    assert "inheriting its tier model" in s


def test_summary_repin_names_previous():
    from grove.flywheel_cli import _summary_model_binding
    s = _summary_model_binding(
        _proposal(
            proposed={"type": "model", "model": "z-ai/glm-5.2"},
            previous={"type": "model", "model": "anthropic/claude-haiku-4.5"},
        )
    )
    assert "pinned to anthropic/claude-haiku-4.5" in s


def test_summary_unpin_reads_naturally():
    from grove.flywheel_cli import _summary_model_binding
    s = _summary_model_binding(
        _proposal(proposed=None, previous={"type": "model", "model": "z-ai/glm-5.2"})
    )
    assert "Clear the model pin" in s
    assert "tier inheritance" in s


def test_diff_carries_before_and_after():
    from grove.flywheel_cli import _model_binding_to_diff
    diff = _model_binding_to_diff(
        _proposal(
            proposed={"type": "model", "model": "z-ai/glm-5.2"},
            previous={"type": "tier_override", "tier": "T2"},
        )
    )
    body = diff["capability record: bindprop-alpha"]["model_binding"]
    assert body["-before"] == {"type": "tier_override", "tier": "T2"}
    assert body["+after"] == {"type": "model", "model": "z-ai/glm-5.2"}


def test_push_frame_is_governance_recommendation():
    body = _proposal(
        proposed={"type": "model", "model": "z-ai/glm-5.2"}
    ).push_body("a binding")
    assert body.startswith("I'm recommending a model binding change")


# ── registry dispatch + end-to-end apply ──────────────────────────────────────


def test_end_to_end_queue_to_apply(caps_env, tmp_path):
    from grove.flywheel_cli import _handler_for

    record_path = _mint(caps_env, _skill_cap("skill.demo.bindprop-alpha"))
    proposal = _proposal(proposed={"type": "model", "model": "z-ai/glm-5.2"})
    assert pq.append(proposal) is True

    queued = [
        p for p in pq.read_all() if p.type == PROPOSAL_TYPE_MODEL_BINDING
    ]
    assert len(queued) == 1

    handler = _handler_for(queued[0].type)  # registry dispatch resolves
    target, applied = handler.apply_callback(queued[0], machine_path=None)

    # fleet-hygiene-sweep P2 — apply writes the STATE overlay; the definition
    # is untouched, the composed load renders the pin.
    from grove.capability_registry import capability_state_dir, load_capabilities

    assert target == capability_state_dir() / "skill__demo__bindprop-alpha.yaml"
    assert applied["record_id"] == "skill.demo.bindprop-alpha"
    assert applied["new_binding"] == {"type": "model", "model": "z-ai/glm-5.2"}
    assert Capability.from_yaml(
        record_path.read_text(encoding="utf-8")
    ).model_binding is None  # definition clean
    reloaded = load_capabilities()["skill.demo.bindprop-alpha"]
    assert reloaded.model_binding.model == "z-ai/glm-5.2"


def test_apply_unpin_clears_record(caps_env, tmp_path):
    from grove.flywheel_cli import _approve_model_binding

    record_path = _mint(
        caps_env,
        _skill_cap(
            "skill.demo.bindprop-beta",
            model_binding=ModelBinding(type="model", model="z-ai/glm-5.2"),
        ),
    )
    proposal = _proposal(
        skill="bindprop-beta",
        proposed=None,
        previous={"type": "model", "model": "z-ai/glm-5.2"},
    )
    target, applied = _approve_model_binding(proposal)
    assert applied["new_binding"] is None
    assert applied["previous_binding"] == {"type": "model", "model": "z-ai/glm-5.2"}
    # P2 — definition keeps its seed pin (read-only); composed load reflects clear
    from grove.capability_registry import load_capabilities

    assert "model_binding" in record_path.read_text(encoding="utf-8")  # defn untouched
    assert load_capabilities()["skill.demo.bindprop-beta"].model_binding is None


def test_apply_refuses_malformed_payload(caps_env):
    from grove.flywheel_cli import _approve_model_binding

    bad = RoutingProposal(
        proposal_id="sha256:bad", type=PROPOSAL_TYPE_MODEL_BINDING,
        payload={"proposed_binding": None}, evidence=(), eval_hash="",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    with pytest.raises(ValueError, match="missing a non-empty skill"):
        _approve_model_binding(bad)


# ── audit join (R5) ───────────────────────────────────────────────────────────


def test_cli_approve_disposition_and_writer_event_join(caps_env, tmp_path, capsys):
    from grove import flywheel_cli

    _mint(caps_env, _skill_cap("skill.demo.bindprop-gamma"))
    proposal = _proposal(
        skill="bindprop-gamma",
        proposed={"type": "model", "model": "z-ai/glm-5.2"},
    )
    assert pq.append(proposal) is True

    rc = flywheel_cli.cli_approve(proposal.short_id)
    assert rc == 0

    # Queue drained.
    assert [
        p for p in pq.read_all() if p.type == PROPOSAL_TYPE_MODEL_BINDING
    ] == []

    # Disposition ledger fired.
    dispositions = _ledger_events(tmp_path, "kaizen_disposition")
    assert len(dispositions) == 1
    assert dispositions[0]["proposal_id"] == proposal.proposal_id
    assert dispositions[0]["proposal_type"] == PROPOSAL_TYPE_MODEL_BINDING
    assert dispositions[0]["disposition"] == "applied"
    assert dispositions[0]["applied_result"]["new_binding"] == {
        "type": "model", "model": "z-ai/glm-5.2",
    }

    # Writer filed its OWN event, joined on the SAME proposal_id (R5).
    writer_events = _ledger_events(tmp_path, "capability_binding_mutation")
    assert len(writer_events) == 1
    assert writer_events[0]["proposal_id"] == proposal.proposal_id
    assert writer_events[0]["surface"] == "proposal_apply"
    assert writer_events[0]["record_id"] == "skill.demo.bindprop-gamma"
