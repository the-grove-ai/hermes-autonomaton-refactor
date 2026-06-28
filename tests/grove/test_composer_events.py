"""composer-observability-v1 (Wave 1) — acceptance tests for the composer
event sink.

Covers the SPEC acceptance criteria AC-1..AC-9 for ``grove.composer_events``
(the writer + envelope builder), the F1 instrumentation in
``grove.prompt.composer.compose``, and the F2 instrumentation in the cellar /
memory providers. The cardinal invariant under test is AC-3: NO prompt text
ever reaches the sink — structure + token math only.

Every writer points at ``tmp_path`` — no test touches the real
``~/.grove/composer_events.jsonl``.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pytest

from grove.composer_events import (
    SCHEMA_VERSION,
    ComposerEventWriter,
    build_composer_event,
    get_writer,
)
from grove.prompt import build_default_composer
from grove.prompt.composer import (
    PromptComposer,
    SectionResult,
    _PROVIDER_GATEABLE_BLOCK,
)
from grove.tier_budget import GATEABLE_CONTEXT_BLOCKS


# ── helpers ──────────────────────────────────────────────────────────────


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit(writer: ComposerEventWriter, **kwargs: Any) -> Dict[str, Any]:
    """Build + emit one event, returning the written dict."""
    kwargs.setdefault("correlation_key", "sess#1")
    kwargs.setdefault("compose_tier", "T2")
    kwargs.setdefault("budget_ceiling", None)
    kwargs.setdefault("timestamp", _ts())
    return writer.emit(build_composer_event(**kwargs))


def _included_provider(label: str, text: str):
    def _p(_ctx: Dict[str, Any]) -> Optional[SectionResult]:
        return SectionResult(label=label, text=text)
    return _p


def _throwing_provider(message: str):
    def _p(_ctx: Dict[str, Any]) -> Optional[SectionResult]:
        raise RuntimeError(message)
    return _p


def _budget_truncating_provider(label: str, blocks: int, tokens: int):
    """Mimic the cellar/memory F2 contract: write the compose-seeded drop sink
    and still serve surviving content."""
    def _p(ctx: Dict[str, Any]) -> Optional[SectionResult]:
        sink = ctx.get("_composer_drops")
        if sink is not None:
            sink[label] = {"dropped_blocks": blocks, "dropped_tokens": tokens}
        return SectionResult(label=label, text="surviving content for " + label)
    return _p


def _matrix_composer() -> PromptComposer:
    """A composer exercising all four status_reasons in ONE compose():
    included, exception_dropped (F1), tier_gated, budget_truncated (F2)."""
    c = PromptComposer()
    c.register_section(
        "identity", _included_provider("identity", "You are an autonomaton. " * 4),
        order=10, tier="stable",
    )
    # gateable provider that will be gated off via the allow-list
    c.register_section(
        "cellar_knowledge", _included_provider("cellar_knowledge", "cellar text"),
        order=11, tier="context",
    )
    # F1: throws with a >50-char message (AC-3 leak bait)
    c.register_section(
        "memory", _throwing_provider("boom with a long secret message " + "Z" * 60),
        order=10, tier="volatile",
    )
    # F2: non-gateable greedy-fill drop
    c.register_section(
        "accumulated_domain_memory",
        _budget_truncating_provider("accumulated_domain_memory", blocks=2, tokens=40),
        order=15, tier="context",
    )
    return c


def _longest_strings(obj: Any, limit: int = 50):
    if isinstance(obj, str):
        return [obj] if len(obj) > limit else []
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _longest_strings(v, limit)]
    if isinstance(obj, list):
        return [s for v in obj for s in _longest_strings(v, limit)]
    return []


# ── AC-1: one record per compose, envelope + one record per provider ──────


def test_ac1_one_record_per_compose_with_envelope(tmp_path):
    composer = build_default_composer(config=None)
    result = composer.compose(model="m", provider="p", platform="cli", session_id="s")
    views = composer.registered_provider_views()

    writer = ComposerEventWriter(tmp_path / "ev.jsonl")
    _emit(writer, result=result, provider_views=views, correlation_key="s#3")

    lines = (tmp_path / "ev.jsonl").read_text().splitlines()
    assert len(lines) == 1                      # one record per compose() call
    rec = json.loads(lines[0])
    # envelope present
    for key in ("schema_version", "correlation_key", "compose_seq",
                "compose_tier", "total_tokens", "budget_ceiling",
                "providers", "timestamp"):
        assert key in rec, f"missing envelope key {key}"
    assert rec["schema_version"] == SCHEMA_VERSION
    assert rec["correlation_key"] == "s#3"
    assert rec["compose_seq"] == 0
    # one provider record per ENABLED registered provider
    assert len(rec["providers"]) == len(views)
    assert {p["provider_id"] for p in rec["providers"]} == {v[0] for v in views}


# ── AC-2: status_reason classification matrix ─────────────────────────────


def test_ac2_status_reason_classification(tmp_path):
    composer = _matrix_composer()
    # allow-list present and EXCLUDING cellar_context → cellar_knowledge gated.
    result = composer.compose(
        tier_context_blocks=frozenset({"claude_contract", "skills_index"}),
    )
    rec = _emit(
        ComposerEventWriter(tmp_path / "ev.jsonl"),
        result=result, provider_views=composer.registered_provider_views(),
    )
    by = {p["provider_id"]: p for p in rec["providers"]}
    assert by["identity"]["status_reason"] == "included"
    assert by["memory"]["status_reason"] == "exception_dropped"
    assert by["cellar_knowledge"]["status_reason"] == "tier_gated"
    assert by["accumulated_domain_memory"]["status_reason"] == "budget_truncated"


# ── AC-3: NO prompt text in payload ───────────────────────────────────────


def test_ac3_no_prompt_text_in_payload(tmp_path):
    composer = _matrix_composer()
    result = composer.compose(
        tier_context_blocks=frozenset({"claude_contract", "skills_index"}),
    )
    rec = _emit(
        ComposerEventWriter(tmp_path / "ev.jsonl"),
        result=result, provider_views=composer.registered_provider_views(),
    )
    # The included provider text ("You are an autonomaton. " * 4 ≈ 96 chars) and
    # the F1 exception message (>50 chars) are both leak bait. Neither may
    # appear: no string field longer than 50 chars anywhere in the payload.
    leaks = _longest_strings(rec)
    assert not leaks, f"prompt-text leak: {leaks}"
    blob = json.dumps(rec)
    assert "autonomaton" not in blob
    assert "secret" not in blob and "ZZZ" not in blob


# ── AC-4: correlation_key round-trips across both sinks ───────────────────


def test_ac4_correlation_key_roundtrip_with_intent_store(tmp_path):
    from grove.intent_store import IntentRecord, IntentStore

    turn_id = "sessXYZ#42"

    # composer sink carries correlation_key
    composer = build_default_composer(config=None)
    result = composer.compose(session_id="sessXYZ")
    cw = ComposerEventWriter(tmp_path / "composer_events.jsonl")
    ev = _emit(cw, result=result,
               provider_views=composer.registered_provider_views(),
               correlation_key=turn_id)

    # intent sink keys on turn_id
    store = IntentStore(tmp_path / "intent_records.jsonl")
    store.append(IntentRecord(
        timestamp=_ts(), session_id="sessXYZ", turn_id=turn_id,
        user_message_stem="hi", pattern_hash="abc", intent_class="chat",
        register_class="conversational", complexity_signal="low",
        confidence=0.9, outcome="success",
    ))
    intent_rec = next(iter(store.records()))

    # the SAME key joins the two feeds
    assert ev["correlation_key"] == intent_rec.turn_id == turn_id


# ── AC-5: is_gateable derived from GATEABLE_CONTEXT_BLOCKS, not hardcoded ──


def test_ac5_is_gateable_derived(tmp_path):
    composer = build_default_composer(config=None)
    result = composer.compose(session_id="s")
    rec = _emit(
        ComposerEventWriter(tmp_path / "ev.jsonl"),
        result=result, provider_views=composer.registered_provider_views(),
    )
    gateable_ids = {p["provider_id"] for p in rec["providers"] if p["is_gateable"]}

    # expectation derived from the live mapping + GATEABLE_CONTEXT_BLOCKS —
    # NOT a hardcoded {claude_contract, skills_index, cellar_context} literal.
    registered = {v[0] for v in composer.registered_provider_views()}
    expected = {
        name for name, block in _PROVIDER_GATEABLE_BLOCK.items()
        if block in GATEABLE_CONTEXT_BLOCKS and name in registered
    }
    assert gateable_ids == expected
    # and the blocks they gate are exactly the gateable set
    assert {_PROVIDER_GATEABLE_BLOCK[n] for n in gateable_ids} == set(GATEABLE_CONTEXT_BLOCKS)
    # everything else is non-gateable
    assert all(
        not p["is_gateable"]
        for p in rec["providers"] if p["provider_id"] not in expected
    )


# ── AC-6: writer uses synchronous lock-guarded append ─────────────────────


def test_ac6_writer_uses_lock_guarded_append(tmp_path):
    writer = ComposerEventWriter(tmp_path / "ev.jsonl")
    # real lock is a threading lock
    assert isinstance(writer._lock, type(threading.Lock()))

    class _TrackingLock:
        def __init__(self, inner):
            self.inner = inner
            self.entered = 0
        def __enter__(self):
            self.entered += 1
            return self.inner.__enter__()
        def __exit__(self, *a):
            return self.inner.__exit__(*a)

    track = _TrackingLock(writer._lock)
    writer._lock = track
    writer.emit({"schema_version": SCHEMA_VERSION, "providers": []})
    writer.emit({"schema_version": SCHEMA_VERSION, "providers": []})
    # the lock is acquired once per emit (the append happens inside it)
    assert track.entered == 2
    # and compose_seq advanced monotonically under that lock
    recs = [json.loads(l) for l in (tmp_path / "ev.jsonl").read_text().splitlines()]
    assert [r["compose_seq"] for r in recs] == [0, 1]


def test_ac6_emit_fail_loud_on_missing_schema_version(tmp_path):
    writer = ComposerEventWriter(tmp_path / "ev.jsonl")
    with pytest.raises(ValueError, match="schema_version"):
        writer.emit({"providers": []})


# ── AC-8: F1 exception → exception_dropped + visible in /context excluded ─


def test_ac8_f1_exception_dropped_and_in_context_excluded(tmp_path):
    from grove.context_report import build_context_report

    c = PromptComposer()
    c.register_section(
        "identity", _included_provider("identity", "alive"), order=10, tier="stable",
    )
    # gateable provider that THROWS while running (not gated) → F1
    c.register_section(
        "context_files", _throwing_provider("kaboom " + "Q" * 60),
        order=20, tier="context",
    )
    result = c.compose(
        tier_context_blocks=frozenset(GATEABLE_CONTEXT_BLOCKS),  # nothing gated off
    )

    rec = _emit(
        ComposerEventWriter(tmp_path / "ev.jsonl"),
        result=result, provider_views=c.registered_provider_views(),
    )
    by = {p["provider_id"]: p for p in rec["providers"]}
    assert by["context_files"]["status_reason"] == "exception_dropped"
    assert by["context_files"]["detail"] == {"exception_class": "RuntimeError"}

    # AC-8: the dropped block surfaces in /context's excluded list. The gateable
    # provider's BLOCK name (claude_contract) is what /context shows.
    assert "claude_contract" in result.gated_context_blocks

    class _Ag:
        pass
    agent = _Ag()
    agent._composed_prompt = result
    report = build_context_report(agent)
    assert "claude_contract" in report.excluded_context_blocks


# ── AC-9: F2 truncation → budget_truncated with positive drop counts ──────


def test_ac9_f2_budget_truncated_real_memory_provider(tmp_path, monkeypatch):
    import grove.memory.provider as memprov

    class _Rec:
        def __init__(self, _id):
            self.id = _id

    class _Store:
        def query(self, **_kw):
            return [_Rec("a"), _Rec("b"), _Rec("c")]
        def mark_accessed(self, *_a):
            pass

    monkeypatch.setattr(memprov, "_format_line", lambda r: "X" * 80)        # cost 20 each
    monkeypatch.setattr(memprov, "_orphaned_graduated_records", lambda s: [])

    prov = memprov.create_memory_provider(
        token_budget=25,                       # 1 line fits, 2 drop
        store_factory=lambda: _Store(),
        dock_goals_loader=lambda: [{"slug": "g"}],
    )
    c = PromptComposer()
    c.register_section("accumulated_domain_memory", prov, order=15, tier="context")
    result = c.compose()

    rec = _emit(
        ComposerEventWriter(tmp_path / "ev.jsonl"),
        result=result, provider_views=c.registered_provider_views(),
    )
    p = {x["provider_id"]: x for x in rec["providers"]}["accumulated_domain_memory"]
    assert p["status_reason"] == "budget_truncated"
    assert p["detail"]["dropped_blocks"] > 0
    assert p["detail"]["dropped_tokens"] > 0
    # the surviving line is still counted in the section total (ceiling measure)
    assert p["measured_tokens"] > 0


# ── singleton accessor ────────────────────────────────────────────────────


def test_get_writer_is_singleton(monkeypatch):
    import grove.composer_events as ce
    monkeypatch.setattr(ce, "_default_writer", None)
    assert get_writer() is get_writer()
