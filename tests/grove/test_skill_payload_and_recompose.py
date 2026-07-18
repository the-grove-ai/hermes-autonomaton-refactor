"""skill-adoption-v1 C2 + C3 — the skill_payload composer block and the
dispatcher recompose keying.

C2: a gateable ``skill_payload`` provider injects the CURRENT turn's primary
skill's payload, gated through the layered body_hash (definition anchor) +
promotion-pin integrity model. Every arm fail-closes to emit-nothing. C3: the
dispatcher forces a recompose when this turn's primary slug differs from the slug
the injected prompt was last composed with; a None primary never forces (payload
persists); any natural recompose resets the tracker (boundary eviction).
"""

from __future__ import annotations

import copy
import types

import pytest
import yaml

from grove import capability_registry as reg
from grove.capability import Capability
from grove.dispatcher import Dispatcher
from grove.prompt.composer import _skill_payload_provider

REPO_CAPS = reg.default_capabilities_dir()
_BASE = yaml.safe_load(
    (REPO_CAPS / "skill__fleet__researcher.yaml").read_text(encoding="utf-8")
)


def _cap(*, cap_id="skill.fleet.myprimary", intents=None, primary=None,
         body_hash="KEEP", state="active", payload="KEEP") -> Capability:
    d = copy.deepcopy(_BASE)
    d["id"] = cap_id
    if intents is not None:
        d["trigger"]["intents"] = list(intents)
    if primary is None:
        d["trigger"].pop("primary_intents", None)
    else:
        d["trigger"]["primary_intents"] = list(primary)
    d["lifecycle"]["state"] = state
    if body_hash != "KEEP":
        if body_hash is None:
            d["lifecycle"].pop("body_hash", None)
        else:
            d["lifecycle"]["body_hash"] = body_hash
    if payload != "KEEP":
        d["context"]["payload"] = payload
    return Capability.from_dict(d)


# ── C2 render (happy path, real researcher) ──────────────────────────────────


def test_render_injects_frame_and_body_slug_only(monkeypatch):
    reg.load_capabilities()  # populate _PRIMACY_MAP: research -> researcher
    ctx = {"intent_class": "research", "skill_payload_ceiling": 5000}
    result = _skill_payload_provider(ctx)
    assert result is not None
    text = result.text
    assert result.label == "skill_payload"
    # Static frame + slug interpolation.
    assert "`researcher`" in text
    assert "canonical procedure" in text
    # Body content present (frontmatter-stripped) …
    assert "research analyst" in text
    # … but NO frontmatter prose (the record description) leaked in.
    assert "accepts an article URL or body" not in text
    assert "name: researcher" not in text


def test_no_primary_emits_nothing():
    reg.load_capabilities()
    assert _skill_payload_provider({"intent_class": "conversation",
                                    "skill_payload_ceiling": 5000}) is None
    assert _skill_payload_provider({"skill_payload_ceiling": 5000}) is None


def test_missing_tier_ceiling_emits_nothing():
    reg.load_capabilities()
    # No skill_payload_ceiling in ctx → tier does not admit the block.
    assert _skill_payload_provider({"intent_class": "research"}) is None


def test_oversize_drops_entire_payload(monkeypatch):
    reg.load_capabilities()
    sink: dict = {}
    ctx = {"intent_class": "research", "skill_payload_ceiling": 5,
           "_composer_drops": sink}
    assert _skill_payload_provider(ctx) is None  # body >> 5 tokens
    assert sink.get("skill_payload", {}).get("dropped_blocks") == 1
    assert sink["skill_payload"]["dropped_tokens"] > 5


# ── C2 fail-closed arms (monkeypatched records) ──────────────────────────────


def _wire(monkeypatch, *, record, pin=None, verify=True, calls=None):
    monkeypatch.setattr(reg, "primary_skill_for_intent", lambda i: "myprimary")
    monkeypatch.setattr(reg, "skill_record_for_name", lambda s: record)
    monkeypatch.setattr(reg, "approved_payload_hash_for", lambda rid: pin)
    monkeypatch.setattr(reg, "verify_payload_hash", lambda r: verify)
    if calls is not None:
        monkeypatch.setattr(
            reg, "file_skill_payload_integrity_violation",
            lambda slug, rid, reason: calls.append((slug, rid, reason)),
        )


def test_disabled_record_emits_nothing(monkeypatch):
    rec = _cap(state="proposed", intents=["research"], primary=["research"])
    _wire(monkeypatch, record=rec)
    assert _skill_payload_provider({"intent_class": "research",
                                    "skill_payload_ceiling": 5000}) is None


def test_missing_body_hash_skips_quietly(monkeypatch):
    calls: list = []
    rec = _cap(body_hash=None, intents=["research"], primary=["research"])
    _wire(monkeypatch, record=rec, calls=calls)
    assert _skill_payload_provider({"intent_class": "research",
                                    "skill_payload_ceiling": 5000}) is None
    assert calls == []  # ii-a is quiet — NO Andon


def test_body_hash_mismatch_skips_with_andon(monkeypatch):
    calls: list = []
    rec = _cap(body_hash="sha256:deadbeefdeadbeef",
               intents=["research"], primary=["research"])
    _wire(monkeypatch, record=rec, calls=calls)
    assert _skill_payload_provider({"intent_class": "research",
                                    "skill_payload_ceiling": 5000}) is None
    assert calls and calls[0][2] == "body_hash"


