"""capability-mutation-surface-v1 T5 — propose viability (banked FAILING).

Gate P0 ruling A-1: the M6 viability refusal lands at the STORE-PENDING
ADMISSION SEAM (``_store_pending_red_proposal`` or its resolution caller),
not in the proposer — it must refuse EROFS/repo-definition targets BEFORE
anything is queued. Phase-0 recon (2026-07-21 VM ledger) showed the live
defect: both a ``propose_governance_change`` and a ``patch`` targeting
``<repo>/config/capabilities/browser_read.yaml`` were store-pended as
approvable cards whose approval was guaranteed to fail (handler refusal /
kernel EROFS) — approvable-but-unappliable churn.

CONTRACT under test:

* The ``propose_governance_change`` handler refusal for a repo-definition
  target is LOUD and ACTIONABLE: it names the deploy SOP (git commit +
  deploy), not just the allowlist.
* ``grove.red_pending_store.is_viable_red_target(tool_name, arguments)
  -> (bool, str)`` — the admission-seam predicate: False (with a
  deploy-SOP-naming reason) for any write effect on a repo-definition
  surface the executor cannot apply.
* ``_store_pending_red_proposal`` consults the predicate (source-level
  wiring pin, same idiom as the writer-conformance scan) so a nonviable
  target never enters red_pending.
"""

from __future__ import annotations

import json
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REPO_CAP_TARGET = str(
    _REPO_ROOT / "config" / "capabilities" / "browser_read.yaml"
)


def test_propose_repo_capabilities_refusal_names_deploy_sop():
    from tools.governance_tool import propose_governance_change

    raw = propose_governance_change(
        target_file=_REPO_CAP_TARGET,
        content="id: browser_read\n",
        rationale="viability test",
    )
    result = json.loads(raw)
    assert result.get("success") is False, (
        "repo capability definitions must never be writable through the "
        "governance door"
    )
    err = str(result.get("error", "")).lower()
    assert "git" in err and "deploy" in err, (
        "CONTRACT: the refusal must point at the sanctioned path (git commit "
        f"+ deploy SOP), not only recite the allowlist; got: {err!r}"
    )


def test_admission_seam_viability_predicate_refuses_repo_definition():
    import grove.red_pending_store as rps

    predicate = getattr(rps, "is_viable_red_target", None)
    assert predicate is not None, (
        "CONTRACT: grove.red_pending_store.is_viable_red_target(tool_name, "
        "arguments) -> (bool, reason) is not implemented — the admission "
        "seam has no viability check (ruling A-1)"
    )
    for tool_name, args in (
        ("patch", {"path": _REPO_CAP_TARGET, "mode": "replace", "content": "x"}),
        (
            "propose_governance_change",
            {"target_file": _REPO_CAP_TARGET, "content": "x", "rationale": "r"},
        ),
    ):
        viable, reason = predicate(tool_name, args)
        assert viable is False, (
            f"CONTRACT: {tool_name} on a repo-definition target must be "
            "nonviable (approval could never apply it)"
        )
        assert "deploy" in reason.lower(), (
            f"CONTRACT: the nonviability reason must name the deploy SOP; "
            f"got {reason!r}"
        )


def test_store_pending_seam_consults_viability_predicate():
    """Wiring pin (ruling A-1): the refusal lands at the store-pending
    admission seam — ``_store_pending_red_proposal`` (or its resolution
    caller) must consult ``is_viable_red_target`` before queueing. Source-
    level pin, same idiom as the writer-conformance basename scan."""
    dispatcher_src = (
        _REPO_ROOT / "grove" / "dispatcher.py"
    ).read_text(encoding="utf-8")
    assert "is_viable_red_target" in dispatcher_src, (
        "CONTRACT: grove/dispatcher.py never consults is_viable_red_target — "
        "nonviable RED targets still enter red_pending as "
        "approvable-but-unappliable cards"
    )


class TestNonviableHaltSurface:
    """P7 HOTFIX pins (live Andon 2026-07-21: seam refusal crashed with
    ValueError — 'red_nonviable_target' is not a valid HaltTrigger). The
    behavioral guarantee, end-to-end through the REAL enum: seam refusal
    completes as a legible TerminalGovernanceHalt whose copy carries the
    seam's reason, no card is minted, and no other exception escapes."""

    def _halt_for(self, tool_name, arguments):
        from types import SimpleNamespace
        return SimpleNamespace(
            intents=[SimpleNamespace(tool_name=tool_name, arguments=arguments)],
            triggering_index=0, zone="red",
            matched_rule="scope_defining:test", pattern_key=None,
            zone_results=[None], reason=None,
        )

    def test_repo_config_write_refusal_is_legible_and_mints_no_card(
        self, tmp_path
    ):
        import pytest as _pytest
        from grove.dispatcher import Dispatcher
        from grove.governance_halt import TerminalGovernanceHalt
        from grove.halt_event import halt_event_from_governance_context
        from grove.halt_renderer import _render_c2a
        from grove.red_pending_store import RedPendingStore

        store = RedPendingStore(db_path=tmp_path / "red.db")
        d = Dispatcher(red_pending_store=store)
        halt = self._halt_for(
            "write_file", {"path": _REPO_CAP_TARGET, "content": "x: 1\n"}
        )
        with _pytest.raises(TerminalGovernanceHalt) as ei:
            d._store_pending_red_proposal(agent=None, gen=None, halt=halt)
        ctx = ei.value.context
        assert ctx.trigger == "red_nonviable_target"
        # The construction-time surface (the live crash site) carried the
        # SOP copy — a governed refusal message, never a traceback.
        surface = str(ei.value)
        assert "git" in surface.lower() and "deploy" in surface.lower()
        assert "Traceback" not in surface and "ValueError" not in surface
        # The C2a boundary adapter resolves the REAL enum member and the
        # renderer reproduces the copy.
        event = halt_event_from_governance_context(ctx)
        rendered = _render_c2a(event)
        assert "deploy" in rendered.lower()
        # Refused BEFORE store-pending: no card minted.
        assert len(store) == 0

    def test_registry_miss_refusal_names_the_miss(self, tmp_path, monkeypatch):
        import pytest as _pytest
        from grove.dispatcher import Dispatcher
        from grove.governance_halt import TerminalGovernanceHalt
        from grove.red_pending_store import RedPendingStore

        monkeypatch.setenv("GROVE_HOME", str(tmp_path))
        store = RedPendingStore(db_path=tmp_path / "red.db")
        d = Dispatcher(red_pending_store=store)
        halt = self._halt_for(
            "propose_governance_change",
            {
                "target_file": str(tmp_path / "zones.schema.yaml"),
                "content": "x: 1\n", "rationale": "r",
            },
        )
        with _pytest.raises(TerminalGovernanceHalt) as ei:
            d._store_pending_red_proposal(agent=None, gen=None, halt=halt)
        assert ei.value.context.trigger == "red_nonviable_target"
        assert "no registered writer" in str(ei.value).lower()
        assert len(store) == 0
