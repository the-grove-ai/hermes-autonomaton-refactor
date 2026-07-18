"""skill-adoption-v1 P3 — the t3-fallback rider: the T3->T2 declaration and the
operator-legible downshift line (no silent downshift).

The governed-downshift MECHANISM (GRV-010 C2d) is pre-existing and covered by
test_c2d_tier_unavailable.py; this file covers the Phase-3 additions — the repo
config declaration and the answer-then-surface legibility line.
"""

from __future__ import annotations

from pathlib import Path

import yaml

import run_agent


REPO_ROUTING = (
    Path(__file__).resolve().parents[2] / "config" / "routing.config.yaml"
)


def test_repo_config_declares_t3_fallback_to_t2():
    raw = yaml.safe_load(REPO_ROUTING.read_text(encoding="utf-8"))
    t3 = raw["routing"]["tier_preferences"]["T3"]
    assert t3.get("fallback_tier") == "T2"


def _agent():
    return run_agent.AIAgent.__new__(run_agent.AIAgent)


def test_notice_renders_legible_line_when_set():
    a = _agent()
    a._tier_fallback_notice = {"failed_tier": "T3", "fallback_tier": "T2"}
    out = a._append_tier_fallback_notice("Here is your answer.")
    assert "Here is your answer." in out
    assert "T3" in out and "T2" in out
    assert "unavailable" in out and "fallback" in out
    # Consumed — a second call does not re-append.
    assert a._tier_fallback_notice is None
    assert a._append_tier_fallback_notice(out) == out


def test_notice_noop_when_unset():
    a = _agent()
    a._tier_fallback_notice = None
    assert a._append_tier_fallback_notice("answer") == "answer"


def test_notice_noop_on_empty_response():
    a = _agent()
    a._tier_fallback_notice = {"failed_tier": "T3", "fallback_tier": "T2"}
    assert a._append_tier_fallback_notice("") == ""
