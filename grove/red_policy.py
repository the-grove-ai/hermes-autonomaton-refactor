"""red-action-store-pending-v1 Phase B — the DENIED_BY_POLICY deny-list.

The load-bearing guardrail behind the Phase A generalization. A RED action whose
AST-derived ``pattern_key`` matches the deny-list is DENIED BY POLICY: never
store-pending, never executed, regardless of operator reachability. Without it the
generalization would make ``rm -rf /`` (pattern_key ``rm:catastrophic``) a
one-click-approvable pending action.

* **Default deny** (hardcoded, ALWAYS present): the catastrophic-``rm`` effect.
* **Operator extension** (declarative, governed): a ``red_denied_by_policy:``
  top-level list in the governed zones schema — repo ``config/zones.schema.yaml``
  or the operator overlay ``~/.grove/zones.autonomaton.yaml``. Editing it is
  itself a RED governed change (``propose_governance_change``). "Never
  forbidden": the operator holds the policy; the denial is LEGIBLE and names how
  to change it.

A deny-entry matches a ``pattern_key`` by exact string OR family-prefix — an entry
ending in ``":"`` (e.g. ``priv:``) denies the whole effect family.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import FrozenSet, List, Optional

import yaml

logger = logging.getLogger(__name__)

# Always-denied effect signatures — never store-pending, never executable, no
# operator config can remove these (the hardcoded floor unions with, is never
# overridden by, the declarative extension).
_HARDCODED_DENIED: FrozenSet[str] = frozenset({"rm:catastrophic"})

# The operator-editable extension key (a top-level list in the governed zones
# schema). Unknown to the zones LOADER (which reads only schema_version / zones /
# tool_zones), so adding it never perturbs classification.
_DENY_CONFIG_KEY = "red_denied_by_policy"


def _repo_schema_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config" / "zones.schema.yaml"


def _overlay_path() -> Optional[Path]:
    p = Path.home() / ".grove" / "zones.autonomaton.yaml"
    return p if p.exists() else None


def _read_deny_list(path: Optional[Path]) -> List[str]:
    """The ``red_denied_by_policy`` list from *path*, or [] (a broken/absent
    config must never crash RED resolution — fail toward MORE denial, never less:
    the hardcoded floor is applied by the caller regardless)."""
    if path is None or not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except Exception as exc:  # noqa: BLE001 — a broken deny-config must not crash resolution
        logger.warning(
            "[red_policy] deny-list config at %s unreadable (%s); ignoring "
            "(hardcoded catastrophic defaults still apply).", path, exc,
        )
        return []
    val = data.get(_DENY_CONFIG_KEY) if isinstance(data, dict) else None
    return [str(x) for x in val] if isinstance(val, list) else []


def denied_patterns() -> FrozenSet[str]:
    """The active deny-set: hardcoded defaults ∪ operator config (repo ∪ overlay).

    Read fresh (RED halts are rare, and an operator ADDING a deny pattern should
    take effect immediately). The hardcoded floor is ALWAYS present.
    """
    extra = set(_read_deny_list(_repo_schema_path())) | set(_read_deny_list(_overlay_path()))
    return frozenset(_HARDCODED_DENIED | extra)


def _matched_entry(pattern_key: Optional[str]) -> Optional[str]:
    """The deny-set entry that matches *pattern_key* (exact or family-prefix), else None."""
    if not pattern_key:
        return None
    pk = str(pattern_key)
    for d in denied_patterns():
        if pk == d or (d.endswith(":") and pk.startswith(d)):
            return d
    return None


def is_denied_by_policy(pattern_key: Optional[str]) -> bool:
    """True iff *pattern_key* matches the deny-set (exact string or family-prefix)."""
    return _matched_entry(pattern_key) is not None


def is_floor_denial(pattern_key: Optional[str]) -> bool:
    """True iff the matched deny-entry is a HARDCODED floor (never removable)."""
    m = _matched_entry(pattern_key)
    return m is not None and m in _HARDCODED_DENIED


def denial_message(pattern_key: Optional[str]) -> str:
    """Legible DENIED_BY_POLICY copy — HONEST about origin (Option B).

    * **Floor** (hardcoded, never removable): a HARD STRUCTURAL BOUNDARY — the
      agent will not run it even with approval. "Never forbidden" applies to the
      SOVEREIGN OPERATOR (their own hands, outside the agent), NOT the autonomaton.
      No false "remove the pattern" promise.
    * **Operator-config**: names the accurate removal lever (a governed change).
    """
    pk = str(pattern_key or "this effect")
    m = _matched_entry(pattern_key)
    if m is not None and m in _HARDCODED_DENIED:
        return (
            f"Hard structural boundary — I will not run this ({pk}) even with your "
            f"approval. It is a fixed catastrophic-effect floor, not a policy I can "
            f"toggle. If you need it, run it yourself outside the agent."
        )
    return (
        f"Denied by your policy — '{pk}' matches '{m or pk}' in "
        f"'{_DENY_CONFIG_KEY}' (the governed zones schema). Remove that entry — "
        f"itself a RED governed change you approve — to allow it."
    )
