"""Unit tests for the host-side retrieval broker (researcher-retrieval-broker-v1
Phase 1). All offline — every external effect is injected. No network, no model.

Covers the SPEC test list: each bound + its failure, budget-exceeded raises (no
partial), truncation flag + bytes_original, content_sha256 exactness, URL topic =
one source / zero queries, out-of-allowlist scheme rejected loudly, malformed
formulation rejected by validation — plus the adapter raw-path constraint,
source cap, per-source drop, private-URL rejection, and the plain-text guarantee.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from grove.fleet.retrieval_broker import (
    MAX_CONTENT_BYTES,
    MAX_SOURCES,
    MAX_TOTAL_MATERIALS_BYTES,
    BrokerBudgetExceeded,
    BrokerMaterialsCeilingExceeded,
    BrokerQueryFormulationError,
    BrokerRequestError,
    BrokerURLRejected,
    RawSource,
    _adapter_web_extract,
    _adapter_web_search,
    _adapter_wiki,
    run_broker,
)


# ── helpers ──────────────────────────────────────────────────────────────────
def _run(coro):
    return asyncio.run(coro)


def _src(raw_content="body", *, url="https://s/1", capability="web_search", query="q", title="T"):
    return RawSource(
        url=url,
        title=title,
        raw_content=raw_content,
        http_status=200,
        content_type="text/plain",
        capability=capability,
        query=query,
    )


def _adapter(sources):
    async def _a(term):
        return list(sources)

    return _a


def _raising_adapter(msg="adapter should not be called"):
    async def _a(term):
        raise AssertionError(msg)

    return _a


def _model_returning(queries):
    def _call(**kwargs):
        return {"queries": list(queries)}

    return _call


def _model_raw(value):
    def _call(**kwargs):
        return value

    return _call


class _FakeClock:
    """Returns queued values in order, clamping to the last (for check_phase)."""

    def __init__(self, values):
        self._values = list(values)
        self._i = 0

    def __call__(self):
        v = self._values[min(self._i, len(self._values) - 1)]
        self._i += 1
        return v


# ── happy path (subject) ──────────────────────────────────────────────────────
def test_happy_path_subject_shape_and_fields():
    ws = _adapter([_src("web-body", url="https://a", capability="web_search", query="q1")])
    wk = _adapter([_src("wiki-body", url="cellar://p", capability="wiki", query="q1")])
    out = _run(
        run_broker(
            {"topic": "climate policy", "operator_intent": {"angle": "x"}},
            web_search_adapter=ws,
            wiki_adapter=wk,
            web_extract_adapter=_raising_adapter("web_extract must not run for a subject"),
            call_model=_model_returning(["q1"]),
        )
    )
    assert set(out.keys()) == {"queries_issued", "phase_duration_ms", "materials"}
    assert out["queries_issued"] == ["q1"]
    assert isinstance(out["phase_duration_ms"], int) and out["phase_duration_ms"] >= 0
    assert len(out["materials"]) == 2
    for m in out["materials"]:
        assert set(m.keys()) == {
            "source_id", "url", "query", "capability", "fetched_at",
            "http_status", "content_type", "bytes_original", "truncated",
            "content_sha256", "content",
        }
    caps = {m["capability"] for m in out["materials"]}
    assert caps == {"web_search", "wiki"}


# ── URL topic = single source, zero queries ──────────────────────────────────
def test_url_topic_single_source_zero_queries():
    ext = _adapter([_src("page-body", url="https://x.com/a", capability="web_extract", query="https://x.com/a")])
    out = _run(
        run_broker(
            {"topic": "https://x.com/a", "operator_intent": {}},
            web_extract_adapter=ext,
            web_search_adapter=_raising_adapter("web_search must not run for a URL topic"),
            wiki_adapter=_raising_adapter("wiki must not run for a URL topic"),
            call_model=_model_raw({"queries": ["SHOULD-NOT-BE-USED"]}),
            url_is_safe=lambda u: True,
        )
    )
    assert out["queries_issued"] == []
    assert len(out["materials"]) == 1
    assert out["materials"][0]["capability"] == "web_extract"


# ── URL safety ────────────────────────────────────────────────────────────────
def test_scheme_outside_allowlist_rejected_loudly():
    with pytest.raises(BrokerURLRejected):
        _run(
            run_broker(
                {"topic": "ftp://internal/secret"},
                web_extract_adapter=_raising_adapter("no fetch on a rejected scheme"),
                url_is_safe=lambda u: True,  # scheme check must fire first
            )
        )


def test_private_or_unsafe_url_rejected_loudly():
    with pytest.raises(BrokerURLRejected):
        _run(
            run_broker(
                {"topic": "https://internal.local/x"},
                web_extract_adapter=_raising_adapter("no fetch on an unsafe URL"),
                url_is_safe=lambda u: False,  # SSRF check blocks it
            )
        )


# ── malformed / hostile formulation output ───────────────────────────────────
@pytest.mark.parametrize(
    "bad",
    [
        {"queries": "not-a-list"},
        {"queries": [{"nested": "object"}]},
        {"queries": [123]},
        {"queries": [""]},
        {"queries": []},
        {"no_queries_key": []},
        ["not", "a", "dict"],
        "a bare string",
    ],
)
def test_malformed_formulation_rejected_by_validation(bad):
    with pytest.raises(BrokerQueryFormulationError):
        _run(
            run_broker(
                {"topic": "a subject"},
                web_search_adapter=_raising_adapter("no fetch when formulation is invalid"),
                wiki_adapter=_raising_adapter("no fetch when formulation is invalid"),
                call_model=_model_raw(bad),
            )
        )


# ── source cap ────────────────────────────────────────────────────────────────
def test_source_cap_discards_excess(caplog):
    ws = _adapter([_src(f"w{i}", url=f"https://w/{i}", capability="web_search") for i in range(4)])
    wk = _adapter([_src(f"k{i}", url=f"cellar://{i}", capability="wiki") for i in range(4)])
    with caplog.at_level("WARNING"):
        out = _run(
            run_broker(
                {"topic": "a subject"},
                web_search_adapter=ws,
                wiki_adapter=wk,
                web_extract_adapter=_raising_adapter(),
                call_model=_model_returning(["q1"]),
            )
        )
    assert len(out["materials"]) == MAX_SOURCES  # 8 fetched → 5 kept
    assert any("discarding 3" in r.message for r in caplog.records)


# ── per-source timeout drop ──────────────────────────────────────────────────
def test_per_source_timeout_drops_source_not_phase():
    async def _slow(term):
        await asyncio.sleep(1.0)
        return [_src("never", capability="web_search")]

    wk = _adapter([_src("wiki-body", url="cellar://p", capability="wiki")])
    out = _run(
        run_broker(
            {"topic": "a subject"},
            web_search_adapter=_slow,
            wiki_adapter=wk,
            web_extract_adapter=_raising_adapter(),
            call_model=_model_returning(["q1"]),
            per_source_timeout=0.02,  # trips the slow web_search fetch
        )
    )
    # web_search dropped; wiki survives — the phase completes.
    assert len(out["materials"]) == 1
    assert out["materials"][0]["capability"] == "wiki"


# ── truncation ────────────────────────────────────────────────────────────────
def test_truncation_sets_flag_and_preserves_bytes_original():
    big = "a" * (MAX_CONTENT_BYTES + 50_000)
    ext = _adapter([_src(big, url="https://x/big", capability="web_extract", query="https://x/big")])
    out = _run(
        run_broker(
            {"topic": "https://x/big"},
            web_extract_adapter=ext,
            url_is_safe=lambda u: True,
        )
    )
    m = out["materials"][0]
    assert m["truncated"] is True
    assert m["bytes_original"] == MAX_CONTENT_BYTES + 50_000
    assert len(m["content"].encode("utf-8")) == MAX_CONTENT_BYTES
    assert m["content"] == "a" * MAX_CONTENT_BYTES


def test_untruncated_source_flag_false_and_full_content():
    ext = _adapter([_src("short body", url="https://x/s", capability="web_extract", query="https://x/s")])
    out = _run(run_broker({"topic": "https://x/s"}, web_extract_adapter=ext, url_is_safe=lambda u: True))
    m = out["materials"][0]
    assert m["truncated"] is False
    assert m["content"] == "short body"
    assert m["bytes_original"] == len("short body".encode("utf-8"))


# ── content_sha256 exactness + plain text ─────────────────────────────────────
def test_content_sha256_matches_emitted_bytes():
    ws = _adapter([_src("some readable text", url="https://a", capability="web_search")])
    wk = _adapter([])
    out = _run(
        run_broker(
            {"topic": "a subject"},
            web_search_adapter=ws,
            wiki_adapter=wk,
            web_extract_adapter=_raising_adapter(),
            call_model=_model_returning(["q1"]),
        )
    )
    m = out["materials"][0]
    assert m["content_sha256"] == hashlib.sha256(m["content"].encode("utf-8")).hexdigest()
    # plain readable text — NOT base64 / binary-safe encoding
    assert m["content"] == "some readable text"


def test_content_sha256_exact_after_truncation():
    big = "x" * (MAX_CONTENT_BYTES + 1)
    ext = _adapter([_src(big, url="https://x/big", capability="web_extract", query="https://x/big")])
    out = _run(run_broker({"topic": "https://x/big"}, web_extract_adapter=ext, url_is_safe=lambda u: True))
    m = out["materials"][0]
    # sha is over the truncated bytes actually placed in "content"
    assert m["content_sha256"] == hashlib.sha256(m["content"].encode("utf-8")).hexdigest()
    assert m["content_sha256"] == hashlib.sha256(("x" * MAX_CONTENT_BYTES).encode()).hexdigest()


# ── total-materials ceiling HARD HALT ─────────────────────────────────────────
def test_total_materials_ceiling_hard_halts():
    per = 500_000  # 5 × 500KB raw = 2.5MB > MAX_TOTAL_MATERIALS_BYTES (2MB)
    ws = _adapter([_src("z" * per, url=f"https://w/{i}", capability="web_search") for i in range(MAX_SOURCES)])
    with pytest.raises(BrokerMaterialsCeilingExceeded):
        _run(
            run_broker(
                {"topic": "a subject"},
                web_search_adapter=ws,
                wiki_adapter=_adapter([]),
                web_extract_adapter=_raising_adapter(),
                call_model=_model_returning(["q1"]),
            )
        )
    assert per * MAX_SOURCES > MAX_TOTAL_MATERIALS_BYTES  # guards the fixture's intent


# ── phase budget HARD HALT — raises, no partial ──────────────────────────────
def test_budget_exceeded_raises_no_partial():
    # start=0.0, first check_phase() sees 100.0 → 100 > 45 → halt before any fetch.
    clock = _FakeClock([0.0, 100.0])
    with pytest.raises(BrokerBudgetExceeded):
        _run(
            run_broker(
                {"topic": "a subject"},
                web_search_adapter=_raising_adapter("phase must halt before fetching"),
                wiki_adapter=_raising_adapter("phase must halt before fetching"),
                web_extract_adapter=_raising_adapter(),
                call_model=_model_returning(["q1"]),
                monotonic=clock,
            )
        )


# ── request validation ────────────────────────────────────────────────────────
@pytest.mark.parametrize("body", [{}, {"topic": ""}, {"topic": "   "}, {"topic": 123}, "not-a-dict"])
def test_missing_or_bad_topic_raises(body):
    with pytest.raises(BrokerRequestError):
        _run(run_broker(body, url_is_safe=lambda u: True))


# ── fetched_at present and phase_duration instrumented ───────────────────────
def test_phase_duration_and_fetched_at_present():
    ws = _adapter([_src("b", capability="web_search")])
    out = _run(
        run_broker(
            {"topic": "a subject"},
            web_search_adapter=ws,
            wiki_adapter=_adapter([]),
            web_extract_adapter=_raising_adapter(),
            call_model=_model_returning(["q1"]),
            now_iso=lambda: "2026-07-25T00:00:00+00:00",
        )
    )
    assert out["materials"][0]["fetched_at"] == "2026-07-25T00:00:00+00:00"
    assert isinstance(out["phase_duration_ms"], int)


# ── adapter contract: raw path, no auxiliary client (the HARD CONSTRAINT) ─────
def test_web_extract_adapter_takes_raw_no_aux_client():
    captured = {}

    async def fake_extract(urls, **kwargs):
        captured["urls"] = urls
        captured["kwargs"] = kwargs
        return json.dumps({"results": [{"url": urls[0], "title": "T", "content": "body"}]})

    srcs = _run(_adapter_web_extract("https://x", extract_fn=fake_extract))
    assert captured["kwargs"].get("use_llm_processing") is False  # raw fallback forced
    # no auxiliary client / summarizer model is passed
    assert "auxiliary_client" not in captured["kwargs"]
    assert "model" not in captured["kwargs"]
    assert len(srcs) == 1 and srcs[0].capability == "web_extract" and srcs[0].raw_content == "body"


def test_web_extract_adapter_skips_error_results():
    async def fake_extract(urls, **kwargs):
        return json.dumps({"results": [{"url": urls[0], "error": "boom"}]})

    srcs = _run(_adapter_web_extract("https://x", extract_fn=fake_extract))
    assert srcs == []


def test_web_search_adapter_normalizes():
    def fake_search(query, limit):
        return json.dumps(
            {"success": True, "data": {"web": [{"title": "A", "url": "u1", "description": "d1", "position": 1}]}}
        )

    srcs = _run(_adapter_web_search("q", search_fn=fake_search))
    assert len(srcs) == 1
    assert srcs[0].capability == "web_search" and srcs[0].url == "u1"
    assert "A" in srcs[0].raw_content and "d1" in srcs[0].raw_content


def test_web_search_adapter_unconfigured_provider_yields_nothing():
    def fake_search(query, limit):
        return json.dumps({"success": False, "error": "No web search provider configured."})

    assert _run(_adapter_web_search("q", search_fn=fake_search)) == []


def test_wiki_adapter_normalizes():
    class _FakeWikiResult:
        source_path = "notes/x.md"
        title = "Title"
        body = "the page body"

    def fake_query(text, k):
        return [_FakeWikiResult()]

    srcs = _run(_adapter_wiki("q", query_fn=fake_query))
    assert len(srcs) == 1
    assert srcs[0].capability == "wiki"
    assert srcs[0].raw_content == "the page body"
    assert srcs[0].url == "cellar://notes/x.md"
