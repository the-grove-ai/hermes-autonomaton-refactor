"""Model catalog loader for the portal model-swap UI (portal-model-swap-v1).

The catalog is the menu of models the operator can bind to a tier from the
portal — display metadata only: slug, human name, provider, and display-only
cost heuristics. The Cognitive Router never reads it; when the operator picks a
slug, the routing writer validates it against ``routing.config.yaml``'s own
constraints (a sandbox ``CognitiveRouter`` build), not against this file.

Sovereign merge (AC-9 / M-9): the effective catalog is a PER-SLUG merge of the
repo seed (``config/model-catalog.yaml``) and the operator override
(``~/.grove/model-catalog.yaml``), operator-wins PER SLUG — NOT whole-file
replace. A sovereign entry masks the repo entry sharing its slug; repo entries
the override does not name survive (so a one-line sovereign file adds one model
without blinding the node to the other repo entries or to repo catalog upgrades).
Precedent: the ``~/.grove/capabilities/state/`` slug-keyed overlay. This is a
read path, so ``yaml.safe_load`` (comments are not preserved once parsed).

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


def _load_catalog_file(path: Path) -> list[dict]:
    """Parse + validate one catalog file; reject in-file duplicate slugs."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "models" not in data:
        raise ValueError(f"model catalog at {path} missing top-level 'models' list")
    models = _validate_catalog(data["models"], path)
    seen: set[str] = set()
    for m in models:
        if m["slug"] in seen:
            raise ValueError(
                f"model catalog at {path} has duplicate slug {m['slug']!r} — "
                f"one entry per model id"
            )
        seen.add(m["slug"])
    return models


def merge_catalogs(repo: list[dict], sovereign: list[dict]) -> list[dict]:
    """Per-slug merge, operator-wins per slug (M-9).

    Repo order first. A sovereign entry REPLACES the repo entry sharing its slug
    (in place); a new sovereign slug is appended. Repo entries the override does
    not name SURVIVE — so a one-line sovereign file adds one model without
    blinding the node to the other repo entries or to repo catalog upgrades.
    (Precedent: the ``~/.grove/capabilities/state/`` slug-keyed overlay.)
    """
    by_slug: dict[str, dict] = {}
    order: list[str] = []
    for m in repo:
        by_slug[m["slug"]] = m
        order.append(m["slug"])
    for m in sovereign:
        if m["slug"] not in by_slug:
            order.append(m["slug"])
        by_slug[m["slug"]] = m
    return [by_slug[s] for s in order]


def load_catalog() -> list[dict]:
    """Load the effective (merged) model catalog.

    Per-slug merge of the repo seed (``config/model-catalog.yaml``) and the
    sovereign override (``~/.grove/model-catalog.yaml``), operator-wins per slug
    (M-9). Raises ``FileNotFoundError`` if NEITHER file exists, ``ValueError`` if
    a present file is malformed (N1). This is the single effective vocabulary —
    the portal dropdown, M-2 swap gate, referential guard, and load-time coherence
    Andon all evaluate against this merged view.
    """
    repo_path, sov_path = _repo_catalog_path(), _sovereign_catalog_path()
    repo = _load_catalog_file(repo_path) if repo_path.exists() else []
    sovereign = _load_catalog_file(sov_path) if sov_path.exists() else []
    if not repo and not sovereign:
        raise FileNotFoundError(
            f"no model catalog found (looked for {sov_path} and {repo_path})"
        )
    merged = merge_catalogs(repo, sovereign)
    logger.debug(
        "[model_catalog] merged %d repo + %d sovereign -> %d effective models",
        len(repo), len(sovereign), len(merged),
    )
    return merged


