"""fleet-mcp-warm-unification-v1 P5 — ensure_mcp_warm wired into the dispatch seam.

Drives ``_maybe_dispatch`` end-to-end for each ordered-check outcome and asserts the
surfacer HONORS the broadcast flag: broadcast=True → the operator alert value is passed
to ``surface_fleet_andon``; broadcast=False → it is passed False (P2's
``test_surface_andon_broadcast`` proves that then suppresses only the operator leg while
the log floor + Kaizen filing still fire). The warm runs ONCE per dispatch, before
``resolve_input_state``, so a fleet-only cold window self-heals with no interactive turn.
"""

import pytest

import tools.mcp_tool as mt
from grove.fleet import manager as manager_mod
from grove.fleet.config import WorkerConfig
from grove.fleet.errors import FleetWorkerAndon, OperatorActionRequired


class _FakeHandle:
    run_id = "run-1"


def _cfg():
    return WorkerConfig(
        id="forge",
        skill="skill.fleet.forge-jobsearch",
        enabled=True,
        cadence=None,  # None -> always due
        input_state={"type": "notion_query", "server": "notion", "data_source": "ds"},
    )


@pytest.fixture
def seam(monkeypatch):
    andons = []
    monkeypatch.setattr(
        manager_mod, "surface_fleet_andon",
        lambda wid, run_id, msg, **kw: andons.append({"wid": wid, "msg": msg, **kw}),
    )
    monkeypatch.setattr(manager_mod, "load_fleet_workers", lambda *a, **k: {"forge": _cfg()})
    dispatched = []
    monkeypatch.setattr(
        manager_mod.runner, "dispatch",
        lambda c, p: dispatched.append((c, p)) or _FakeHandle(),
    )
    # resolve returns work by default (proceed path); overridden per-test if needed.
    monkeypatch.setattr(manager_mod, "resolve_input_state", lambda inp, wid: {"rows": [{"id": "r1"}]})
    warmed = []
    return {"andons": andons, "dispatched": dispatched, "warmed": warmed, "mp": monkeypatch}


def _install_warm(mp, warmed, *, raises=None):
    async def _warm(server_id, context):
        warmed.append((server_id, context))
        if raises is not None:
            raise raises
        return None

    mp.setattr(mt, "ensure_mcp_warm", _warm)


def test_warm_server_dispatch_proceeds(seam):
    _install_warm(seam["mp"], seam["warmed"])  # warm returns -> proceed

    manager_mod.FleetManager()._maybe_dispatch(_now())

    assert seam["warmed"] == [("notion", {"wid": "forge"})]  # warmed ONCE, derived server
    assert len(seam["dispatched"]) == 1                       # dispatch proceeded
    assert seam["andons"] == []                               # no halt


def test_cold_server_self_heals_without_interactive_turn(seam):
    # A "cold" server is warmed by ensure_mcp_warm (Check-4) inside the dispatch —
    # the seam only sees "warm returned -> proceed". No interactive turn is involved:
    # the warm is driven by the dispatch itself.
    _install_warm(seam["mp"], seam["warmed"])

    manager_mod.FleetManager()._maybe_dispatch(_now())

    assert ("notion", {"wid": "forge"}) in seam["warmed"]     # dispatch drove the warm
    assert len(seam["dispatched"]) == 1                       # proceeded, no interactive turn
    assert seam["andons"] == []


def test_auth_dead_first_broadcasts_and_halts(seam):
    _install_warm(
        seam["mp"], seam["warmed"],
        raises=OperatorActionRequired("re-auth", check="mcp_auth_dead", broadcast=True),
    )

    manager_mod.FleetManager()._maybe_dispatch(_now())

    assert len(seam["andons"]) == 1
    assert seam["andons"][0]["broadcast"] is True             # loud operator alert
    assert seam["andons"][0]["check"] == "mcp_auth_dead"
    assert seam["dispatched"] == []                           # worker halted


def test_auth_dead_subsequent_is_local_no_repeat_alert(seam):
    _install_warm(
        seam["mp"], seam["warmed"],
        raises=OperatorActionRequired("re-auth", check="mcp_auth_dead", broadcast=False),
    )

    manager_mod.FleetManager()._maybe_dispatch(_now())

    assert len(seam["andons"]) == 1
    assert seam["andons"][0]["broadcast"] is False            # latch-suppressed: no repeat ping
    assert seam["dispatched"] == []                           # still halted (local)


def test_breaker_open_local_no_cadence_storm(seam):
    _install_warm(
        seam["mp"], seam["warmed"],
        raises=FleetWorkerAndon("breaker open", check="mcp_breaker_open", broadcast=False),
    )

    manager_mod.FleetManager()._maybe_dispatch(_now())

    assert len(seam["andons"]) == 1
    assert seam["andons"][0]["broadcast"] is False            # G3: no operator storm
    assert seam["andons"][0]["check"] == "mcp_breaker_open"
    assert seam["dispatched"] == []


def test_warm_runs_before_resolve(seam):
    """Ordering guard: if the warm raises, resolve_input_state is never reached
    (warm is BEFORE resolve, once per dispatch)."""
    resolve_hits = []
    seam["mp"].setattr(
        manager_mod, "resolve_input_state",
        lambda inp, wid: resolve_hits.append(1) or {"rows": []},
    )
    _install_warm(
        seam["mp"], seam["warmed"],
        raises=FleetWorkerAndon("breaker open", check="mcp_breaker_open", broadcast=False),
    )

    manager_mod.FleetManager()._maybe_dispatch(_now())

    assert resolve_hits == []                                 # never resolved — warm gated it
    assert len(seam["andons"]) == 1


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)
