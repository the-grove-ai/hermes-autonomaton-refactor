"""Load-time catalog coherence Andon (model-catalog-v1 M-2 / G-5).

An ACTIVE ``tier_preferences`` binding whose model is absent from the (merged)
catalog is dead config in the making — it would 400 at call time whether or not
a catalog exists, so the router MUST NOT hard-fail or degrade the tier over it
(that would be a boot dependency and itself a silent service degradation).
Instead this surfaces an operator-facing, RECURRING Andon:

  * a **portal badge** — live-read on the routing page, so it reflects the
    current state and clears the moment the binding is reconciled; and
  * a **Kaizen card** — a ledger Andon filed at boot.

NON-FATAL by construction: every entry point swallows its own failure. Lives
OUTSIDE the dispatch path (router/dispatcher never import ``model_catalog`` —
the G-1b isolation invariant), so importing it carries no edge into routing
execution; the router is never coupled to the catalog.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def off_catalog_active_bindings(
    tier_prefs: Any, catalog_slugs: set[str]
) -> list[dict]:
    """``[{"tier": t, "model": m}]`` for each active tier binding whose model is
    not in *catalog_slugs*. Pure — the testable core."""
    out: list[dict] = []
    for tier, entry in (tier_prefs or {}).items():
        if isinstance(entry, dict):
            model = entry.get("model")
            if model and model not in catalog_slugs:
                out.append({"tier": tier, "model": model})
    return out


def _live_tier_prefs() -> dict:
    from grove.router import _resolve_config_path
    from grove.router_merge import load_merged_routing_config

    op = _resolve_config_path(None)
    machine = op.parent / "routing.autonomaton.yaml"
    merged = load_merged_routing_config(op, machine if machine.exists() else None)
    return (merged.get("routing", {}) or {}).get("tier_preferences", {}) or {}


def evaluate_coherence() -> dict:
    """Live report ``{"coherent": bool, "violations": [...]}`` from the merged
    catalog + operator routing config. Read-only."""
    from grove.config.model_catalog import load_catalog

    slugs = {m["slug"] for m in load_catalog()}
    violations = off_catalog_active_bindings(_live_tier_prefs(), slugs)
    return {"coherent": not violations, "violations": violations}


def coherence_badge_html(tier_prefs: Any, catalog: list) -> str:
    """Portal routing-page badge. Empty string when coherent (so it renders
    nothing); a red badge naming every off-catalog binding otherwise. Computed
    from the in-hand live tier_prefs + catalog, so it clears on reconcile."""
    from grove.api.fragments import _esc

    slugs = {m["slug"] for m in (catalog or []) if isinstance(m, dict) and m.get("slug")}
    violations = off_catalog_active_bindings(tier_prefs, slugs)
    if not violations:
        return ""
    items = ", ".join(f"{_esc(v['tier'])}→{_esc(v['model'])}" for v in violations)
    return (
        '<p class="badge badge-red" role="alert">'
        f"Off-catalog tier binding(s): {items} — add the model to the catalog or "
        "rebind the tier. Dispatch is unaffected; this is a coherence warning, "
        "not a failure."
        "</p>"
    )


def _file_coherence_andon(violations: list[dict]) -> None:
    """File one recurring ``catalog_coherence_violation`` Kaizen ledger Andon
    (component-filer sentinel session). Error-log floor — a filing failure must
    not break boot; the badge stands regardless."""
    try:
        from datetime import datetime, timezone

        from grove.kaizen_ledger import KaizenLedger

        sid = "cli-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        KaizenLedger(session_id=sid).record(
            "catalog_coherence_violation",
            violations=violations,
            detail=(
                "active tier_preferences bind a model absent from the model "
                "catalog — add it to the catalog or rebind the tier"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — filing leg, log floor stands
        logger.error(
            "[catalog_coherence] andon filing failed (badge still stands): %r", exc
        )


def check_catalog_coherence_at_boot() -> dict:
    """NON-FATAL boot hook — never raises, never degrades a tier.

    Evaluates coherence; on any off-catalog active binding, logs LOUD and files
    a recurring Kaizen Andon. The portal badge is the live recurring surface.
    Returns the report (``{"coherent", "violations"[, "skipped"]}``).
    """
    try:
        report = evaluate_coherence()
    except Exception as exc:  # noqa: BLE001 — boot must proceed even if config/catalog unreadable
        logger.warning(
            "[catalog_coherence] boot check skipped (unreadable config/catalog): %r",
            exc,
        )
        return {"coherent": True, "violations": [], "skipped": True}
    if not report["coherent"]:
        for v in report["violations"]:
            logger.warning(
                "[catalog_coherence] off-catalog ACTIVE binding: tier=%s model=%s "
                "— dead config; add to catalog or rebind (dispatch unaffected)",
                v["tier"], v["model"],
            )
        _file_coherence_andon(report["violations"])
    return report