def merged_catalog_provenance() -> list[dict]:
    """The merged catalog with per-entry provenance for the approval card (G-4).

    Each returned dict is ``{**entry, "_origin": ..., "_shadowed_fields": {...}}``:
      * ``_origin`` — ``"repo"`` (repo only), ``"override"`` (sovereign-only slug),
        or ``"override_shadows_repo"`` (sovereign slug that masks a repo entry).
      * ``_shadowed_fields`` — for a shadowing entry, ``field -> {"repo": old,
        "override": new}`` for every field whose value the override changed. The
        card renders the RESOLVED value and marks these as SHADOWS.
    """
    repo_path, sov_path = _repo_catalog_path(), _sovereign_catalog_path()
    repo = _load_catalog_file(repo_path) if repo_path.exists() else []
    sovereign = _load_catalog_file(sov_path) if sov_path.exists() else []
    repo_by_slug = {m["slug"]: m for m in repo}
    sov_by_slug = {m["slug"]: m for m in sovereign}

    out: list[dict] = []
    for entry in merge_catalogs(repo, sovereign):
        slug = entry["slug"]
        rec = dict(entry)
        if slug in sov_by_slug and slug in repo_by_slug:
            old = repo_by_slug[slug]
            shadowed = {
                k: {"repo": old.get(k), "override": entry.get(k)}
                for k in set(old) | set(entry)
                if old.get(k) != entry.get(k)
            }
            rec["_origin"] = "override_shadows_repo"
            rec["_shadowed_fields"] = shadowed
        elif slug in sov_by_slug:
            rec["_origin"] = "override"
            rec["_shadowed_fields"] = {}
        else:
            rec["_origin"] = "repo"
            rec["_shadowed_fields"] = {}
        out.append(rec)
    return out


def get_models_for_tier(tier: str, catalog: list[dict]) -> list[dict]:
    """Return the models offered for ``tier``.

    v1: no per-tier filtering — the operator picks any catalog model for any
    tier, and the routing writer's sandbox validation is the real guardrail.
    ``tier`` is accepted now so a future policy (e.g. hide apex models from T1)
    can filter here without changing callers.
    """
    return list(catalog)


# ── approval-card rendering for a catalog write (model-catalog-v1 M-5/G-4) ────


def is_catalog_path(path: Any) -> bool:
    """True if *path* targets a model catalog file (repo seed or sovereign)."""
    return isinstance(path, str) and path.strip().endswith("model-catalog.yaml")


def _fmt_costs(entry: dict) -> str:
    return f"${entry.get('input_cost_per_mtok')}/${entry.get('output_cost_per_mtok')} per Mtok"


def describe_catalog_write(path: Any, content: Any, *, max_entries: int = 25) -> str | None:
    """Approval-card body for a write to the model catalog (M-5/G-4).

    Renders the FULLY-RESOLVED merged view of the PROPOSED content (treated as
    the would-be sovereign file, merged per-slug over the repo seed), never a
    delta alone. Each written entry shows its resolved fields and a per-slug
    marker: ``[NEW]``, ``[SHADOWS repo: <fields>]`` (override masks repo fields),
    or ``[matches repo]``. Repo entries the write does not name survive silently
    (M-9) — noted in the header so the operator is not misled into thinking they
    vanish.

    Returns ``None`` when *path* is not a catalog file or *content* does not
    parse into a ``models`` list — the caller then falls back to the generic
    write_file render (path + bounded content), so the card never blanks out.
    """
    if not is_catalog_path(path):
        return None
    try:
        data = yaml.safe_load(content)
    except Exception:  # noqa: BLE001 — a malformed proposal falls back, never crashes the card
        return None
    proposed = data.get("models") if isinstance(data, dict) else None
    if not isinstance(proposed, list) or not proposed:
        return None

    repo_path = _repo_catalog_path()
    try:
        repo = _load_catalog_file(repo_path) if repo_path.exists() else []
    except Exception:  # noqa: BLE001 — repo unreadable: still render the proposal, unmarked
        repo = []
    repo_by = {m["slug"]: m for m in repo if isinstance(m, dict) and m.get("slug")}

    lines: list[str] = []
    for m in proposed:
        if not isinstance(m, dict) or not m.get("slug"):
            continue
        slug = m["slug"]
        resolved = f"{slug} | {m.get('display_name')} | {m.get('provider')} | {_fmt_costs(m)}"
        if slug in repo_by:
            old = repo_by[slug]
            masked = sorted(k for k in set(old) | set(m) if old.get(k) != m.get(k))
            marker = f"[SHADOWS repo: {', '.join(masked)}]" if masked else "[matches repo]"
        else:
            marker = "[NEW]"
        lines.append(f"  {resolved} {marker}")

    survivors = len([s for s in repo_by if s not in {m.get("slug") for m in proposed}])
    header = (
        f"Catalog write to {path} — resolved merged view "
        f"({len(lines)} written, {survivors} unlisted repo entr"
        f"{'y' if survivors == 1 else 'ies'} survive):"
    )
    shown = lines[:max_entries]
    if len(lines) > max_entries:
        shown.append(f"  … (+{len(lines) - max_entries} more)")
    return header + "\n" + "\n".join(shown)


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
