"""portal-action-error-surfacing-v1 (Phase 1) — agentless proposal filing
and the ``portal_action_failure`` render registration.

Standalone: file_agentless_proposal writes to a tmp queue path; the render
assertions read the in-process RENDER_REGISTRY. No gateway, no deploy.
"""

from __future__ import annotations

import pytest

from grove import flywheel_cli
from grove.eval.proposal_queue import (
    PROPOSAL_TYPE_PORTAL_ACTION_FAILURE,
    RoutingProposal,
    compute_proposal_id,
    file_agentless_proposal,
    read_all,
)


def _queue(tmp_path):
    return tmp_path / "proposals.jsonl"


# ── Dedup: identity is the (failure_class, action, evidence) triad ───


class TestAgentlessFilingDedup:
    def test_same_class_same_id_and_flood_guarded(self, tmp_path):
        q = _queue(tmp_path)
        id1, ok1 = file_agentless_proposal(
            failure_class="notion_cold_session",
            action="forge_publish",
            evidence="drive_ok_notion_cold",
            justification="Notion MCP was cold at publish.",
            instance={"slug": "acme-eng", "at": "2026-07-03T10:00:00Z"},
            path=q,
        )
        id2, ok2 = file_agentless_proposal(
            failure_class="notion_cold_session",
            action="forge_publish",
            evidence="drive_ok_notion_cold",
            justification="Notion MCP was cold at publish.",
            # DIFFERENT ephemeral instance — must not change identity.
            instance={"slug": "beta-corp", "at": "2026-07-03T11:30:00Z"},
            path=q,
        )
        assert id1 == id2  # same stable class → same id
        assert ok1 is True
        assert ok2 is False  # second identical proposal deduped (flood-guard)
        assert len(read_all(path=q)) == 1

    def test_varying_instance_does_not_change_identity(self, tmp_path):
        q = _queue(tmp_path)
        base = dict(
            failure_class="drive_quota",
            action="forge_publish",
            evidence="drive_429",
            justification="Drive rejected the upload.",
        )
        id_a, _ = file_agentless_proposal(instance={"n": 1}, path=q, **base)
        id_b, _ = file_agentless_proposal(instance={"n": 999, "x": "y"}, path=q, **base)
        assert id_a == id_b

    def test_different_failure_class_different_id(self, tmp_path):
        q = _queue(tmp_path)
        id_a, _ = file_agentless_proposal(
            failure_class="notion_cold_session",
            action="forge_publish",
            evidence="e",
            justification="j",
            path=q,
        )
        id_b, _ = file_agentless_proposal(
            failure_class="drive_quota",
            action="forge_publish",
            evidence="e",
            justification="j",
            path=q,
        )
        assert id_a != id_b
        assert len(read_all(path=q)) == 2

    def test_id_matches_compute_proposal_id_over_stable_fields(self, tmp_path):
        q = _queue(tmp_path)
        got, _ = file_agentless_proposal(
            failure_class="fc",
            action="act",
            evidence="ev",
            justification="ignored for identity",
            instance={"ephemeral": "ignored too"},
            path=q,
        )
        expected = compute_proposal_id(
            type=PROPOSAL_TYPE_PORTAL_ACTION_FAILURE,
            payload={"failure_class": "fc", "action": "act"},
            evidence=("ev",),
        )
        assert got == expected

    def test_instance_detail_lands_in_excluded_justification(self, tmp_path):
        q = _queue(tmp_path)
        file_agentless_proposal(
            failure_class="fc",
            action="act",
            evidence="ev",
            justification="cold session",
            instance={"slug": "acme"},
            path=q,
        )
        [rec] = read_all(path=q)
        # Ephemeral detail is carried in the EXCLUDED field, not the payload.
        assert "slug=acme" in rec.semantic_justification
        assert "cold session" in rec.semantic_justification
        assert rec.payload == {"failure_class": "fc", "action": "act"}


# ── Type registration + render ───────────────────────────────────────


