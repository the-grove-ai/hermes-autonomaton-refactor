"""Model catalog loader for the portal model-swap UI (portal-model-swap-v1).

The catalog is the menu of models the operator can bind to a tier from the
portal — display metadata only: slug, human name, provider, and display-only
cost heuristics. The Cognitive Router never reads it; when the operator picks a
slug, the routing writer validates it against ``routing.config.yaml``'s own
constraints (a sandbox ``CognitiveRouter`` build), not against this file.

Sovereign override (AC-9): if ``~/.grove/model-catalog.yaml`` exists it is loaded
INSTEAD of the repo seed (``config/model-catalog.yaml``) — the same operator-wins
precedence the routing config follows. This is a read path, so ``yaml.safe_load``
(the catalog carries no comments worth preserving once parsed).

Schema is validated on load (N1): a file that parses but is missing a required
field, or carries a cost as a string, fails loud HERE rather than degrading into
a broken dropdown downstream.

METADATA-ONLY CONTRACT (model-catalog-v1 G-1a — GATE-B fold):
This file is model *metadata* — id, human name, provider label, display-only
cost. It is NEVER load-bearing for traffic routing. The Cognitive Router does
not read it (dispatch-isolation invariant, test-pinned), and the schema below
REJECTS any unknown field and, explicitly, any endpoint / URL / credential-class
field (``url``, ``endpoint``, ``base_url``, ``api_key``, ``token``, ``secret``,
``auth`` …). A catalog entry therefore cannot carry the data an execution path
would need to resolve a call, so a Yellow catalog write can never alter
Red-walled execution semantics by construction. Add such a field and the
loader fails loud rather than letting the catalog quietly become routing config.

Operator write target (DoD): the sovereign override ``~/.grove/model-catalog.yaml``
— an add-to-catalog is a supervised write to THAT file, no deploy, no restart
(live-read). See ``config/routing.config.yaml`` for where a cataloged model is
bound to a tier (``routing.tier_preferences``) — the catalog says what EXISTS,
routing.config.yaml says what RUNS where.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_REQUIRED_STR_FIELDS = ("slug", "display_name", "provider")
_REQUIRED_NUM_FIELDS = ("input_cost_per_mtok", "output_cost_per_mtok")
_OPTIONAL_FIELDS = ("notes",)
# The complete, closed set of catalog fields. Anything else is rejected on load.
_ALLOWED_FIELDS = frozenset(_REQUIRED_STR_FIELDS + _REQUIRED_NUM_FIELDS + _OPTIONAL_FIELDS)
# Substrings that mark a field as endpoint/URL/credential-class. These can NEVER
# appear in a metadata catalog — their presence would make the file load-bearing
# for traffic routing (G-1a). Matched case-insensitively against the field name.
_CREDENTIAL_FIELD_MARKERS = (
    "url", "endpoint", "base", "host", "uri",
    "api_key", "apikey", "key", "token", "secret", "credential",
    "auth", "bearer", "password", "header",
)


def _repo_catalog_path() -> Path:
    """The repo-shipped seed catalog: ``<repo>/config/model-catalog.yaml``."""
    return Path(__file__).resolve().parents[2] / "config" / "model-catalog.yaml"


def _sovereign_catalog_path() -> Path:
    """The operator override: ``$GROVE_HOME/model-catalog.yaml``."""
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "model-catalog.yaml"


def _catalog_path() -> Path:
    """Resolve which catalog to load — the sovereign override wins (AC-9)."""
    sovereign = _sovereign_catalog_path()
    if sovereign.exists():
        return sovereign
    return _repo_catalog_path()


def _validate_catalog(models: Any, source: Path) -> list[dict]:
    """Validate the parsed catalog; raise ``ValueError`` on any defect (N1).

    Fail loud: a missing required field or a cost given as a string is a config
    error the operator must fix, not something to silently drop. ``bool`` is a
    subclass of ``int`` in Python, so it is rejected explicitly for the numeric
    cost fields (``True``/``False`` are not valid prices).
    """
    if not isinstance(models, list) or not models:
        raise ValueError(
            f"model catalog at {source} must have a non-empty 'models' list"
        )
    for i, entry in enumerate(models):
        where = f"model catalog at {source}, entry [{i}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{where} is not a mapping: {entry!r}")
        for field in _REQUIRED_STR_FIELDS:
            val = entry.get(field)
            if not isinstance(val, str) or not val.strip():
                raise ValueError(
                    f"{where} missing required string field {field!r}: {entry!r}"
                )
        for field in _REQUIRED_NUM_FIELDS:
            val = entry.get(field)
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise ValueError(
                    f"{where} field {field!r} must be a number, got {val!r} "
                    f"({type(val).__name__}) — costs are display-only but must "
                    f"still be numeric"
                )
        # Metadata-only contract (G-1a): the field set is closed. Reject any
        # unknown field, and reject endpoint/credential-class fields with a
        # louder message — the catalog must never become routing-load-bearing.
        for field in entry:
            if field in _ALLOWED_FIELDS:
                continue
            lowered = str(field).lower()
            if any(marker in lowered for marker in _CREDENTIAL_FIELD_MARKERS):
                raise ValueError(
                    f"{where} carries forbidden endpoint/credential-class field "
                    f"{field!r}: the model catalog is metadata-only and can never "
                    f"hold routing-load-bearing data (URLs, endpoints, keys, "
                    f"tokens). Remove it."
                )
            raise ValueError(
                f"{where} has unknown field {field!r} — the catalog schema is "
                f"closed; allowed fields are {sorted(_ALLOWED_FIELDS)}"
            )
    return models


def load_catalog() -> list[dict]:
    """Load and validate the model catalog (sovereign override > repo seed).

    Returns the list of model dicts. Raises ``FileNotFoundError`` if neither
    file exists, ``ValueError`` if the chosen file is malformed (N1).
    """
    path = _catalog_path()
    if not path.exists():
        raise FileNotFoundError(
            f"no model catalog found (looked for {_sovereign_catalog_path()} "
            f"then {_repo_catalog_path()})"
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "models" not in data:
        raise ValueError(f"model catalog at {path} missing top-level 'models' list")
    catalog = _validate_catalog(data["models"], path)
    logger.debug("[model_catalog] loaded %d models from %s", len(catalog), path)
    return catalog


def get_models_for_tier(tier: str, catalog: list[dict]) -> list[dict]:
    """Return the models offered for ``tier``.

    v1: no per-tier filtering — the operator picks any catalog model for any
    tier, and the routing writer's sandbox validation is the real guardrail.
    ``tier`` is accepted now so a future policy (e.g. hide apex models from T1)
    can filter here without changing callers.
    """
    return list(catalog)


# ── referential integrity guard (model-catalog-v1 G-2) ───────────────────────


class CatalogWriteError(RuntimeError):
    """A proposed catalog mutation was refused for referential integrity (G-2)."""


def collect_catalog_referrers() -> dict[str, list[str]]:
    """Live map: model slug -> list of referrer descriptions.

    Two referrer classes hold a model in use:
      * active routing bindings (``routing.tier_preferences.<tier>.model``);
      * per-skill/fleet ``ModelBinding`` records (``type: model``).
    READ-ONLY and best-effort: an unreadable source is logged and contributes
    no referrers rather than crashing the guard. Both sources are read lazily
    (inside this function) so the catalog module carries no import-time edge to
    the router — the dispatch-isolation invariant (G-1b) stays intact.
    """
    referrers: dict[str, list[str]] = {}

    def _add(slug: Any, desc: str) -> None:
        if isinstance(slug, str) and slug.strip():
            referrers.setdefault(slug, []).append(desc)

    # (a) routing tier_preferences bindings
    try:
        from grove.router import _resolve_config_path
        from grove.router_merge import load_merged_routing_config

        op = _resolve_config_path(None)
        machine = op.parent / "routing.autonomaton.yaml"
        merged = load_merged_routing_config(op, machine if machine.exists() else None)
        prefs = (merged.get("routing", {}) or {}).get("tier_preferences", {}) or {}
        for tier, entry in prefs.items():
            if isinstance(entry, dict):
                _add(entry.get("model"), f"tier_preferences.{tier}")
    except Exception as exc:  # noqa: BLE001 — best-effort scan, never crash the guard
        logger.warning("[model_catalog] referrer scan: routing config unreadable: %r", exc)

    # (b) ModelBinding records (type: model)
    try:
        from grove.capability_registry import load_capabilities

        for cap_id, cap in load_capabilities().items():
            mb = getattr(cap, "model_binding", None)
            if mb is not None and getattr(mb, "type", None) == "model":
                _add(getattr(mb, "model", None), f"ModelBinding[{cap_id}]")
    except Exception as exc:  # noqa: BLE001 — best-effort scan, never crash the guard
        logger.warning("[model_catalog] referrer scan: capabilities unreadable: %r", exc)

    return referrers


def assert_safe_catalog_mutation(
    new_models: Any,
    *,
    current_models: list[dict] | None = None,
    referrers: dict[str, list[str]] | None = None,
) -> list[dict]:
    """Fail-closed guard for a proposed catalog write (G-2).

    Validates the proposed catalog against the metadata-only schema, then
    refuses any mutation that REMOVES a slug still referenced by a live routing
    binding or ``ModelBinding`` record. A rename is a delete+add, so renaming a
    referenced slug is refused the same way. The error names every referrer so
    the operator knows exactly what to rebind first. Returns the validated
    models on success.

    ``current_models`` / ``referrers`` are injectable to keep the guard
    unit-testable without touching the live config.
    """
    validated = _validate_catalog(new_models, _sovereign_catalog_path())
    if current_models is None:
        current_models = load_catalog()
    if referrers is None:
        referrers = collect_catalog_referrers()

    current_slugs = {m["slug"] for m in current_models}
    new_slugs = {m["slug"] for m in validated}
    removed = current_slugs - new_slugs
    blocked = {s: referrers[s] for s in sorted(removed) if referrers.get(s)}
    if blocked:
        detail = "; ".join(
            f"{slug} <- referenced by {', '.join(refs)}" for slug, refs in blocked.items()
        )
        raise CatalogWriteError(
            f"refusing catalog write: it removes/renames {len(blocked)} model(s) "
            f"still referenced by live config — {detail}. Rebind or remove the "
            f"referrer(s) first (a rename is a delete+add; rename the binding too)."
        )
    return validated
