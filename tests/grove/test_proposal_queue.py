"""Sprint 47 — proposal queue tests (GRV-008 § II)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from grove.eval.proposal_queue import (
    RoutingProposal,
    append,
    compute_eval_hash,
    compute_proposal_id,
    read,
    read_all,
    remove,
)


def _proposal(
    *,
    rule: str = "downward",
    intents=("conversation",),
    evidence=("t_001", "t_002"),
    eval_hash: str = "sha256:abc",
) -> RoutingProposal:
    payload = {"rule": rule, "add_intents": list(intents)}
    return RoutingProposal(
        proposal_id=compute_proposal_id(
            type="routing_update",
            payload=payload,
            evidence=tuple(evidence),
        ),
        type="routing_update",
        payload=payload,
        evidence=tuple(evidence),
        eval_hash=eval_hash,
        created_at="2026-05-30T00:00:00+00:00",
    )


# ── Hash semantics ───────────────────────────────────────────────────


class TestProposalIdHash:
    def test_same_input_same_id(self) -> None:
        a = compute_proposal_id(
            type="routing_update",
            payload={"rule": "downward", "add_intents": ["conversation"]},
            evidence=("t_a", "t_b"),
        )
        b = compute_proposal_id(
            type="routing_update",
            payload={"rule": "downward", "add_intents": ["conversation"]},
            evidence=("t_a", "t_b"),
        )
        assert a == b
        assert a.startswith("sha256:")

    def test_payload_order_irrelevant(self) -> None:
        a = compute_proposal_id(
            type="routing_update",
            payload={"rule": "downward", "add_intents": ["a", "b"]},
            evidence=("t_1",),
        )
        # JSON sorted-keys serialization makes payload field order
        # irrelevant; the test asserts the docstring contract.
        b = compute_proposal_id(
            type="routing_update",
            payload={"add_intents": ["a", "b"], "rule": "downward"},
            evidence=("t_1",),
        )
        assert a == b

    def test_evidence_order_irrelevant(self) -> None:
        a = compute_proposal_id(
            type="routing_update",
            payload={"rule": "downward", "add_intents": ["x"]},
            evidence=("t_a", "t_b"),
        )
        b = compute_proposal_id(
            type="routing_update",
            payload={"rule": "downward", "add_intents": ["x"]},
            evidence=("t_b", "t_a"),
        )
        assert a == b

    def test_different_evidence_different_id(self) -> None:
        a = compute_proposal_id(
            type="routing_update",
            payload={"rule": "downward", "add_intents": ["x"]},
            evidence=("t_a",),
        )
        b = compute_proposal_id(
            type="routing_update",
            payload={"rule": "downward", "add_intents": ["x"]},
            evidence=("t_b",),
        )
        assert a != b


class TestEvalHash:
    def test_confidence_independence(self) -> None:
        """Confidence-band variance MUST NOT invalidate the eval_hash."""

        class _Result:
            def __init__(self, conf: float):
                self.prompt_id = "p1"
                self.observed_intent = "planning"
                self.observed_complexity = "moderate"
                self.observed_tier = "T2"
                self.observed_tools = {"write_file", "web_search"}
                self.observed_confidence = conf
                self.passed = True

        class _Report:
            def __init__(self, conf):
                self.results = (_Result(conf),)

        h_low = compute_eval_hash(_Report(0.82))
        h_high = compute_eval_hash(_Report(0.95))
        assert h_low == h_high

    def test_intent_change_changes_hash(self) -> None:
        class _Result:
            def __init__(self, intent):
                self.prompt_id = "p1"
                self.observed_intent = intent
                self.observed_complexity = "moderate"
                self.observed_tier = "T2"
                self.observed_tools = set()
                self.observed_confidence = 0.9
                self.passed = True

        class _Report:
            def __init__(self, intent):
                self.results = (_Result(intent),)

        h_a = compute_eval_hash(_Report("planning"))
        h_b = compute_eval_hash(_Report("code_generation"))
        assert h_a != h_b


# ── Queue I/O ────────────────────────────────────────────────────────


class TestQueueAppendRead:
    def test_append_and_read_one(self, tmp_path: Path) -> None:
        queue = tmp_path / "proposals.jsonl"
        prop = _proposal()
        assert append(prop, path=queue) is True
        all_props = read_all(path=queue)
        assert len(all_props) == 1
        assert all_props[0] == prop

    def test_idempotent_on_duplicate_id(self, tmp_path: Path) -> None:
        queue = tmp_path / "proposals.jsonl"
        prop = _proposal()
        assert append(prop, path=queue) is True
        assert append(prop, path=queue) is False
        assert len(read_all(path=queue)) == 1

    def test_read_by_id(self, tmp_path: Path) -> None:
        queue = tmp_path / "proposals.jsonl"
        prop = _proposal()
        append(prop, path=queue)
        looked = read(prop.proposal_id, path=queue)
        assert looked == prop
        assert read("sha256:does-not-exist", path=queue) is None

    def test_remove_compacts_queue(self, tmp_path: Path) -> None:
        queue = tmp_path / "proposals.jsonl"
        a = _proposal(evidence=("t_a",))
        b = _proposal(evidence=("t_b",))
        append(a, path=queue)
        append(b, path=queue)
        assert len(read_all(path=queue)) == 2
        assert remove(a.proposal_id, path=queue) is True
        remaining = read_all(path=queue)
        assert len(remaining) == 1
        assert remaining[0].proposal_id == b.proposal_id

    def test_remove_last_deletes_file(self, tmp_path: Path) -> None:
        queue = tmp_path / "proposals.jsonl"
        prop = _proposal()
        append(prop, path=queue)
        assert queue.exists()
        remove(prop.proposal_id, path=queue)
        assert not queue.exists()

    def test_remove_unknown_returns_false(self, tmp_path: Path) -> None:
        queue = tmp_path / "proposals.jsonl"
        prop = _proposal()
        append(prop, path=queue)
        assert remove("sha256:unknown", path=queue) is False
        assert len(read_all(path=queue)) == 1

    def test_corrupted_line_skipped(self, tmp_path: Path) -> None:
        queue = tmp_path / "proposals.jsonl"
        prop = _proposal()
        append(prop, path=queue)
        with open(queue, "a", encoding="utf-8") as fh:
            fh.write("this is not json\n")
        # read_all skips the corrupted line at debug; no crash.
        loaded = read_all(path=queue)
        assert len(loaded) == 1


# ── detail envelope (proposal-card-legibility-v1 Phase 2) ────────────


class TestDetailEnvelope:
    def test_identity_excluded_by_signature(self) -> None:
        """The hash seed is type|payload|evidence and NOTHING else — detail
        (like lease/proposer/source_patterns) is structurally excluded because
        compute_proposal_id has no parameter for it. Pin the signature so a
        future envelope field can't quietly join the identity."""
        import inspect
        params = set(inspect.signature(compute_proposal_id).parameters)
        assert params == {"type", "payload", "evidence"}

    def test_to_dict_omits_none_detail(self) -> None:
        """A detail-less proposal serializes WITHOUT the key — never null."""
        data = _proposal().to_dict()
        assert "detail" not in data

    def test_to_dict_carries_detail_when_set(self, tmp_path: Path) -> None:
        detail = {"samples": [{"ts": "2026-07-11", "subject": "terminal",
                               "outcome": "cancel"}]}
        prop = RoutingProposal(
            **{**_proposal().to_dict(), "evidence": ("t_001", "t_002"),
               "detail": detail},
        )
        assert prop.to_dict()["detail"] == detail
        # Round-trips through the queue file intact.
        queue = tmp_path / "proposals.jsonl"
        append(prop, path=queue)
        assert read_all(path=queue)[0].detail == detail

    def test_read_absent_and_null_both_none(self, tmp_path: Path) -> None:
        """Legacy record (no key) and explicit null both deserialize to None."""
        queue = tmp_path / "proposals.jsonl"
        prop = _proposal()
        append(prop, path=queue)  # written WITHOUT a detail key
        as_null = dict(prop.to_dict())
        as_null["proposal_id"] = "sha256:" + "0" * 64
        as_null["detail"] = None  # explicit null on disk
        with open(queue, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(as_null) + "\n")
        loaded = read_all(path=queue)
        assert len(loaded) == 2
        assert loaded[0].detail is None
        assert loaded[1].detail is None
