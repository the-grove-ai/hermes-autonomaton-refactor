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
    await w.swap_tier_model("T2", "deepseek/deepseek-v4-flash")
    model, prev = _model(cfg, "T2")
    assert model == "deepseek/deepseek-v4-flash"
    assert prev == before


async def test_revert_toggles_model_and_previous(writer):
    w, cfg = writer
    orig, _ = _model(cfg, "T2")
    await w.swap_tier_model("T2", "deepseek/deepseek-v4-flash")
    await w.revert_tier_model("T2")
    model, prev = _model(cfg, "T2")
    assert model == orig
    # one-level undo: previous now points at the model we reverted away from
    assert prev == "deepseek/deepseek-v4-flash"


async def test_swap_preserves_comments(writer):
    w, cfg = writer
    assert "# DEFAULT PROVIDER" in cfg.read_text()
    await w.swap_tier_model("T2", "deepseek/deepseek-v4-flash")
    after = cfg.read_text()
    assert "# DEFAULT PROVIDER" in after  # AC-8
    assert "THE FOUR TIERS" in after


# ----- writer: concurrency --------------------------------------------------


async def test_concurrent_swaps_no_corruption(writer):
    w, cfg = writer
    await asyncio.gather(
        w.swap_tier_model("T1", "deepseek/deepseek-v4-flash"),
        w.swap_tier_model("T2", "deepseek/deepseek-v4-pro"),
        w.swap_tier_model("T3", "zhipu/glm-5.2"),
    )
    data = ruamel.yaml.YAML().load(cfg.read_text(encoding="utf-8"))
    tiers = data["routing"]["tier_preferences"]
    assert tiers["T1"]["model"] == "deepseek/deepseek-v4-flash"
    assert tiers["T2"]["model"] == "deepseek/deepseek-v4-pro"
    assert tiers["T3"]["model"] == "zhipu/glm-5.2"


# ----- writer: validation (fail loud, file untouched) -----------------------


async def test_invalid_slug_empty_raises(writer):
    w, cfg = writer
    before = cfg.read_bytes()
    with pytest.raises(ConfigValidationError):
        await w.swap_tier_model("T2", "")
    assert cfg.read_bytes() == before


async def test_swap_same_model_raises(writer):
    w, cfg = writer
    current, _ = _model(cfg, "T2")
    with pytest.raises(ConfigValidationError):
        await w.swap_tier_model("T2", current)


async def test_invalid_tier_raises(writer):
    w, _ = writer
    # unknown tier, the telemetry pointer (not a tier_preferences key), and the
    # handler-backed T0 (no model) all fail loud.
    for bad in ("T9", "telemetry", "T0"):
        with pytest.raises(ConfigValidationError):
            await w.swap_tier_model(bad, "deepseek/deepseek-v4-flash")


async def test_revert_without_previous_raises(writer):
    w, _ = writer
    with pytest.raises(ConfigValidationError):
        await w.revert_tier_model("T2")


# ----- catalog loader -------------------------------------------------------


def test_catalog_loads_repo_seed():
    catalog = load_catalog()
    assert len(catalog) >= 9
    assert "deepseek/deepseek-v4-flash" in {m["slug"] for m in catalog}


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
