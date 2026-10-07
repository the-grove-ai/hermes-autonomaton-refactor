"""The Operator Portal's left navigation, rendered from a declared file.

Telemetry stage, presentation only: the nav decides nothing and changes
nothing a page shows. It is read from ``config/portal.nav.yaml`` (the repo
default) or, when the operator has written one, ``~/.grove/portal.nav.yaml``,
which replaces the default whole. Every target is an existing page; a label is
the only thing a nav entry adds.

Two entries are counted live, from the same stores the pages read:

  to_sign      proposals that change the system's authority and so wait for
               the operator's signature (everything in the proposal queue that
               the Proposals page shows as a card);
  suggestions  memory suggestions, which need no signature and are counted on
               their own.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from aiohttp import web

logger = logging.getLogger(__name__)

BADGES = ("to_sign", "suggestions")
LANDING_FIRST_GOAL = "first_goal_with_work"
_ITEM_KEYS = frozenset({
    "label", "target", "badge", "items", "flag", "indent",
    "goals_with_work", "scheduled_skills",
})


def _repo_nav_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "portal.nav.yaml"


def _operator_nav_path() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "portal.nav.yaml"


def _check_item(item: Any, where: str, flags: Mapping[str, Any]) -> None:
    if not isinstance(item, Mapping):
        raise ValueError(f"{where} must be a mapping")
    unknown = sorted(set(item) - _ITEM_KEYS)
    if unknown:
        raise ValueError(f"{where} has unknown keys {unknown}")
    if item.get("flag") is not None and item["flag"] not in flags:
        raise ValueError(f"{where} names flag {item['flag']!r}, which is not declared")
    if item.get("badge") is not None and item["badge"] not in BADGES:
        raise ValueError(f"{where} names badge {item['badge']!r}; it can be {list(BADGES)}")
    if item.get("goals_with_work") or item.get("scheduled_skills"):
        return
    if not isinstance(item.get("label"), str) or not item["label"].strip():
        raise ValueError(f"{where} needs a label")
    if "items" in item:
        if not isinstance(item["items"], list) or not item["items"]:
            raise ValueError(f"{where}: items must be a non-empty list")
        for index, child in enumerate(item["items"]):
            _check_item(child, f"{where} > {item['label']}[{index}]", flags)
    elif not isinstance(item.get("target"), str) or not item["target"].startswith("fragments/"):
        raise ValueError(f"{where} ({item['label']}) needs a target that begins 'fragments/'")


def load_nav(path: Optional[Path] = None) -> Dict[str, Any]:
    """The declared nav. The operator's file wins whole when it exists. A file
    that cannot be read as a nav raises ValueError naming what is wrong — a
    nav is never guessed."""
    import yaml

    if path is None:
        operator = _operator_nav_path()
        path = operator if operator.exists() else _repo_nav_path()
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, Mapping) or not isinstance(data.get("groups"), list):
        raise ValueError(f"portal nav {path} needs a 'groups' list")
    flags = data.get("flags") or {}
    if not isinstance(flags, Mapping) or not all(isinstance(v, bool) for v in flags.values()):
        raise ValueError(f"portal nav {path}: flags must each be true or false")
    hidden = data.get("hidden_skills") or []
    if not isinstance(hidden, list) or not all(isinstance(n, str) for n in hidden):
        raise ValueError(f"portal nav {path}: hidden_skills must be a list of names")
    landing = data.get("landing") or LANDING_FIRST_GOAL
    if landing != LANDING_FIRST_GOAL and not str(landing).startswith("fragments/"):
        raise ValueError(
            f"portal nav {path}: landing must be {LANDING_FIRST_GOAL!r} or a fragments/ target")
    for index, group in enumerate(data["groups"]):
        _check_item(group, f"portal nav {path}: group[{index}]", flags)
    return {"landing": str(landing), "flags": dict(flags), "hidden_skills": list(hidden),
            "groups": list(data["groups"]), "source": str(path)}


# ── what the nav reads live ───────────────────────────────────────────


def goals_with_work() -> List[Dict[str, str]]:
    """Each Dock goal that declares decision work: its id and plain name."""
    from grove.decision_work import load_config
    from grove.dock import load_dock

    out = []
    try:
        dock = load_dock()
        for goal in (getattr(dock, "goals", None) or ()):
            cfg = load_config(goal)
            if cfg is not None:
                name = (cfg.keg.name if cfg.keg else None) or getattr(goal, "name", None) or goal.id
                out.append({"id": str(goal.id), "name": str(name)})
    except Exception as exc:  # noqa: BLE001 — a Dock fault is shown on the goals page
        logger.warning("[portal.nav] could not read the Dock's goals: %r", exc)
    return out


def scheduled_skills(hidden: List[str]) -> List[str]:
    """The skills that run on a schedule or a loop, minus the hidden ones."""
    try:
        from grove.api.fragments import _fleet_index_rows
        names = [str(r.get("name")) for r in _fleet_index_rows() if r.get("name")]
    except Exception as exc:  # noqa: BLE001
        logger.warning("[portal.nav] could not list scheduled skills: %r", exc)
        names = []
    return [n for n in names if n not in set(hidden)]


def counts() -> Dict[str, int]:
    """The two live counts, from the stores the Proposals page reads."""
    from grove.api.fragments import _partition_proposals, read_all_proposals
    from grove.api.portal import pending_memory_proposal_items

    try:
        _artifact, to_sign = _partition_proposals([p.to_dict() for p in read_all_proposals()])
        return {"to_sign": len(to_sign), "suggestions": len(pending_memory_proposal_items())}
    except Exception as exc:  # noqa: BLE001 — a count never costs the nav
        logger.warning("[portal.nav] could not count pending items: %r", exc)
        return {"to_sign": 0, "suggestions": 0}


def landing_target(nav: Mapping[str, Any], goals: List[Dict[str, str]]) -> str:
    """The page the portal opens on."""
    if nav["landing"] != LANDING_FIRST_GOAL:
        return nav["landing"]
    return f"fragments/goal/{goals[0]['id']}" if goals else "fragments/dock/goals"


# ── rendering ─────────────────────────────────────────────────────────


def render_nav(nav: Mapping[str, Any], *, goals: List[Dict[str, str]],
               skills: List[str], live: Mapping[str, int]) -> str:
    """The inner HTML of the nav list. Pure: every live input is passed in."""
    from grove.api.fragments import _esc, _nav_badge

    def link(label: str, target: str, badge: Optional[str], cls: str) -> str:
        count = (_nav_badge(int(live.get(badge, 0))) if badge else "")
        # The count rides on the entry so the shell can tell when it has grown.
        mark = (f' data-badge="{_esc(badge)}" data-count="{int(live.get(badge, 0))}"'
                if badge else "")
        return (f'<li class="{cls}"{mark}><a href="/portal#{_esc(target)}">{_esc(label)}'
                f'{count}</a></li>')

    def item(entry: Mapping[str, Any], depth: int) -> str:
        flag = entry.get("flag")
        if flag is not None and not nav["flags"].get(flag):
            return ""
        level = depth + (1 if entry.get("indent") else 0)
        cls = f"nav-entry lvl{level}"
        if entry.get("goals_with_work"):
            return "".join(link(g["name"], f"fragments/goal/{g['id']}", None, cls)
                           for g in goals)
        if entry.get("scheduled_skills"):
            return "".join(link(name, f"fragments/fleet/{name}/", None, cls)
                           for name in skills)
        if "items" in entry:
            inner = "".join(item(child, level + 1) for child in entry["items"])
            if not inner:
                return ""
            return f'<li class="nav-sub lvl{level}">{_esc(entry["label"])}</li>{inner}'
        return link(entry["label"], entry["target"], entry.get("badge"), cls)

    parts: List[str] = []
    for group in nav["groups"]:
        if group.get("flag") is not None and not nav["flags"].get(group["flag"]):
            continue
        if "items" in group:
            inner = "".join(item(child, 0) for child in group["items"])
            if inner:
                parts.append(f'<li class="nav-group">{_esc(group["label"])}</li>{inner}')
        else:
            # A top-level page of its own: one click from anywhere.
            parts.append(link(group["label"], group["target"], group.get("badge"),
                              "nav-entry nav-top"))
    landing = landing_target(nav, goals)
    parts.append(f'<li hidden id="nav-landing" data-landing="{_esc(landing)}"></li>')
    return "".join(parts)


async def handle_main_nav(request: web.Request) -> web.Response:
    """``GET /portal/fragments/nav/main`` — the whole nav, from the declared
    file and the live counts. A nav file that cannot be read is said so in the
    nav itself, with the old plain links still usable below it."""
    from grove.api.fragments import _esc, _html_fragment

    try:
        nav = load_nav()
    except (ValueError, OSError) as exc:
        logger.error("[portal.nav] nav file unreadable: %r", exc)
        return _html_fragment(
            f'<li class="nav-group">Navigation file unreadable</li>'
            f'<li class="nav-entry lvl0"><span class="meta error">{_esc(str(exc))}</span></li>'
            f'<li class="nav-entry lvl0"><a href="/portal#fragments/dock/goals">Goals</a></li>'
            f'<li class="nav-entry lvl0"><a href="/portal#fragments/proposals/pending">'
            f'Proposals</a></li>'
            f'<li class="nav-entry lvl0"><a href="/portal#fragments/audit/">Audit</a></li>')
    skills = (scheduled_skills(nav["hidden_skills"])
              if nav["flags"].get("scheduled_skills") else [])
    return _html_fragment(render_nav(nav, goals=goals_with_work(), skills=skills, live=counts()))


def register_nav_routes(app: web.Application) -> None:
    app.router.add_get("/portal/fragments/nav/main", handle_main_nav)