class TestRenderRegistration:
    def test_type_in_render_registry(self):
        assert PROPOSAL_TYPE_PORTAL_ACTION_FAILURE in flywheel_cli.RENDER_REGISTRY
        renderer = flywheel_cli.get_renderer(PROPOSAL_TYPE_PORTAL_ACTION_FAILURE)
        assert callable(renderer)

    def test_type_has_push_priority(self):
        assert PROPOSAL_TYPE_PORTAL_ACTION_FAILURE in flywheel_cli._PUSH_PRIORITY

    def _proposal(self):
        payload = {"failure_class": "notion_cold_session", "action": "forge_publish"}
        return RoutingProposal(
            proposal_id=compute_proposal_id(
                type=PROPOSAL_TYPE_PORTAL_ACTION_FAILURE,
                payload=payload,
                evidence=("ev",),
            ),
            type=PROPOSAL_TYPE_PORTAL_ACTION_FAILURE,
            payload=payload,
            evidence=("ev",),
            eval_hash="",
            created_at="2026-07-03T00:00:00+00:00",
            semantic_justification="Notion MCP was cold at publish.",
        )

    def test_compose_offering_pull_body_renders(self):
        body = flywheel_cli.compose_offering(self._proposal(), is_push=False)
        assert "forge_publish" in body
        assert "notion_cold_session" in body

    def test_compose_offering_push_note_is_dismiss_only(self):
        note = flywheel_cli.compose_offering(self._proposal(), is_push=True)
        # In-chat offering (requires_portal_review is False for this type), but
        # DISMISS-ONLY: approve dead-ends at _handler_for (render-only type), so
        # the push must not offer it. The shop-floor frame + dismiss remain.
        assert flywheel_cli._OFFERING_PUSH_PREFIX in note
        assert "dismiss" in note.lower()
        assert "approve" not in note.lower()

    def test_offers_approve_is_false(self):
        # The type-ignorant opt-out the composer branches on.
        assert self._proposal().offers_approve is False

    # Regression: offers_approve now gates approve visibility for the WHOLE
    # surface, so handler-backed types must still resolve True AND render the
    # approve tail — the dismiss-only branch is scoped to render-only types only.
    @pytest.mark.parametrize(
        "ptype, payload",
        [
            ("routing_adjustment", {"rule": "downward", "add_intents": ["conversation"]}),
            ("skill_synthesis", {"skill_name": "foo"}),
            ("zone_promotion", {"tool": "read_file", "pattern": "*.md"}),
            ("skill_promotion", {"skill_name": "bar"}),
            ("pattern_promotion", {"intent_class": "x", "cacheable_type": "y"}),
        ],
    )
    def test_handler_backed_types_keep_approve(self, ptype, payload):
        proposal = RoutingProposal(
            proposal_id=compute_proposal_id(type=ptype, payload=payload, evidence=("t",)),
            type=ptype,
            payload=payload,
            evidence=("t",),
            eval_hash="",
            created_at="2026-07-03T00:00:00+00:00",
        )
        assert proposal.offers_approve is True
        note = flywheel_cli.compose_offering(proposal, is_push=True)
        assert "approve" in note.lower()
        assert "dismiss" in note.lower()

    def test_all_proposal_handlers_rows_resolve_offers_approve_true(self):
        # Surface-wide invariant: NO type with an apply handler is silently
        # stripped of approve by the render-only denylist.
        from grove.flywheel_cli import PROPOSAL_HANDLERS

        for ptype in PROPOSAL_HANDLERS:
            proposal = RoutingProposal(
                proposal_id=compute_proposal_id(type=ptype, payload={}, evidence=("t",)),
                type=ptype,
                payload={},
                evidence=("t",),
                eval_hash="",
                created_at="2026-07-03T00:00:00+00:00",
            )
            assert proposal.offers_approve is True, f"{ptype} lost approve"

    def test_not_portal_review_gated(self):
        # Phase-1 decision: in-chat offering, NOT a portal-only review type.
        assert self._proposal().requires_portal_review is False


class TestMemoryPushFallbackLandmine:
    """The composer's ``offers_approve`` branch (added for portal_action_failure)
    is read on EVERY renderable that reaches the in-chat note — including a
    ``MemoryProposalRenderable`` in the portal-URL-unresolved fallback
    (``requires_portal_review`` True + no ``portal_base_url`` → verbose in-chat).
    Without ``offers_approve`` on that class the branch AttributeErrors. These
    guard the landmine the flywheel_cli.py edit would otherwise introduce."""

    def _memory_renderable(self):
        from grove.kaizen.renderable import MemoryProposalRenderable

        return MemoryProposalRenderable(
            {
                "status": "pending",
                "proposal": {
                    "action": "crystallize",
                    "proposed_record": {"confidence": 0.9, "content": "x"},
                },
            }
        )

    def test_memory_fallback_renders_without_attributeerror(self):
        rend = self._memory_renderable()
        note = flywheel_cli.compose_offering(
            rend, is_push=True, portal_base_url=None
        )
        assert flywheel_cli._OFFERING_PUSH_PREFIX in note
        # Memory HAS an apply path (offers_approve True) → keeps approve/dismiss.
        assert "approve" in note.lower()
        assert "dismiss" in note.lower()

    def test_offers_approve_is_load_bearing_on_the_fallback(self, monkeypatch):
        # Strip the property (the pre-edit state) and the same fallback call must
        # fail LOUD (AttributeError) — never a silent swallow / empty return.
        from grove.kaizen.renderable import MemoryProposalRenderable

        rend = self._memory_renderable()
        monkeypatch.delattr(MemoryProposalRenderable, "offers_approve")
        with pytest.raises(AttributeError):
            flywheel_cli.compose_offering(rend, is_push=True, portal_base_url=None)
