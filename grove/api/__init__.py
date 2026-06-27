"""Operator Portal substrate API package (Sprint P1, portal-api-scaffold-v1).

Read-only GET endpoints under ``/api/substrate/`` that expose the
grove-autonomaton substrate (cellar pages, memory records, Dock goals,
Kaizen proposals, capability records, FTS5 search, system metadata) over
the existing aiohttp gateway. The namespace describes what is exposed,
not who reads it — Fleet workers, CLI, and the portal share one contract.
"""

from grove.api.portal import (
    init_substrate_singletons,
    portal_auth_middleware,
    register_portal_routes,
)

__all__ = [
    "init_substrate_singletons",
    "portal_auth_middleware",
    "register_portal_routes",
]
