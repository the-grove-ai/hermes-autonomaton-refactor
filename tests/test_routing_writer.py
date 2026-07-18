"""Integration tests for portal-model-swap-v1 — routing writer + model catalog.

Exercises ``RoutingConfigWriter`` (swap/revert/validation/concurrency, comment
preservation) and the ``model_catalog`` loader (sovereign override, schema
validation). The writer runs against a temp copy of the repo routing template
with an injected no-op reload and a nonexistent machine overlay, so no ~/.grove
state and no live router are touched.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
import ruamel.yaml

from grove.config.model_catalog import _validate_catalog, load_catalog
from grove.config.routing_writer import ConfigValidationError, RoutingConfigWriter

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATE = _REPO_ROOT / "config" / "routing.config.yaml"


def _model(path: Path, tier: str):
    """(model, previous_model) for a tier, read back through ruamel."""
    data = ruamel.yaml.YAML().load(path.read_text(encoding="utf-8"))
    entry = data["routing"]["tier_preferences"][tier]
    return entry.get("model"), entry.get("previous_model")


@pytest.fixture
def writer(tmp_path):
    cfg = tmp_path / "routing.config.yaml"
    shutil.copy(_TEMPLATE, cfg)
    mac = tmp_path / "routing.autonomaton.yaml"  # absent -> operator-only merge
    return RoutingConfigWriter(cfg, machine_path=mac, reload_fn=lambda: None), cfg


# ----- writer: swap / revert ------------------------------------------------


async def test_swap_sets_model_and_previous(writer):
    w, cfg = writer
    before, _ = _model(cfg, "T2")
    await w.swap_tier_model("T2", "deepseek/deepseek-chat")
    model, prev = _model(cfg, "T2")
    assert model == "deepseek/deepseek-chat"
    assert prev == before


async def test_revert_toggles_model_and_previous(writer):
    w, cfg = writer
    orig, _ = _model(cfg, "T2")
    await w.swap_tier_model("T2", "deepseek/deepseek-chat")
    await w.revert_tier_model("T2")
    model, prev = _model(cfg, "T2")
    assert model == orig
    # one-level undo: previous now points at the model we reverted away from
    assert prev == "deepseek/deepseek-chat"


async def test_swap_preserves_comments(writer):
    w, cfg = writer
    assert "# DEFAULT PROVIDER" in cfg.read_text()
    await w.swap_tier_model("T2", "deepseek/deepseek-chat")
    after = cfg.read_text()
    assert "# DEFAULT PROVIDER" in after  # AC-8
    assert "THE FOUR TIERS" in after


# ----- writer: concurrency --------------------------------------------------


async def test_concurrent_swaps_no_corruption(writer):
    w, cfg = writer
    await asyncio.gather(
        w.swap_tier_model("T1", "deepseek/deepseek-chat"),
        w.swap_tier_model("T2", "deepseek/deepseek-v3.2"),
        w.swap_tier_model("T3", "z-ai/glm-4.6"),
    )
    data = ruamel.yaml.YAML().load(cfg.read_text(encoding="utf-8"))
    tiers = data["routing"]["tier_preferences"]
    assert tiers["T1"]["model"] == "deepseek/deepseek-chat"
    assert tiers["T2"]["model"] == "deepseek/deepseek-v3.2"
    assert tiers["T3"]["model"] == "z-ai/glm-4.6"


# ----- writer: validation (fail loud, file untouched) -----------------------


async def test_invalid_slug_empty_raises(writer):
    w, cfg = writer
    before = cfg.read_bytes()
    with pytest.raises(ConfigValidationError):
        await w.swap_tier_model("T2", "")
    assert cfg.read_bytes() == before


async def test_swap_same_model_is_noop(writer):
    # ledger-eventtype-hygiene-v1 Change 3 — a swap to the model the tier already
    # holds is a NO-OP, not a ConfigValidationError. It returns status="noop" and
    # writes NOTHING: file bytes AND mtime unchanged, and no .bak is created.
    w, cfg = writer
    current, _ = _model(cfg, "T2")
    bytes_before = cfg.read_bytes()
    mtime_before = cfg.stat().st_mtime_ns
    bak = cfg.with_suffix(cfg.suffix + ".bak")

    result = await w.swap_tier_model("T2", current)

    assert result.status == "noop"
    assert result.tier == "T2" and result.model == current
    # PIN: no write occurred — the read-only pre-check caught the no-op before
    # apply_mutation's backup/replace could touch the file.
    assert cfg.read_bytes() == bytes_before
    assert cfg.stat().st_mtime_ns == mtime_before
    assert not bak.exists()


async def test_swap_returns_swapped_result(writer):
    w, cfg = writer
    result = await w.swap_tier_model("T2", "deepseek/deepseek-chat")
    assert result.status == "swapped"
    assert result.tier == "T2" and result.model == "deepseek/deepseek-chat"


async def test_invalid_tier_raises(writer):
    w, _ = writer
    # unknown tier, the telemetry pointer (not a tier_preferences key), and the
    # handler-backed T0 (no model) all fail loud.
    for bad in ("T9", "telemetry", "T0"):
        with pytest.raises(ConfigValidationError):
            await w.swap_tier_model(bad, "deepseek/deepseek-chat")


async def test_revert_without_previous_raises(writer):
    w, _ = writer
    with pytest.raises(ConfigValidationError):
        await w.revert_tier_model("T2")


# ----- catalog loader -------------------------------------------------------


def test_catalog_loads_repo_seed():
    catalog = load_catalog()
    assert len(catalog) >= 9
    slugs = {m["slug"] for m in catalog}
    # stable anchors: the default tier models + a Google AI Studio entry
    assert "anthropic/claude-sonnet-4.6" in slugs
    assert "google/gemini-2.5-pro" in slugs


def test_catalog_sovereign_override(tmp_path, monkeypatch):
    sovereign = tmp_path / "model-catalog.yaml"
    sovereign.write_text(
        "models:\n"
        '  - slug: "x/only"\n'
        '    display_name: "Only Model"\n'
        "    provider: openrouter\n"
        "    input_cost_per_mtok: 1.0\n"
        "    output_cost_per_mtok: 2.0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "grove.config.model_catalog._sovereign_catalog_path", lambda: sovereign
    )
    catalog = load_catalog()
    assert [m["slug"] for m in catalog] == ["x/only"]  # AC-9: sovereign wins


def test_catalog_schema_validation_failures():
    with pytest.raises(ValueError, match="non-empty 'models'"):
        _validate_catalog([], Path("t.yaml"))
    with pytest.raises(ValueError, match="slug"):
        _validate_catalog(
            [{"display_name": "x", "provider": "p",
              "input_cost_per_mtok": 1, "output_cost_per_mtok": 2}],
            Path("t.yaml"),
        )
    with pytest.raises(ValueError, match="input_cost_per_mtok"):
        _validate_catalog(
            [{"slug": "a/b", "display_name": "x", "provider": "p",
              "input_cost_per_mtok": "1.0", "output_cost_per_mtok": 2}],
            Path("t.yaml"),
        )


# ----- R-W4: RoutingConfigWriter self-audit (ledger rider) -------------------


async def test_swap_files_routing_config_mutation_event(writer, monkeypatch):
    """The writer audits itself: an apply_mutation files a routing_config_mutation
    Kaizen event under a cli-<utc> sentinel session with surface_class=scope_defining
    (mirrors capability_registry._file_binding_mutation_event)."""
    w, cfg = writer
    captured = []
    from grove import kaizen_ledger as kl
    monkeypatch.setattr(
        kl.KaizenLedger, "record",
        lambda self, event_type, **fields: captured.append(
            (self.session_id, event_type, fields)
        ),
    )
    await w.swap_tier_model("T2", "deepseek/deepseek-chat")
    events = [c for c in captured if c[1] == "routing_config_mutation"]
    assert len(events) == 1
    sid, _et, fields = events[0]
    assert sid.startswith("cli-")                       # sentinel session
    assert fields["surface_class"] == "scope_defining"
    assert "T2" in fields["label"]
    assert fields["config_path"] == str(cfg)


async def test_ledger_filing_failure_does_not_fail_the_mutation(writer, monkeypatch, caplog):
    """Error-log floor: the mutation lands atomically BEFORE filing, so a ledger
    failure must NOT misreport the write as failed — it logs ERROR and stands."""
    import logging
    w, cfg = writer
    from grove import kaizen_ledger as kl

    def _boom(self, *a, **k):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(kl.KaizenLedger, "record", _boom)
    with caplog.at_level(logging.ERROR):
        await w.swap_tier_model("T2", "deepseek/deepseek-chat")  # must NOT raise
    assert "deepseek/deepseek-chat" in cfg.read_text()  # mutation still landed
    assert any("filing failed" in r.getMessage() for r in caplog.records)
