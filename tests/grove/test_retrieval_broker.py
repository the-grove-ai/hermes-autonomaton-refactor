"""Unit tests for the host-side retrieval broker (researcher-retrieval-broker-v1
Phase 1 + P1b pipeline amendment). All offline — every external effect is
injected. No network, no model.

Covers: subject topic yields EXTRACTED article text (not snippets); candidate
URLs deduped before extraction; MAX_SOURCES caps final materials with excess
candidates discarded pre-extraction; a search-result URL failing is_safe_url is
rejected (drop+log), not silently skipped; URL topic still = one source / zero
queries and never enters the pipeline; capability vs discovery kept distinct;
budget hard-halts (no partial) under the amended pipeline; plus the retained P1
invariants (truncation flag + bytes_original, sha256 exactness, out-of-allowlist
scheme rejected, malformed formulation rejected, raw-path/no-aux adapter).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading

import pytest

from grove.fleet.retrieval_broker import (
    _FORMULATION_MAX_WORKERS,
    _formulate_queries,
    MAX_CONTENT_BYTES,
    MAX_SOURCES,
    MAX_TOTAL_MATERIALS_BYTES,
    WIKI_RESERVED_SLOTS,
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


def _raw(url="https://s/1", *, content="body", capability="web_search", query="q", discovery="web_search", title="T"):
    return RawSource(
        url=url,
        title=title,
        raw_content=content,
        http_status=200,
        content_type="text/plain",
        capability=capability,
        query=query,
        discovery=discovery,
    )


def _raising_adapter(msg="adapter should not be called"):
    async def _a(term):
        raise AssertionError(msg)

    return _a


def _search_returning(urls_per_query):
    """urls_per_query: list of URLs each query returns (same for every query)."""

    async def _a(q):
        return [_raw(url=u, capability="web_search", query=q, discovery="web_search") for u in urls_per_query]

    return _a


def _extract_returning(text="ARTICLE TEXT", *, by_url=None):
    calls = []

    async def _a(url):
        calls.append(url)
        content = by_url.get(url, text) if by_url is not None else text
        return [
            RawSource(
                url=url, title="T", raw_content=content, http_status=200,
                content_type="text/markdown", capability="web_extract",
                query=url, discovery="web_extract",
            )
        ]

    _a.calls = calls
    return _a


def _wiki_returning(*contents):
    async def _a(q):
        return [_raw(url=f"cellar://{i}", content=c, capability="wiki", query=q, discovery="wiki") for i, c in enumerate(contents)]

    return _a


def _empty_adapter():
    async def _a(term):
        return []

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
    def __init__(self, values):
        self._values = list(values)
        self._i = 0

    def __call__(self):
        v = self._values[min(self._i, len(self._values) - 1)]
        self._i += 1
        return v


# ── P1b: subject topic yields EXTRACTED article text, not snippets ───────────
def test_subject_yields_extracted_article_text_not_snippets():
    search = _search_returning(["https://a/article"])
    extract = _extract_returning("THE FULL ARTICLE BODY WITH ARGUMENTS")
    out = _run(
        run_broker(
            {"topic": "climate policy", "operator_intent": {"angle": "x"}},
            web_search_adapter=search,
            web_extract_adapter=extract,
            wiki_adapter=_empty_adapter(),
            call_model=_model_returning(["climate policy 2030"]),
            url_is_safe=lambda u: True,
        )
    )
    assert out["queries_issued"] == ["climate policy 2030"]
    assert len(out["materials"]) == 1
    m = out["materials"][0]
    assert m["content"] == "THE FULL ARTICLE BODY WITH ARGUMENTS"   # article text, not snippet
    assert m["capability"] == "web_extract"                          # CONTENT method
    assert m["discovery"] == "web_search"                            # URL discovery method
    assert m["query"] == "climate policy 2030"                       # the query that produced it
    assert extract.calls == ["https://a/article"]


def test_subject_shape_includes_discovery_field():
    out = _run(
        run_broker(
            {"topic": "a subject"},
            web_search_adapter=_search_returning(["https://a"]),
            web_extract_adapter=_extract_returning("body"),
            wiki_adapter=_empty_adapter(),
            call_model=_model_returning(["q1"]),
            url_is_safe=lambda u: True,
        )
    )
    assert set(out.keys()) == {"queries_issued", "phase_duration_ms", "materials"}
    assert set(out["materials"][0].keys()) == {
        "source_id", "url", "query", "capability", "discovery", "fetched_at",
        "http_status", "content_type", "bytes_original", "truncated",
        "content_sha256", "content",
    }


# ── P1b: dedupe candidates before extraction ─────────────────────────────────
def test_candidate_urls_deduped_before_extraction():
    # both queries surface the same duplicate URL plus one unique each.
    async def search(q):
        base = {"q1": "https://uniq/1", "q2": "https://uniq/2"}[q]
        return [_raw(url="https://dup/x", capability="web_search", query=q, discovery="web_search"),
                _raw(url=base, capability="web_search", query=q, discovery="web_search")]

    extract = _extract_returning("body")
    out = _run(
        run_broker(
            {"topic": "a subject"},
            web_search_adapter=search,
            web_extract_adapter=extract,
            wiki_adapter=_empty_adapter(),
            call_model=_model_returning(["q1", "q2"]),
            url_is_safe=lambda u: True,
        )
    )
    # dedup: https://dup/x extracted once, not twice.
    assert extract.calls.count("https://dup/x") == 1
    assert sorted(extract.calls) == ["https://dup/x", "https://uniq/1", "https://uniq/2"]
    assert len(out["materials"]) == 3


# ── P1b: MAX_SOURCES caps final; excess candidates discarded pre-extraction ───
def test_max_sources_caps_final_and_discards_candidates_before_extraction(caplog):
    urls = [f"https://a/{i}" for i in range(8)]  # 8 unique candidates
    extract = _extract_returning("body")
    with caplog.at_level("WARNING"):
        out = _run(
            run_broker(
                {"topic": "a subject"},
                web_search_adapter=_search_returning(urls),
                web_extract_adapter=extract,
                wiki_adapter=_empty_adapter(),
                call_model=_model_returning(["q1"]),
                url_is_safe=lambda u: True,
            )
        )
    assert len(out["materials"]) == MAX_SOURCES
    # excess discarded BEFORE extracting — exactly 5 extracts, never 8.
    assert len(extract.calls) == MAX_SOURCES
    assert any("discarded pre-extraction" in r.message for r in caplog.records)


# ── P1b: search-result URL failing is_safe_url is rejected, not silently skipped
def test_search_candidate_failing_safety_rejected_not_silently_skipped(caplog):
    extract = _extract_returning("body")

    def only_good_is_safe(u):
        return u != "https://evil/private"

    with caplog.at_level("WARNING"):
        out = _run(
            run_broker(
                {"topic": "a subject"},
                web_search_adapter=_search_returning(["https://good/1", "https://evil/private"]),
                web_extract_adapter=extract,
                wiki_adapter=_empty_adapter(),
                call_model=_model_returning(["q1"]),
                url_is_safe=only_good_is_safe,
            )
        )
    # unsafe candidate never extracted...
    assert "https://evil/private" not in extract.calls
    assert extract.calls == ["https://good/1"]
    # ...and NOT silently skipped — a loud rejection is logged.
    assert any("candidate rejected" in r.message for r in caplog.records)
    assert len(out["materials"]) == 1


# ── P1b: wiki alongside ───────────────────────────────────────────────────────
def test_wiki_runs_alongside_search_extract_in_subject():
    out = _run(
        run_broker(
            {"topic": "a subject"},
            web_search_adapter=_search_returning(["https://a"]),
            web_extract_adapter=_extract_returning("article"),
            wiki_adapter=_wiki_returning("wiki page body"),
            call_model=_model_returning(["q1"]),
            url_is_safe=lambda u: True,
        )
    )
    caps = {(m["capability"], m["discovery"]) for m in out["materials"]}
    assert ("web_extract", "web_search") in caps
    assert ("wiki", "wiki") in caps


# ── C1: wiki reserved floor ──────────────────────────────────────────────────
def test_wiki_reserved_floor_when_wiki_has_hits():
    # 3 wiki hits (floor 2) + 5 safe candidates → reserve 2 for wiki, so only
    # 3 web slots → exactly 3 extracts, final = 3 web + 2 wiki.
    assert WIKI_RESERVED_SLOTS == 2
    extract = _extract_returning("article")
    out = _run(
        run_broker(
            {"topic": "a subject"},
            web_search_adapter=_search_returning([f"https://a/{i}" for i in range(5)]),
            web_extract_adapter=extract,
            wiki_adapter=_wiki_returning("w0", "w1", "w2"),
            call_model=_model_returning(["q1"]),
            url_is_safe=lambda u: True,
        )
    )
    assert len(extract.calls) == MAX_SOURCES - WIKI_RESERVED_SLOTS  # 3 extracts, not 5
    caps = [m["capability"] for m in out["materials"]]
    assert caps.count("web_extract") == 3
    assert caps.count("wiki") == 2
    assert len(out["materials"]) == MAX_SOURCES


def test_wiki_empty_web_takes_all_slots():
    # Empty cellar reserves nothing — web takes all MAX_SOURCES.
    extract = _extract_returning("article")
    out = _run(
        run_broker(
            {"topic": "a subject"},
            web_search_adapter=_search_returning([f"https://a/{i}" for i in range(8)]),
            web_extract_adapter=extract,
            wiki_adapter=_empty_adapter(),
            call_model=_model_returning(["q1"]),
            url_is_safe=lambda u: True,
        )
    )
    assert len(extract.calls) == MAX_SOURCES  # 5 extracts, no reserved slots wasted
    assert len(out["materials"]) == MAX_SOURCES
    assert all(m["capability"] == "web_extract" for m in out["materials"])


def test_wiki_one_hit_reserves_only_one():
    extract = _extract_returning("article")
    out = _run(
        run_broker(
            {"topic": "a subject"},
            web_search_adapter=_search_returning([f"https://a/{i}" for i in range(8)]),
            web_extract_adapter=extract,
            wiki_adapter=_wiki_returning("only-wiki"),
            call_model=_model_returning(["q1"]),
            url_is_safe=lambda u: True,
        )
    )
    assert len(extract.calls) == MAX_SOURCES - 1  # 4 web slots + 1 wiki
    caps = [m["capability"] for m in out["materials"]]
    assert caps.count("web_extract") == 4 and caps.count("wiki") == 1


# ── C2: subject-with-colon is not a URL; url-shape requires scheme + netloc ───
def test_subject_with_colon_not_treated_as_url():
    seen = {}

    def model(**kwargs):
        seen["called"] = True
        return {"queries": ["rust async runtime internals"]}

    out = _run(
        run_broker(
            {"topic": "Rust: async runtime internals"},
            web_search_adapter=_search_returning(["https://a"]),
            web_extract_adapter=_extract_returning("body"),
            wiki_adapter=_empty_adapter(),
            call_model=model,
            url_is_safe=lambda u: True,
        )
    )
    assert seen.get("called") is True  # subject path → formulation ran
    assert out["queries_issued"] == ["rust async runtime internals"]


def test_url_shaped_topic_goes_url_path():
    extract = _extract_returning("body")
    out = _run(
        run_broker(
            {"topic": "https://example.com/a"},
            web_extract_adapter=extract,
            web_search_adapter=_raising_adapter("URL topic must not search"),
            wiki_adapter=_raising_adapter("URL topic must not wiki"),
            call_model=_model_raw({"queries": ["SHOULD-NOT-BE-USED"]}),
            url_is_safe=lambda u: True,
        )
    )
    assert out["queries_issued"] == [] and len(out["materials"]) == 1


def test_url_shaped_out_of_allowlist_scheme_rejected():
    with pytest.raises(BrokerURLRejected):
        _run(
            run_broker(
                {"topic": "file://host/etc/passwd"},
                web_extract_adapter=_raising_adapter("no fetch on a rejected url-shaped scheme"),
                url_is_safe=lambda u: True,  # scheme check fires first
            )
        )


# ── C3: formulation call is bounded; timeout is a HARD HALT ──────────────────
def test_formulation_timeout_hard_halts_no_partial():
    import time as _time

    def slow_model(**kwargs):
        _time.sleep(0.5)  # exceeds the injected per_source_timeout below
        return {"queries": ["q"]}

    with pytest.raises(BrokerQueryFormulationError):
        _run(
            run_broker(
                {"topic": "a subject"},
                web_search_adapter=_raising_adapter("no fetch when formulation times out"),
                wiki_adapter=_raising_adapter("no fetch when formulation times out"),
                web_extract_adapter=_raising_adapter(),
                call_model=slow_model,
                per_source_timeout=0.05,
            )
        )


# ── P1b: provenance — capability (content) vs discovery (url) kept distinct ───
def test_provenance_capability_vs_discovery_distinct():
    # subject search→extract
    subj = _run(
        run_broker(
            {"topic": "a subject"},
            web_search_adapter=_search_returning(["https://a"]),
            web_extract_adapter=_extract_returning("body"),
            wiki_adapter=_empty_adapter(),
            call_model=_model_returning(["q1"]),
            url_is_safe=lambda u: True,
        )
    )
    m = subj["materials"][0]
    assert (m["capability"], m["discovery"]) == ("web_extract", "web_search")

    # url topic → discovery is operator_url, content still via web_extract
    url = _run(
        run_broker(
            {"topic": "https://x.com/a"},
            web_extract_adapter=_extract_returning("body"),
            web_search_adapter=_raising_adapter("no search on a URL topic"),
            wiki_adapter=_raising_adapter("no wiki on a URL topic"),
            url_is_safe=lambda u: True,
        )
    )
    mu = url["materials"][0]
    assert (mu["capability"], mu["discovery"]) == ("web_extract", "operator_url")


# ── URL topic UNCHANGED: single source, zero queries, never enters pipeline ───
def test_url_topic_single_source_zero_queries():
    extract = _extract_returning("page body")
    out = _run(
        run_broker(
            {"topic": "https://x.com/a", "operator_intent": {}},
            web_extract_adapter=extract,
            web_search_adapter=_raising_adapter("web_search must not run for a URL topic"),
            wiki_adapter=_raising_adapter("wiki must not run for a URL topic"),
            call_model=_model_raw({"queries": ["SHOULD-NOT-BE-USED"]}),
            url_is_safe=lambda u: True,
        )
    )
    assert out["queries_issued"] == []
    assert len(out["materials"]) == 1
    assert out["materials"][0]["capability"] == "web_extract"
    assert extract.calls == ["https://x.com/a"]


# ── URL safety (operator URL) ─────────────────────────────────────────────────
def test_operator_scheme_outside_allowlist_rejected_loudly():
    with pytest.raises(BrokerURLRejected):
        _run(
            run_broker(
                {"topic": "ftp://internal/secret"},
                web_extract_adapter=_raising_adapter("no fetch on a rejected scheme"),
                url_is_safe=lambda u: True,  # scheme check must fire first
            )
        )


def test_operator_private_url_rejected_loudly():
    with pytest.raises(BrokerURLRejected):
        _run(
            run_broker(
                {"topic": "https://internal.local/x"},
                web_extract_adapter=_raising_adapter("no fetch on an unsafe URL"),
                url_is_safe=lambda u: False,
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
                web_extract_adapter=_raising_adapter(),
                call_model=_model_raw(bad),
            )
        )


# ── per-source timeout drop (a slow extract drops that source, not the phase) ─
def test_per_source_extract_timeout_drops_source_not_phase():
    async def slow_extract(url):
        await asyncio.sleep(1.0)
        return [_raw(url=url, capability="web_extract")]

    out = _run(
        run_broker(
            {"topic": "a subject"},
            web_search_adapter=_search_returning(["https://slow"]),
            web_extract_adapter=slow_extract,
            wiki_adapter=_wiki_returning("wiki body"),
            call_model=_model_returning(["q1"]),
            url_is_safe=lambda u: True,
            per_source_timeout=0.02,  # trips the slow extract
        )
    )
    # extract dropped; wiki survives — the phase completes.
    assert len(out["materials"]) == 1
    assert out["materials"][0]["capability"] == "wiki"


# ── truncation (URL-topic single source) ─────────────────────────────────────
def test_truncation_sets_flag_and_preserves_bytes_original():
    big = "a" * (MAX_CONTENT_BYTES + 50_000)
    out = _run(
        run_broker(
            {"topic": "https://x/big"},
            web_extract_adapter=_extract_returning(big),
            url_is_safe=lambda u: True,
        )
    )
    m = out["materials"][0]
    assert m["truncated"] is True
    assert m["bytes_original"] == MAX_CONTENT_BYTES + 50_000
    assert len(m["content"].encode("utf-8")) == MAX_CONTENT_BYTES
    assert m["content"] == "a" * MAX_CONTENT_BYTES


def test_untruncated_source_flag_false_and_full_content():
    out = _run(
        run_broker(
            {"topic": "https://x/s"},
            web_extract_adapter=_extract_returning("short body"),
            url_is_safe=lambda u: True,
        )
    )
    m = out["materials"][0]
    assert m["truncated"] is False
    assert m["content"] == "short body"
    assert m["bytes_original"] == len("short body".encode("utf-8"))


# ── content_sha256 exactness + plain text ─────────────────────────────────────
def test_content_sha256_matches_emitted_bytes():
    out = _run(
        run_broker(
            {"topic": "https://x/a"},
            web_extract_adapter=_extract_returning("some readable text"),
            url_is_safe=lambda u: True,
        )
    )
    m = out["materials"][0]
    assert m["content_sha256"] == hashlib.sha256(m["content"].encode("utf-8")).hexdigest()
    assert m["content"] == "some readable text"  # plain text, not base64


def test_content_sha256_exact_after_truncation():
    big = "x" * (MAX_CONTENT_BYTES + 1)
    out = _run(
        run_broker(
            {"topic": "https://x/big"},
            web_extract_adapter=_extract_returning(big),
            url_is_safe=lambda u: True,
        )
    )
    m = out["materials"][0]
    assert m["content_sha256"] == hashlib.sha256(m["content"].encode("utf-8")).hexdigest()
    assert m["content_sha256"] == hashlib.sha256(("x" * MAX_CONTENT_BYTES).encode()).hexdigest()


# ── total-materials ceiling HARD HALT (subject search→extract) ───────────────
def test_total_materials_ceiling_hard_halts():
    urls = [f"https://a/{i}" for i in range(MAX_SOURCES)]
    per = 500_000  # 5 × 500KB raw = 2.5MB > MAX_TOTAL_MATERIALS_BYTES (2MB)
    with pytest.raises(BrokerMaterialsCeilingExceeded):
        _run(
            run_broker(
                {"topic": "a subject"},
                web_search_adapter=_search_returning(urls),
                web_extract_adapter=_extract_returning("z" * per),
                wiki_adapter=_empty_adapter(),
                call_model=_model_returning(["q1"]),
                url_is_safe=lambda u: True,
            )
        )
    assert per * MAX_SOURCES > MAX_TOTAL_MATERIALS_BYTES


# ── phase budget HARD HALT under the amended pipeline — raises, no partial ────
def test_budget_exceeded_pre_fetch_raises_no_partial():
    # start=0.0; first check_phase (post-formulation, pre-stage-1) sees 100 → halt.
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


def test_budget_exceeded_after_fetch_raises_no_partial():
    # Clock tuned to the subject-path check_phase order: start, then checks after
    # formulate / stage1 / candidate-cap / stage2 (calls 2-5, all < 45), then the
    # first build-loop check (call 6) trips at 100 — AFTER fetching, proving no
    # partial materials are returned.
    clock = _FakeClock([0.0, 1.0, 2.0, 3.0, 4.0, 100.0])
    extract = _extract_returning("body")
    with pytest.raises(BrokerBudgetExceeded):
        _run(
            run_broker(
                {"topic": "a subject"},
                web_search_adapter=_search_returning(["https://a"]),
                web_extract_adapter=extract,
                wiki_adapter=_empty_adapter(),
                call_model=_model_returning(["q1"]),
                url_is_safe=lambda u: True,
                monotonic=clock,
            )
        )
    assert extract.calls == ["https://a"]  # fetch ran; the halt still returned nothing


# ── P3a: formulation executor containment (deterministic, xdist-safe) ────────
def test_formulation_runs_on_dedicated_pool_not_default():
    # Formulation executes on the DEDICATED pool (broker-formulation*), never the
    # loop's default pool that the search/wiki adapters share.
    seen = []

    def model(**kwargs):
        seen.append(threading.current_thread().name)
        return {"queries": ["q"]}

    asyncio.run(_formulate_queries("t", None, call_model=model, tier="T1", max_queries=3))
    assert seen and all(n.startswith("broker-formulation") for n in seen)


def test_formulation_saturation_fails_loud_no_default_slot():
    # Occupy every dedicated slot directly — equivalent to N formulations
    # in-flight (hung), but deterministic: no threads, no timing. The (N+1)th
    # fails LOUD at the non-blocking admission gate, BEFORE any executor
    # submission — so it consumes no slot in ANY pool (default or dedicated).
    from grove.fleet.retrieval_broker import _formulation_slots

    held = sum(1 for _ in range(_FORMULATION_MAX_WORKERS) if _formulation_slots.acquire(blocking=False))
    try:
        assert held == _FORMULATION_MAX_WORKERS
        with pytest.raises(BrokerQueryFormulationError):
            asyncio.run(
                _formulate_queries(
                    "t", None, call_model=lambda **k: {"queries": ["q"]}, tier="T1", max_queries=3
                )
            )
    finally:
        for _ in range(held):
            _formulation_slots.release()


# ── request validation ────────────────────────────────────────────────────────
@pytest.mark.parametrize("body", [{}, {"topic": ""}, {"topic": "   "}, {"topic": 123}, "not-a-dict"])
def test_missing_or_bad_topic_raises(body):
    with pytest.raises(BrokerRequestError):
        _run(run_broker(body, url_is_safe=lambda u: True))


# ── fetched_at present and phase_duration instrumented ───────────────────────
def test_phase_duration_and_fetched_at_present():
    out = _run(
        run_broker(
            {"topic": "https://x/a"},
            web_extract_adapter=_extract_returning("b"),
            url_is_safe=lambda u: True,
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
    assert "auxiliary_client" not in captured["kwargs"]
    assert "model" not in captured["kwargs"]
    assert len(srcs) == 1 and srcs[0].capability == "web_extract" and srcs[0].raw_content == "body"


def test_web_extract_adapter_skips_error_results():
    async def fake_extract(urls, **kwargs):
        return json.dumps({"results": [{"url": urls[0], "error": "boom"}]})

    assert _run(_adapter_web_extract("https://x", extract_fn=fake_extract)) == []


def test_web_search_adapter_normalizes():
    def fake_search(query, limit):
        return json.dumps(
            {"success": True, "data": {"web": [{"title": "A", "url": "u1", "description": "d1", "position": 1}]}}
        )

    srcs = _run(_adapter_web_search("q", search_fn=fake_search))
    assert len(srcs) == 1
    assert srcs[0].capability == "web_search" and srcs[0].url == "u1"


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
    assert srcs[0].capability == "wiki" and srcs[0].discovery == "wiki"
    assert srcs[0].raw_content == "the page body"
    assert srcs[0].url == "cellar://notes/x.md"
