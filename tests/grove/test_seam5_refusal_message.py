"""C-SEAM5 refusal message legibility (GRV-009 §V).

The refusal message must name the governing capability record, its config path,
and its trigger.intents/always/disclosure — and mark the routing tier
non-determinative. Foregrounding tier= is what misled the diagnosing Autonomaton
in the original bug report.
"""
from __future__ import annotations

from grove.capability_registry import load_capabilities
from run_agent import _seam5_refusal_message


def test_message_names_record_and_marks_tier_non_determinative():
    rec = load_capabilities()["session_search"]  # any proactive+intents record
    msg = _seam5_refusal_message(
        "session_search",
        "not in the per-turn offered surface",
        intent="creative_writing",
        tier="T2",
        record=rec,
    )
    # Names the human-readable rule the operator can edit.
    assert "config/capabilities/session_search.yaml" in msg
    assert "session_search.trigger.intents" in msg
    # Reports the record's actual admission fields.
    assert str(sorted(rec.trigger.intents)) in msg
    assert "always=" in msg and "disclosure=" in msg
    # Tier is explicitly non-determinative, not the stated cause.
    assert "non-determinative" in msg
    assert "T2" in msg


def test_message_without_record_still_avoids_tier_as_cause():
    msg = _seam5_refusal_message(
        "some_unregistered_tool",
        "not in the per-turn offered surface",
        intent="conversation",
        tier="T1",
        record=None,
    )
    assert "non-determinative" in msg
    assert "some_unregistered_tool" in msg
