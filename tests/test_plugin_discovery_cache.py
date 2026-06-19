"""mcp-plugin-discovery-cache-v1 — the module-level plugin manifest scan is
amortized to once per process; only registration replays into each fresh
per-turn ToolRegistry, under a module-level threading.Lock.

These tests drive the cache orchestration through the real ``_load_winners``
decision logic and the real ``PluginContext`` register facade, with a counted
fake scan standing in for the filesystem walk. DoD items 1-7 plus the A4
guard (a config flip between two warm-cache replays must be honored without a
rescan — the decision is never cached).
"""

from __future__ import annotations

import threading

import pytest

import hermes_cli.plugins as plugins
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from tools.registry import ToolRegistry

HOOK = "post_tool_call"
PLUGIN_TOOLS = ("alpha", "beta")


def _schema(name: str) -> dict:
    return {
        "name": name,
        "description": f"test tool {name}",
        "parameters": {"type": "object", "properties": {}},
    }


def _make_manager(
    scan_calls: list,
    *,
    kind: str = "backend",
    source: str = "bundled",
    key: str = "testplug",
):
    """A PluginManager whose SCAN is faked (each call appended to *scan_calls*)
    and whose ``_load_plugin`` registers ``PLUGIN_TOOLS`` + one hook into the
    current registry via the real PluginContext facade. ``kind``/``source``
    select the load/skip branch in ``_load_winners`` (bundled+backend
    auto-loads; user+standalone is gated by ``plugins.enabled``)."""
    pm = PluginManager()
    manifest = PluginManifest(name="testplug", key=key, source=source, kind=kind)

    def fake_scan():
        scan_calls.append(1)
        return {key: manifest}

    pm._scan_and_resolve_winners = fake_scan  # type: ignore[method-assign]

    def fake_load(mf):
        ctx = PluginContext(mf, pm, registry=pm._registry)
        for tool in PLUGIN_TOOLS:
            ctx.register_tool(
                name=tool,
                toolset="testset",
                schema=_schema(tool),
                handler=lambda **k: "ok",
            )
        ctx.register_hook(HOOK, lambda *a, **k: None)

    pm._load_plugin = fake_load  # type: ignore[method-assign]
    return pm, manifest


@pytest.fixture(autouse=True)
def _deterministic_config(monkeypatch):
    """Deterministic plugins.enabled/disabled. The scan cache is per
    PluginManager instance, so each test's fresh manager starts cold."""
    monkeypatch.setattr(plugins, "_get_disabled_plugins", lambda: set())
    monkeypatch.setattr(plugins, "_get_enabled_plugins", lambda: None)
    yield


def _tools(registry: ToolRegistry) -> set:
    return set(registry.get_all_tool_names())


# ── DoD 1 — scan once, decision per replay ───────────────────────────


def test_1_scan_runs_once_across_fresh_registries(monkeypatch):
    scan_calls: list = []
    pm, _ = _make_manager(scan_calls)
    enabled_reads: list = []
    monkeypatch.setattr(
        plugins, "_get_enabled_plugins", lambda: enabled_reads.append(1) or None
    )
    pm.discover_and_load(registry=ToolRegistry())
    pm.discover_and_load(registry=ToolRegistry())
    assert len(scan_calls) == 1          # filesystem scan amortized
    assert len(enabled_reads) == 2       # decision (config read) re-run per replay


# ── DoD 2 — no starvation (the dbaaa7770 guard) ──────────────────────


def test_2_warm_replay_full_toolset_no_starvation():
    scan_calls: list = []
    pm, _ = _make_manager(scan_calls)
    reg_a, reg_b = ToolRegistry(), ToolRegistry()
    pm.discover_and_load(registry=reg_a)
    pm.discover_and_load(registry=reg_b)
    assert _tools(reg_a) == set(PLUGIN_TOOLS)
    assert _tools(reg_b) == _tools(reg_a)  # fresh registry fully populated
    assert len(scan_calls) == 1


# ── DoD 3 — hooks not doubled (clear-then-replay parity) ─────────────


