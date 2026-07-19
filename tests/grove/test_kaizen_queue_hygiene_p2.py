"""kaizen-queue-hygiene-v1 Phase 2 — copy + renderer folds (K-3, K-4, K-5).

  * K-3 — the stale "session-scoped / do not survive a restart" copy is gone from
    BOTH operator-facing sites (the not_found approve error + the EXPIRED card); the
    new copy states the real cause (payload disposed, queue row left behind = orphan).
  * K-4 — governance_env_pending resolves through RENDER_REGISTRY (no ValueError on the
    push/CLI path) and renders from the non-secret ``tool`` field.
  * K-5 — a write_file RED card renders its path + content, never "arguments hidden".
"""

from __future__ import annotations

import pytest

from grove.kaizen.rendering import RENDER_REGISTRY, get_renderer
from grove.red_pending_store import (
    RED_PENDING_PROPOSAL_TYPE,
    RedPendingStore,
    approve_red_proposal,
    describe_red_action,
)


# ── K-4: renderer coverage (push + CLI paths, no ValueError) ─────────────────


def _bridge(tool="propose_governance_change"):
    from grove.eval.proposal_queue import RoutingProposal

    return RoutingProposal(
        proposal_id=f"{RED_PENDING_PROPOSAL_TYPE}:abc",
        type=RED_PENDING_PROPOSAL_TYPE,
        payload={"tool": tool, "zone": "red"},
        evidence=(),
        eval_hash="abc",
        created_at="2026-07-19T00:00:00+00:00",
        proposer="governance",
    )


def test_governance_env_pending_has_renderer():
    assert RED_PENDING_PROPOSAL_TYPE in RENDER_REGISTRY


def test_get_renderer_no_valueerror_and_renders_tool():
    # the exact resolution the conversational push / CLI take — previously raised
    # ValueError (silent-skip); now returns a legible body sourced from `tool`.
    renderer = get_renderer(RED_PENDING_PROPOSAL_TYPE)
    body = renderer(_bridge(tool="terminal"))
    assert isinstance(body, str) and body
    assert "terminal" in body
    assert "portal" in body.lower()


def test_renderer_tolerates_missing_tool():
    body = get_renderer(RED_PENDING_PROPOSAL_TYPE)(_bridge(tool=None))
    assert "action" in body  # graceful default, never crashes


# ── K-5: write_file RED card is legible (render gap, not data gap) ────────────


def test_write_file_red_renders_path_and_content():
    desc, opaque = describe_red_action("write_file", {"path": "/etc/app.conf", "content": "A=1\n"})
    assert opaque is False
    assert "/etc/app.conf" in desc
    assert "A=1" in desc
    assert "arguments hidden" not in desc


def test_write_file_red_content_preview_bounded():
    big = "x" * 500
    desc, _ = describe_red_action("write_file", {"path": "/p", "content": big})
    assert "/p" in desc
    assert "..." in desc and "arguments hidden" not in desc
    assert len(desc) < 300  # bounded preview, not the whole 500-char body


def test_unknown_tool_still_hides_arguments():
    # the generic fallback is intact for tools with no legible-by-design branch
    desc, _ = describe_red_action("some_mcp_tool", {"secret_arg": "v"})
    assert "arguments hidden" in desc


# ── K-3: the corrected copy (no session-scoped claim) ────────────────────────


def test_not_found_copy_states_real_cause(tmp_path):
    store = RedPendingStore(db_path=tmp_path / "red.db")  # empty → pop returns None
    result = approve_red_proposal("no-such-id", store=store)
    assert result["success"] is False and result["reason"] == "not_found"
    err = result["error"]
    assert "session-scoped" not in err
    assert "do not survive" not in err
    assert "survives restarts" in err  # states the store IS durable


def test_expired_card_copy_states_real_cause(tmp_path):
    from grove.api.fragments import _render_red_proposal_card

    store = RedPendingStore(db_path=tmp_path / "red.db")  # no payload → orphan/EXPIRED

    class _Req:
        app = {"red_pending_store": store}

    html = _render_red_proposal_card(_Req(), f"{RED_PENDING_PROPOSAL_TYPE}:gone", "gone")
    assert "expired" in html.lower()
    assert "session-scoped" not in html
    assert "orphan" in html.lower()  # the real cause named
    assert "Dismiss" in html
