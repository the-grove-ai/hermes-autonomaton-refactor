"""model-catalog-v1 P4 — the add-to-catalog mint tool.

``add_catalog_entry`` lets the operator add a model to the sovereign catalog
(``~/.grove/model-catalog.yaml``) WITHOUT the agent free-handing YAML — a live
run proved that produces a malformed file that fails load. The SYSTEM mints the
schema-valid record from the operator's derived fields (slug, name, pricing),
per the identity-minting-belongs-system-side canon.

The Dispatcher classifies this unlisted tool YELLOW (default), so the existing
sovereignty prompt fires — the operator's approval of THAT prompt is the grant,
and its card renders the fully-resolved merged view (per-slug SHADOWS markers).
On approval the handler mints, per-slug-upserts, validates (schema + referential
guard against the merged view), and atomically writes the sovereign file. The
catalog is live-read, so the model is available immediately — no deploy, no
restart.
"""

from __future__ import annotations

from tools.registry import tool_error

ADD_CATALOG_ENTRY_SCHEMA = {
    "name": "add_catalog_entry",
    "description": (
        "Add a model to the operator's sovereign model catalog so it becomes "
        "available for tier binding (portal dropdown, bind-eligibility). Derive "
        "the fields from the model's page. The system builds the schema-valid "
        "record and stages it for the operator's one-tap approval — do NOT write "
        "the catalog file yourself. Adding a model does NOT bind it to a tier."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "slug": {
                "type": "string",
                "description": (
                    "The exact provider/model slug as it appears on the model's "
                    "page (e.g. 'moonshotai/kimi-k3'). This is the id a tier binds."
                ),
            },
            "display_name": {
                "type": "string",
                "description": "Human-readable name (e.g. 'Kimi K3').",
            },
            "input_cost_per_mtok": {
                "type": "number",
                "description": "USD per million input tokens (display-only heuristic).",
            },
            "output_cost_per_mtok": {
                "type": "number",
                "description": "USD per million output tokens (display-only heuristic).",
            },
            "notes": {
                "type": "string",
                "description": "Optional one-line note (strengths, context window, etc.).",
            },
            "provider": {
                "type": "string",
                "description": (
                    "Routing provider. Default 'openrouter' and almost always "
                    "correct — every slug routes via OpenRouter (incl. Google BYOK). "
                    "The vendor is in the slug prefix, NOT this field."
                ),
            },
            "expected_origin": {
                "type": "string",
                "enum": ["new", "shadows_repo"],
                "description": (
                    "Leave as 'new' for a brand-new model. Set 'shadows_repo' ONLY "
                    "when you intend to override a model already shipped in the repo "
                    "catalog. The system re-checks at write time and refuses if "
                    "reality drifted from what you staged."
                ),
            },
        },
        "required": ["slug", "display_name", "input_cost_per_mtok", "output_cost_per_mtok"],
    },
}


def add_catalog_entry(
    slug: str,
    display_name: str,
    input_cost_per_mtok,
    output_cost_per_mtok,
    notes: str | None = None,
    provider: str = "openrouter",
    expected_origin: str = "new",
    task_id: str = "default",
) -> str:
    """Mint + write one catalog entry (post-approval sanctioned effect).

    By the time this runs the Dispatcher has classified the call YELLOW and the
    operator has approved — the write IS the approved effect. Every failure is a
    loud ``tool_error`` (the deterministic mint never lets a malformed entry
    reach disk)."""
    from grove.config.model_catalog import (
        CatalogWriteError,
        mint_catalog_entry,
        upsert_sovereign_entry,
        write_sovereign_catalog,
    )

    try:
        entry = mint_catalog_entry(
            slug=slug,
            display_name=display_name,
            input_cost_per_mtok=input_cost_per_mtok,
            output_cost_per_mtok=output_cost_per_mtok,
            provider=provider or "openrouter",
            notes=notes,
        )
    except ValueError as exc:
        return tool_error(f"add_catalog_entry: invalid entry — {exc}")

    try:
        new_sovereign = upsert_sovereign_entry(entry, expected_origin=expected_origin or "new")
        path = write_sovereign_catalog(new_sovereign)
    except (CatalogWriteError, ValueError) as exc:
        return tool_error(f"add_catalog_entry: refused — {exc}")
    except Exception as exc:  # noqa: BLE001 — surface any write failure loudly
        return tool_error(f"add_catalog_entry: write failed — {exc!r}")

    return (
        f"Added {entry['slug']} ({entry['display_name']}) to the catalog at {path}. "
        f"It is now available in the portal binding dropdown and eligible for a "
        f"tier bind — no restart. Bind it to a tier when you want it routed."
    )


def register(reg):
    """Auto-discovered by tools.registry.register_builtin_tools. Registered under
    the ``file`` toolset — the operator-approved companion to the catalog."""
    reg.register(
        name="add_catalog_entry",
        toolset="file",
        schema=ADD_CATALOG_ENTRY_SCHEMA,
        handler=lambda args, **kw: add_catalog_entry(
            slug=args.get("slug", ""),
            display_name=args.get("display_name", ""),
            input_cost_per_mtok=args.get("input_cost_per_mtok"),
            output_cost_per_mtok=args.get("output_cost_per_mtok"),
            notes=args.get("notes"),
            provider=args.get("provider", "openrouter"),
            expected_origin=args.get("expected_origin", "new"),
            task_id=kw.get("task_id", "default"),
        ),
        emoji="🗂️",
    )