def test_3_hooks_not_doubled_across_replays():
    scan_calls: list = []
    pm, _ = _make_manager(scan_calls)
    pm.discover_and_load(registry=ToolRegistry())
    pm.discover_and_load(registry=ToolRegistry())
    assert len(pm._hooks.get(HOOK, [])) == 1


# ── DoD 4 — LOAD-BEARING: concurrent replays, no cross-contamination ─


def test_4_concurrent_replays_no_cross_contamination():
    scan_calls: list = []
    pm, _ = _make_manager(scan_calls)
    pm.discover_and_load(registry=ToolRegistry())  # warm the scan cache first

    results: dict = {}
    barrier = threading.Barrier(2)

    def worker(label: str):
        reg = ToolRegistry()
        barrier.wait()
        for _ in range(25):
            pm.discover_and_load(registry=reg)
        results[label] = _tools(reg)

    t1 = threading.Thread(target=worker, args=("A",))
    t2 = threading.Thread(target=worker, args=("B",))
    t1.start(); t2.start(); t1.join(); t2.join()

    # Each registry got its OWN complete toolset — the coarse lock prevents the
    # self._registry swap-mid-replay race from landing tools in the wrong one.
    assert results["A"] == set(PLUGIN_TOOLS)
    assert results["B"] == set(PLUGIN_TOOLS)
    assert len(pm._hooks.get(HOOK, [])) == 1  # not doubled under concurrency
    assert len(scan_calls) == 1               # never re-scanned


# ── DoD 5 — purity: identical schemas, no per-replay mutation ────────


def test_5_replay_identical_schemas_pure():
    scan_calls: list = []
    pm, _ = _make_manager(scan_calls)
    reg_a, reg_b = ToolRegistry(), ToolRegistry()
    pm.discover_and_load(registry=reg_a)
    pm.discover_and_load(registry=reg_b)
    assert reg_a.get_definitions({"alpha"}) == reg_b.get_definitions({"alpha"})


# ── DoD 6 — explicit reload rescans ──────────────────────────────────


def test_6_explicit_force_reload_rescans():
    scan_calls: list = []
    pm, _ = _make_manager(scan_calls)
    reg_a = ToolRegistry()
    pm.discover_and_load(registry=reg_a)            # cold scan
    pm.discover_and_load(registry=ToolRegistry())   # warm replay, no rescan
    assert len(scan_calls) == 1
    pm.discover_and_load(force=True, registry=reg_a)  # operator reload
    assert len(scan_calls) == 2                      # filesystem re-walked


# ── DoD 7 — same-registry repeat is an unchanged early-return ────────


def test_7_same_registry_repeat_early_return():
    scan_calls: list = []
    pm, _ = _make_manager(scan_calls)
    reg_a = ToolRegistry()
    pm.discover_and_load(registry=reg_a)
    before = _tools(reg_a)
    pm.discover_and_load(registry=reg_a)  # same registry → early return
    assert len(scan_calls) == 1
    assert _tools(reg_a) == before


# ── A4 guard — config flip honored on warm cache, no rescan ──────────


def test_a4_config_flip_honored_without_rescan(monkeypatch):
    scan_calls: list = []
    pm, _ = _make_manager(scan_calls, kind="standalone", source="user", key="gated")
    enabled = {"set": {"gated"}}
    monkeypatch.setattr(plugins, "_get_enabled_plugins", lambda: set(enabled["set"]))

    reg_a = ToolRegistry()
    pm.discover_and_load(registry=reg_a)
    assert _tools(reg_a) == set(PLUGIN_TOOLS)  # enabled → loaded

    # Flip config to disabled; the next warm replay must honor it WITHOUT a
    # rescan — proving the load/skip decision is re-evaluated, never cached.
    enabled["set"] = set()
    reg_b = ToolRegistry()
    pm.discover_and_load(registry=reg_b)
    assert len(scan_calls) == 1            # scan stayed warm
    assert _tools(reg_b) == set()          # decision re-evaluated → skipped
