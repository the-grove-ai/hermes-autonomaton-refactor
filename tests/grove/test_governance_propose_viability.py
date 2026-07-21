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