def test_promotion_pin_present_and_mismatched_skips_with_andon(monkeypatch):
    # body_hash green, but a promotion pin EXISTS and no longer matches → skip.
    calls: list = []
    rec = _cap(intents=["research"], primary=["research"])  # body_hash kept (green)
    _wire(monkeypatch, record=rec, pin="somehash", verify=False, calls=calls)
    assert _skill_payload_provider({"intent_class": "research",
                                    "skill_payload_ceiling": 5000}) is None
    assert calls and calls[0][2] == "promotion_pin"


def test_promotion_pin_absent_is_inert(monkeypatch):
    # No pin → the promotion layer is inert; body_hash green → payload injects.
    rec = _cap(intents=["research"], primary=["research"])
    _wire(monkeypatch, record=rec, pin=None)
    result = _skill_payload_provider({"intent_class": "research",
                                      "skill_payload_ceiling": 5000})
    assert result is not None and result.label == "skill_payload"


def test_f2_non_primary_never_loads_payload(monkeypatch):
    # A record with NO primary_intents is never returned by primary_skill_for_intent
    # → the provider emits nothing (payload injection is primary-only).
    monkeypatch.setattr(reg, "primary_skill_for_intent", lambda i: None)
    assert _skill_payload_provider({"intent_class": "research",
                                    "skill_payload_ceiling": 5000}) is None


# ── C2 loader primacy-dark warn ──────────────────────────────────────────────


def test_primacy_dark_warn_fires(monkeypatch, caplog, tmp_path):
    monkeypatch.setattr(reg, "_file_primacy_violations", lambda v: None)
    rec = _cap(cap_id="skill.fleet.darkclaim", intents=["research"],
               primary=["research"], body_hash=None)
    fname = rec.id.replace(".", "__") + ".yaml"
    (tmp_path / fname).write_text(yaml.safe_dump(rec.to_dict(), sort_keys=False),
                                  encoding="utf-8")
    import logging
    with caplog.at_level(logging.WARNING):
        reg.load_capabilities(tmp_path)
    assert any("primacy dark: darkclaim" in r.message for r in caplog.records)


def test_researcher_is_not_dark(monkeypatch, caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        reg.load_capabilities()
    assert not any("primacy dark: researcher" in r.message for r in caplog.records)


# ── C3 recompose keying (Dispatcher shell) ───────────────────────────────────


def _disp(monkeypatch, *, last_primary, primary_map):
    d = Dispatcher.__new__(Dispatcher)
    d._last_applied_tier_context_blocks = "BLOCKS"
    d._last_loaded_primary_slug = last_primary
    d.agent = types.SimpleNamespace(_tier_context_blocks="BLOCKS")  # unchanged
    monkeypatch.setattr(reg, "primary_skill_for_intent",
                        lambda i: primary_map.get(i))
    return d


def _spy(d):
    calls = []
    d.recompose_system_prompt = lambda **kw: calls.append("recompose")
    return calls


def _set_intent(d, intent):
    d._current_turn_classification = types.SimpleNamespace(intent_class=intent)


def test_same_primary_short_circuits(monkeypatch):
    d = _disp(monkeypatch, last_primary="researcher",
              primary_map={"research": "researcher"})
    calls = _spy(d)
    _set_intent(d, "research")
    d._maybe_recompose_for_tier(d.agent)
    assert calls == []  # research -> research, same slug, blocks unchanged


def test_primary_to_none_does_not_recompose(monkeypatch):
    d = _disp(monkeypatch, last_primary="researcher", primary_map={})
    calls = _spy(d)
    _set_intent(d, "conversation")  # no primary
    d._maybe_recompose_for_tier(d.agent)
    assert calls == []  # None primary never forces; payload persists
    assert d._last_loaded_primary_slug == "researcher"  # tracker unchanged


def test_none_to_primary_forces_recompose(monkeypatch):
    d = _disp(monkeypatch, last_primary=None,
              primary_map={"research": "researcher"})
    calls = _spy(d)
    _set_intent(d, "research")
    d._maybe_recompose_for_tier(d.agent)
    assert calls == ["recompose"]  # chat -> research loads the payload


def test_displacement_forces_recompose(monkeypatch):
    d = _disp(monkeypatch, last_primary="skill_a",
              primary_map={"intent_b": "skill_b"})
    calls = _spy(d)
    _set_intent(d, "intent_b")
    d._maybe_recompose_for_tier(d.agent)
    assert calls == ["recompose"]  # different primary displaces


def test_tier_block_change_recomposes_even_with_none_primary(monkeypatch):
    # Boundary: blocks changed + intent None → recompose fires (which will evict
    # the payload, since compose resets the tracker to the current None primary).
    d = _disp(monkeypatch, last_primary="researcher", primary_map={})
    d.agent = types.SimpleNamespace(_tier_context_blocks="NEW_BLOCKS")  # changed
    calls = _spy(d)
    _set_intent(d, "conversation")
    d._maybe_recompose_for_tier(d.agent)
    assert calls == ["recompose"]


def test_current_turn_primary_slug_none_evicts(monkeypatch):
    # The eviction primitive: with a None-primary intent, the helper returns None,
    # so compose_system_prompt sets _last_loaded_primary_slug = None (payload gone).
    d = Dispatcher.__new__(Dispatcher)
    monkeypatch.setattr(reg, "primary_skill_for_intent", lambda i: None)
    _set_intent(d, "conversation")
    assert d._current_turn_primary_slug() is None
    # And a real primary resolves through the same seam.
    monkeypatch.setattr(reg, "primary_skill_for_intent",
                        lambda i: "researcher" if i == "research" else None)
    _set_intent(d, "research")
    assert d._current_turn_primary_slug() == "researcher"
