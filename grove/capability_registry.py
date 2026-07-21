"""Grove Capability Registry — load declarative Capability records (GRV-009 E2).

Loads every ``config/capabilities/*.yaml`` into a :class:`grove.capability.Capability`
under the E2 migration discipline (GRV-009 Amendment A3): **dry-run validation** —
full ``Capability`` construction at load time, so ``validate()`` fires on every
record before the Router can ever consume it. The Router must never discover a
validation error at runtime.

Fail-loud (Architectural Prime Directive): ANY unreadable / malformed / invalid
record raises :class:`CapabilityLoadError` naming the **filename + offending
field**. The load is all-or-nothing — a partial registry is never returned; one
bad file aborts the whole load.

This module is the loader only. It is consumed by its own tests in E2; the
per-turn disclosure hook that reads the registry lands in E2 commit 3.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, NamedTuple, Optional

import yaml

from grove.capability import (
    EXECUTABLE_STATES,
    LEGAL_TRANSITIONS,
    Capability,
    LifecycleState,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None

__all__ = [
    "BindingWriteError",
    "BindingWriteResult",
    "CapabilityLoadError",
    "default_capabilities_dir",
    "capability_state_dir",
    "read_admission_overlay",
    "publication_unattended_authorized",
    "set_admission_overlay",
    "set_model_binding",
    "write_admission_state",
    "orphaned_state_slugs",
    "load_capabilities",
    "transition_record",
    "TransitionResult",
    "TRANSITION_APPLIED",
    "TRANSITION_DEFERRED",
    "TRANSITION_SKIPPED",
    "register_installed_skill",
    "register_proposed_skill",
    "register_skills_in_tree",
    "skill_record_id_for_name",
    "skill_record_for_name",
    "SkillResolution",
    "resolve_skill_record",
    "scan_skill_slug_collisions",
    "set_skill_pinned",
    "update_lifecycle_fields",
]

logger = logging.getLogger(__name__)

# Process-level guard so the migration-coverage report (uncovered CONFIGURABLE_
# TOOLSETS keys) is logged once per distinct gap, not on every load_capabilities
# call (run_agent loads the registry several times per turn).
_reported_uncovered: set[FrozenSet[str]] = set()


class CapabilityLoadError(RuntimeError):
    """A capability record failed to load or validate.

    The message names the offending file and (via the wrapped validation error)
    the offending field.
    """


class BindingWriteError(RuntimeError):
    """A model_binding write was refused or failed (binding-governance-surfaces-v1).

    Raised by :func:`set_model_binding` on resolution refusal (none/ambiguous/
    inside-lock mismatch), lock contention (operator-initiated writes fail loud,
    never silently defer), binding validation failure, or catalog-membership
    failure. The live record file is restored from backup before this raises.
    """


def default_capabilities_dir() -> Path:
    """The repo-default record directory: ``<repo>/config/capabilities``.

    Holds the version-controlled bundled records (the migrated 92, verbs, MCP).
    """
    return Path(__file__).resolve().parent.parent / "config" / "capabilities"


def grove_home_capabilities_dir() -> Path:
    """The machine-local record directory: ``<GROVE_HOME>/capabilities``.

    GRV-009 E6b C1 — installed/managed records (provenance:installed) mint HERE,
    not into the repo tree. GROVE_HOME is the real per-machine / per-profile
    boundary, so installed state is naturally machine-local and test-isolated
    (a tmp GROVE_HOME yields tmp mints). ``load_capabilities`` overlays this dir
    on the repo dir; the bundled records stay tracked in the repo.
    """
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "capabilities"


def capability_state_dir() -> Path:
    """The node-local capability STATE overlay: ``<GROVE_HOME>/capabilities/state``.

    fleet-hygiene-sweep — the deploy-immune residence for operator-mutable
    capability STATE (model_binding, lifecycle mutables, the transition audit
    log), layered field-wise over the repo-bundled DEFINITIONS by
    :func:`load_capabilities`. Distinct from ``grove_home_capabilities_dir``
    (the whole-file overlay for MINTED records, byte-untouched here): state
    files never carry a whole record, only the allowlisted mutable keys keyed
    by ``id``. Absent dir → the merge is a no-op (fresh-install path)."""
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "capabilities" / "state"


# fleet-hygiene-sweep R-A9 — the STATE allowlist, derived exactly from the
# writer census (set_model_binding / transition_record / update_lifecycle_
# fields). Top-level keys, and the per-block sub-key allowlists. ``id`` is the
# record SELECTOR (identity, not state). ANY key outside these sets makes the
# state file invalid → R-B1 fallback (drop STATE, keep pure definition, loud).
_STATE_TOP_KEYS: FrozenSet[str] = frozenset(
    {"id", "model_binding", "lifecycle", "lineage",
     # operator-mutable-admission-v1 P1 — ADDITIVE admission keys. Read per-turn
     # at the builder (grove.context_budget), NOT applied by _compose_state.
     "added_intents", "force_always",
     # forge-unattended-publish-v1 P1 — operator-mutable publication-autonomy
     # grant. Allowlisted so the operator CAN grant it via STATE overlay (the
     # enable-flag override precedent), but DELIBERATELY NOT applied by
     # _compose_state — the merged runtime Capability never carries it. Its SOLE
     # reader is publication_unattended_authorized(), a strict fail-closed read.
     "publication",
     # skill-adoption-v1 C4 — approval-time payload pin. sha256 of the SKILL.md
     # bytes as written at promotion (sovereignty.promote). Written to STATE by
     # set_approved_payload_hash (operator state, deploy-immune), NOT applied by
     # _compose_state — its SOLE reader is verify_payload_hash(), a fail-closed
     # check the C2 load path (Phase 2) uses to refuse a post-approval mutation.
     "approved_payload_sha256",
     # capability-mutation-surface-v1 M2 (ruling A-3) — CANONICAL admission
     # keys, ABSOLUTE-STATE full-list replacement applied by _compose_state:
     # ``intents`` -> trigger.intents, ``tiers`` -> tier_rule.eligible.
     # ``added_intents`` above stays LEGACY: loader-honored (per-turn additive
     # read at grove.context_budget), never emitted by the sanctioned writer
     # (write_admission_state). ``provenance`` is the mandatory approval stamp
     # on admission-field writes — audit metadata, NOT applied by
     # _compose_state (the merged runtime Capability never carries it).
     "intents", "tiers", "provenance"}
)
_STATE_LIFECYCLE_KEYS: FrozenSet[str] = frozenset(
    {"state", "pinned", "use_count", "last_used"}
)
_STATE_LINEAGE_KEYS: FrozenSet[str] = frozenset({"decision_log"})
# forge-unattended-publish-v1 P1 — the only publication sub-key. A malformed
# publication block (non-mapping, unknown sub-key, non-bool unattended) is the
# R-B1 signal in _read_state_file, and DENY in publication_unattended_authorized.
_STATE_PUBLICATION_KEYS: FrozenSet[str] = frozenset({"unattended"})
# capability-mutation-surface-v1 M2 (ruling A-3) — the REQUIRED provenance
# stamp on admission-field state writes: exactly these four keys, all
# non-empty strings. A partial/malformed stamp is the R-B1 signal in
# _read_state_file and a ValueError refusal in write_admission_state.
_STATE_PROVENANCE_KEYS: FrozenSet[str] = frozenset(
    {"approval_id", "timestamp", "surface", "write_class"}
)


class _StateFileInvalid(Exception):
    """A state file is unparseable, mis-keyed, or fails post-merge validation —
    the R-B1 signal: that record drops STATE and falls back to its pure
    definition (never drops the record, never poisons the load)."""


def _read_state_file(path: Path) -> "tuple[str, Dict[str, Any]]":
    """Parse + allowlist-check ONE state file. Returns ``(record_id, state)``.

    Raises :class:`_StateFileInvalid` on a torn/partial read (atomic-write
    tolerance), a non-mapping doc, a missing/blank ``id``, or ANY key outside
    the allowlist (top-level or within lifecycle/lineage). No merge yet — this
    is pure shape validation so the caller logs file+key precisely."""
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise _StateFileInvalid(f"unreadable/unparseable ({exc})") from exc
    if not isinstance(doc, dict):
        raise _StateFileInvalid("state file is not a mapping")
    rid = doc.get("id")
    if not isinstance(rid, str) or not rid.strip():
        raise _StateFileInvalid("state file missing a non-empty 'id'")
    unknown = set(doc) - _STATE_TOP_KEYS
    if unknown:
        raise _StateFileInvalid(f"unknown top-level key(s) {sorted(unknown)}")
    lc = doc.get("lifecycle")
    if lc is not None:
        if not isinstance(lc, dict):
            raise _StateFileInvalid("'lifecycle' must be a mapping")
        bad = set(lc) - _STATE_LIFECYCLE_KEYS
        if bad:
            raise _StateFileInvalid(f"unknown lifecycle key(s) {sorted(bad)}")
    ln = doc.get("lineage")
    if ln is not None:
        if not isinstance(ln, dict):
            raise _StateFileInvalid("'lineage' must be a mapping")
        bad = set(ln) - _STATE_LINEAGE_KEYS
        if bad:
            raise _StateFileInvalid(f"unknown lineage key(s) {sorted(bad)}")
    # operator-mutable-admission-v1 P1 — ADDITIVE admission keys, shape-checked
    # here so a malformed value is the R-B1 signal (caller falls back to the pure
    # definition; per-turn reader logs an Andon warning and applies no additions).
    ai = doc.get("added_intents")
    if ai is not None and (
        not isinstance(ai, list) or not all(isinstance(x, str) for x in ai)
    ):
        raise _StateFileInvalid("'added_intents' must be a list of strings")
    fa = doc.get("force_always")
    if fa is not None and not isinstance(fa, bool):
        raise _StateFileInvalid("'force_always' must be a boolean")
    # forge-unattended-publish-v1 P1 — publication block, shape-checked here so a
    # malformed grant is the R-B1 signal (mistyped unattended never silently
    # reads as authorized). unattended must be a real bool: isinstance(1, bool)
    # is False, so a truthy int/str is rejected, not coerced.
    pub = doc.get("publication")
    if pub is not None:
        if not isinstance(pub, dict):
            raise _StateFileInvalid("'publication' must be a mapping")
        bad = set(pub) - _STATE_PUBLICATION_KEYS
        if bad:
            raise _StateFileInvalid(f"unknown publication key(s) {sorted(bad)}")
        un = pub.get("unattended")
        if un is not None and not isinstance(un, bool):
            raise _StateFileInvalid("'publication.unattended' must be a boolean")
    # skill-adoption-v1 C4 — the payload pin, shape-checked here so a malformed
    # pin is the R-B1 signal (a torn/non-string pin never silently reads as a
    # valid hash; verify_payload_hash then fails closed on the missing pin).
    aph = doc.get("approved_payload_sha256")
    if aph is not None and (not isinstance(aph, str) or not aph.strip()):
        raise _StateFileInvalid(
            "'approved_payload_sha256' must be a non-empty string"
        )
    # capability-mutation-surface-v1 M2 (ruling A-3) — CANONICAL admission
    # keys, shape-checked here so a malformed value is the R-B1 signal.
    # ``intents``/``tiers`` are absolute-state replacements applied by
    # _compose_state; ``provenance`` is the mandatory approval stamp
    # (exactly _STATE_PROVENANCE_KEYS, all non-empty strings).
    ci = doc.get("intents")
    if ci is not None and (
        not isinstance(ci, list)
        or not all(isinstance(x, str) and x.strip() for x in ci)
    ):
        raise _StateFileInvalid("'intents' must be a list of non-empty strings")
    ct = doc.get("tiers")
    if ct is not None and (
        not isinstance(ct, list)
        or not ct
        or not all(
            isinstance(x, int) and not isinstance(x, bool) and x in (0, 1, 2, 3)
            for x in ct
        )
    ):
        raise _StateFileInvalid(
            "'tiers' must be a non-empty list of ints in {0, 1, 2, 3}"
        )
    prov = doc.get("provenance")
    if prov is not None:
        if not isinstance(prov, dict):
            raise _StateFileInvalid("'provenance' must be a mapping")
        bad = set(prov) - _STATE_PROVENANCE_KEYS
        if bad:
            raise _StateFileInvalid(f"unknown provenance key(s) {sorted(bad)}")
        missing = _STATE_PROVENANCE_KEYS - set(prov)
        if missing:
            raise _StateFileInvalid(
                f"provenance missing key(s) {sorted(missing)}"
            )
        for k in sorted(_STATE_PROVENANCE_KEYS):
            v = prov.get(k)
            if not isinstance(v, str) or not v.strip():
                raise _StateFileInvalid(
                    f"'provenance.{k}' must be a non-empty string"
                )
    return rid, doc


def _compose_state(cap: Capability, state: Dict[str, Any]) -> Capability:
    """Field-wise merge: apply the state's allowlisted keys onto the DEFINITION
    dict, then reconstruct — ``from_dict`` re-runs ``validate()`` in
    ``__post_init__``, so a malformed value (bad model_binding shape, illegal
    state) raises here and the caller falls back (R-B1). decision_log is a FULL
    REPLACEMENT (R-A9): the writer carries the seed entries forward, so the
    state list is the complete chain — never concatenated at load.

    Present-key semantics: a key ABSENT from the state file leaves the
    definition's value; ``model_binding: null`` in state CLEARS the pin."""
    d = cap.to_dict()
    if "model_binding" in state:
        mb = state["model_binding"]
        if mb is None:
            d.pop("model_binding", None)
        else:
            d["model_binding"] = mb
    # capability-mutation-surface-v1 M2 (ruling A-3) — canonical admission
    # keys: ABSOLUTE-STATE full-list replacement, present-key semantics
    # (key present -> wholesale replacement, no union; absent -> definition
    # untouched). ``added_intents`` is deliberately NOT handled here (legacy
    # additive read path at grove.context_budget); ``provenance`` is audit
    # metadata and never lands on the merged runtime Capability. Post-merge
    # from_dict re-validation below keeps the invariants (e.g. a replacement
    # dropping a declared primary intent raises -> R-B1 fallback).
    if "intents" in state:
        d.setdefault("trigger", {})["intents"] = list(state["intents"])
    if "tiers" in state:
        tr = d.setdefault("tier_rule", {})
        new_eligible = list(state["tiers"])
        tr["eligible"] = new_eligible
        # Invariant reconciliation (P2 gate note): the composed record must
        # satisfy ``preferred in eligible``. When the absolute-state
        # restriction excludes the definition's preferred tier, preferred
        # re-anchors to the HIGHEST remaining eligible tier (the closest
        # realization of the definition's prefer-the-most-capable intent) —
        # never silently keeps an inadmissible preference.
        if tr.get("preferred") not in new_eligible:
            tr["preferred"] = max(new_eligible)
    lc = state.get("lifecycle") or {}
    for key in _STATE_LIFECYCLE_KEYS:
        if key in lc:
            d.setdefault("lifecycle", {})[key] = lc[key]
    ln = state.get("lineage") or {}
    if "decision_log" in ln:
        d.setdefault("lineage", {})["decision_log"] = ln["decision_log"]
    try:
        return Capability.from_dict(d)
    except Exception as exc:  # noqa: BLE001 — R-B1: surface, caller falls back
        raise _StateFileInvalid(f"post-merge validation failed: {exc}") from exc


def _compose_state_overlay(records: Dict[str, Capability]) -> None:
    """Layer the node-local STATE overlay over the loaded DEFINITIONS in place.

    Absent state dir → no-op (fresh install). For each state file: match its
    ``id`` to a loaded record and field-merge; a state file for an unknown id
    is a GHOST (warn + skip, never raise — do not poison the load). A per-file
    failure (torn read, mis-keyed, bad value) drops THAT record's state and
    keeps its pure definition, logged CRITICAL with file + reason (R-B1)."""
    state_dir = capability_state_dir()
    if not state_dir.is_dir():
        return
    for path in sorted(state_dir.glob("*.yaml")):
        try:
            rid, state = _read_state_file(path)
        except _StateFileInvalid as exc:
            logger.critical(
                "[grove.capability_registry] STATE overlay file %s is invalid "
                "(%s) — the affected record falls back to its pure definition; "
                "no state applied. Fix or remove the file.", path, exc,
            )
            continue
        if rid not in records:
            logger.warning(
                "[grove.capability_registry] STATE overlay file %s targets id "
                "%r which no loaded definition carries — ghost state, ignored.",
                path, rid,
            )
            continue
        try:
            records[rid] = _compose_state(records[rid], state)
        except _StateFileInvalid as exc:
            logger.critical(
                "[grove.capability_registry] STATE overlay for %r (%s) is "
                "invalid (%s) — dropping STATE for this record, using its pure "
                "definition. Fix or remove the file.", rid, path, exc,
            )
            # records[rid] retains the pre-merge pure-definition Capability.


# ── operator-mutable-admission-v1 P1 — additive admission overlay (per-turn) ──


def read_admission_overlay(
    state_dir: Optional[Path] = None,
) -> "Dict[str, tuple[FrozenSet[str], bool]]":
    """The ADDITIVE admission overlay, read FRESH on every call (no cache).

    Returns ``{record_id: (frozenset(added_intents), force_always_bool)}`` for
    every state file that declares at least one additive admission key. The
    builder (``grove.context_budget``) calls this per resolution so an operator
    (or Kaizen) edit takes effect on the NEXT turn with no restart — the
    deploy-immune sovereignty seam for admission.

    Read-resilient (I2): a torn / mis-keyed / mistyped file logs ONE Andon
    warning and is SKIPPED — that record simply gets no additions and falls back
    to its repo definition. Never raises, never returns partial garbage. Because
    the merge is additive-only, a skipped record can only ever *keep* the repo
    surface, never shrink it. Absent dir → empty map (fresh install)."""
    sd = Path(state_dir) if state_dir is not None else capability_state_dir()
    out: Dict[str, tuple[FrozenSet[str], bool]] = {}
    if not sd.is_dir():
        return out
    for path in sorted(sd.glob("*.yaml")):
        try:
            rid, doc = _read_state_file(path)
        except _StateFileInvalid as exc:
            logger.warning(
                "[grove.capability_registry] admission overlay file %s is invalid "
                "(%s) — that record falls back to its repo definition; no "
                "additions applied.", path, exc,
            )
            continue
        added = doc.get("added_intents") or []
        force = doc.get("force_always") is True
        if not added and not force:
            continue  # a pure model_binding/lifecycle state file — no admission keys
        out[rid] = (frozenset(added), force)
    return out


# ── forge-unattended-publish-v1 P1 — publication authorization (fail-closed) ──


def publication_unattended_authorized(
    record_id: str,
    *,
    directory: Optional[Path] = None,
    state_dir: Optional[Path] = None,
) -> bool:
    """Strict, fail-closed read of ``governance.publication.unattended`` for a
    record. Returns ``True`` ONLY when the effective value ``is True`` (a real
    Python bool). Every other outcome returns ``False``: an absent field, a
    ``false`` value, a string ``"false"`` or any non-bool, a missing overlay, or
    a corrupt/unparseable overlay entry for this record.

    DELIBERATE DIVERGENCE from the R-B1 read-resilient STATE merge
    (``_compose_state_overlay``): there, a corrupt overlay for a record DROPS the
    state and the record keeps its pure-definition value. Here, a corrupt overlay
    entry for this record DENIES — it never resolves to the definition/template
    value. Deny is the only fallback. This is why the read targets the per-record
    STATE file directly and interprets ``_StateFileInvalid`` as ``False`` (and a
    surfaced config error), rather than going through the resilient merge.

    Resolution order:
      1. The operator STATE overlay (``<state_dir>/<id . → __>.yaml``): if the
         file exists but is invalid → DENY. If it carries
         ``publication.unattended`` → that value governs (``is True``).
      2. Otherwise the repo DEFINITION's ``governance.publication.unattended``
         (absent ≡ ``False``). The bundled record ships this absent/false.

    forge-unattended-publish-v1 P1 is INERT: no caller wires this yet.
    """
    # 1. Operator STATE overlay — read STRICTLY for THIS record's own file.
    sd = Path(state_dir) if state_dir is not None else capability_state_dir()
    ov_path = _state_path_for_id(record_id, sd)
    if ov_path.exists():
        try:
            _rid, doc = _read_state_file(ov_path)
        except _StateFileInvalid as exc:
            logger.error(
                "[grove.capability_registry] publication authorization DENIED for "
                "%r — STATE overlay %s is invalid (%s). Fail-closed: no resilient "
                "fallback to the definition. Fix or remove the file.",
                record_id, ov_path, exc,
            )
            return False
        pub = doc.get("publication")
        if isinstance(pub, dict) and "unattended" in pub:
            return pub.get("unattended") is True
        # Overlay present but silent on publication → the definition governs.

    # 2. Repo DEFINITION — pure (no state overlay); absent field ≡ deny.
    def_dir = Path(directory) if directory is not None else default_capabilities_dir()
    try:
        defs = load_capabilities(directory=def_dir)
    except CapabilityLoadError as exc:
        logger.error(
            "[grove.capability_registry] publication authorization DENIED for %r — "
            "definition load failed (%s).", record_id, exc,
        )
        return False
    cap = defs.get(record_id)
    if cap is None or not isinstance(cap.governance, dict):
        return False
    pub = cap.governance.get("publication")
    if not isinstance(pub, dict):
        return False
    return pub.get("unattended") is True


def set_admission_overlay(
    cap_id: str,
    *,
    add_intents: Optional[List[str]] = None,
    force_always: Optional[bool] = None,
    directory: Optional[Path] = None,
    state_dir: Optional[Path] = None,
) -> str:
    """The SOLE sanctioned writer for the additive admission overlay (P1).

    UNIONs *add_intents* into the record's ``added_intents`` and/or sets
    ``force_always: true`` on the record's ``~/.grove/capabilities/state`` file,
    preserving any Capability-state keys (model_binding / lifecycle / lineage)
    already in that file. Same lock + atomic + ``.bak`` discipline as the other
    state writers.

    WRITE-STRICT (fail loud): rejects a non-list *add_intents*, a non-str intent,
    any *force_always* other than ``True`` (additive-only — a default is removed
    by editing the repo definition, never by operator state), and a no-op call.
    Raises :class:`CapabilityLoadError` when no definition carries *cap_id*.

    Returns ``"applied"`` or ``"deferred"`` (lock contended — caller retries)."""
    if add_intents is not None and (
        not isinstance(add_intents, list)
        or not all(isinstance(x, str) for x in add_intents)
    ):
        raise ValueError(
            "set_admission_overlay: add_intents must be a list of strings"
        )
    if force_always is not None and force_always is not True:
        raise ValueError(
            "set_admission_overlay: force_always accepts only True — a default is "
            "removed by editing the repo definition, never by operator state"
        )
    if not add_intents and force_always is None:
        raise ValueError(
            "set_admission_overlay: no-op — provide add_intents and/or "
            "force_always=True"
        )

    if directory is not None:
        search_dirs = [Path(directory)]
    else:
        search_dirs = [default_capabilities_dir(), grove_home_capabilities_dir()]
    path = None
    for d in search_dirs:
        if d.is_dir():
            path = _record_path_for_id(cap_id, d)
            if path is not None:
                break
    if path is None:
        raise CapabilityLoadError(
            f"set_admission_overlay: no capability record with id {cap_id!r} in "
            f"{[str(d) for d in search_dirs]}"
        )

    state_path = _state_path_for_id(cap_id, state_dir or capability_state_dir())

    def _apply() -> str:
        prior: Dict[str, Any] = {}
        if state_path.exists():
            try:
                loaded = yaml.safe_load(state_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    prior = loaded
            except yaml.YAMLError:
                prior = {}  # torn prior; .bak below retains the bytes
        merged = dict(prior)
        merged["id"] = cap_id
        if add_intents:
            existing = merged.get("added_intents")
            if not isinstance(existing, list):
                existing = []
            merged["added_intents"] = sorted(
                {x for x in existing if isinstance(x, str)} | set(add_intents)
            )
        if force_always is True:
            merged["force_always"] = True
        state_path.parent.mkdir(parents=True, exist_ok=True)
        prior_bytes = state_path.read_bytes() if state_path.exists() else b""
        if prior_bytes:
            state_path.with_suffix(state_path.suffix + ".bak").write_bytes(prior_bytes)
        _atomic_write_yaml(
            state_path,
            yaml.safe_dump(merged, sort_keys=False, allow_unicode=True),
        )
        return "applied"

    if fcntl is None:  # pragma: no cover - non-POSIX best-effort
        return _apply()

    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_suffix(".yaml.lock")
    fd = open(lock_path, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            return "deferred"
        try:
            return _apply()
        finally:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    finally:
        fd.close()


def write_admission_state(
    record_id: str,
    *,
    intents: Optional[List[str]] = None,
    tiers: Optional[List[int]] = None,
    provenance: Optional[Dict[str, Any]] = None,
    state_dir: Optional[Path] = None,
) -> Path:
    """The SOLE sanctioned writer for CANONICAL admission-field state
    (capability-mutation-surface-v1 M2, ruling A-3; uniqueness pinned by the
    writer-conformance guard's admission-writer signature: this function is
    the one place that both calls ``_atomic_write_yaml`` and carries the
    ``provenance`` stamp).

    ABSOLUTE-STATE per-record replacement: ``intents`` / ``tiers`` land on the
    record's state file wholesale (no union with prior values); ``_compose_
    state`` applies them as full-list replacements of ``trigger.intents`` /
    ``tier_rule.eligible``. Emits ONLY the canonical keys ``{id, intents,
    tiers, provenance}`` on the admission surface — ``added_intents`` is
    legacy (loader-honored, never writer-emitted) and is RETIRED from a file
    this writer touches (the canonical replacement supersedes it). Non-
    admission state keys already in the file (model_binding / lifecycle /
    lineage / publication / force_always / approved_payload_sha256) are
    preserved byte-faithfully.

    WRITE-STRICT (fail loud, validate-before-touch): a missing or partial
    ``provenance`` stamp (exactly ``_STATE_PROVENANCE_KEYS``, all non-empty
    strings), a malformed ``intents``/``tiers`` value, an unknown *record_id*,
    or a no-op call raises with NO file written. Same lock + ``.bak`` +
    atomic-replace discipline as the sibling state writers.
    """
    # ── validation FIRST: a refusal must leave nothing behind ──
    if not isinstance(record_id, str) or not record_id.strip():
        raise ValueError("write_admission_state: record_id must be a non-empty string")
    if intents is None and tiers is None:
        raise ValueError(
            "write_admission_state: no-op — provide intents and/or tiers"
        )
    if intents is not None and (
        not isinstance(intents, list)
        or not all(isinstance(x, str) and x.strip() for x in intents)
    ):
        raise ValueError(
            "write_admission_state: intents must be a list of non-empty strings"
        )
    if tiers is not None and (
        not isinstance(tiers, list)
        or not tiers
        or not all(
            isinstance(x, int) and not isinstance(x, bool) and x in (0, 1, 2, 3)
            for x in tiers
        )
    ):
        raise ValueError(
            "write_admission_state: tiers must be a non-empty list of ints in "
            "{0, 1, 2, 3}"
        )
    # The mandatory approval stamp — stampless/partial admission writes are
    # refused (ruling A-3; scope-defining writes carry their provenance).
    if not isinstance(provenance, dict):
        raise ValueError(
            "write_admission_state: refused — admission-field writes require a "
            f"provenance stamp {{{', '.join(sorted(_STATE_PROVENANCE_KEYS))}}}"
        )
    _bad = set(provenance) - _STATE_PROVENANCE_KEYS
    if _bad:
        raise ValueError(
            f"write_admission_state: unknown provenance key(s) {sorted(_bad)}"
        )
    _missing = _STATE_PROVENANCE_KEYS - set(provenance)
    if _missing:
        raise ValueError(
            "write_admission_state: refused — provenance stamp missing "
            f"{sorted(_missing)} (partial stamps are not admissible)"
        )
    for _k in sorted(_STATE_PROVENANCE_KEYS):
        _v = provenance.get(_k)
        if not isinstance(_v, str) or not _v.strip():
            raise ValueError(
                f"write_admission_state: provenance.{_k} must be a non-empty "
                "string"
            )
    # Ghost prevention through the sanctioned door: the id must name a loaded
    # DEFINITION (repo-bundled or GROVE_HOME-minted). Orphans that arrive by
    # other means are surfaced by orphaned_state_slugs().
    _def_path = None
    for _d in (default_capabilities_dir(), grove_home_capabilities_dir()):
        if _d.is_dir():
            _def_path = _record_path_for_id(record_id, _d)
            if _def_path is not None:
                break
    if _def_path is None:
        raise CapabilityLoadError(
            f"write_admission_state: no capability record with id {record_id!r}"
        )

    state_path = _state_path_for_id(record_id, state_dir or capability_state_dir())

    # ── locked read-modify-write (inline — the conformance signature keys on
    # THIS function as the primitive caller; no nested closure) ──
    lock_fd = None
    if fcntl is not None:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = open(
            state_path.with_suffix(".yaml.lock"), "a+", encoding="utf-8"
        )
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
    try:
        prior: Dict[str, Any] = {}
        if state_path.exists():
            try:
                loaded = yaml.safe_load(state_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    prior = loaded
            except yaml.YAMLError:
                prior = {}  # torn prior; .bak below retains the bytes
        merged = dict(prior)
        merged["id"] = record_id
        # Canonical replacement of the ADMISSION surface: legacy added_intents
        # is retired from this file (superseded, never writer-emitted).
        merged.pop("added_intents", None)
        if intents is not None:
            merged["intents"] = list(intents)
        if tiers is not None:
            merged["tiers"] = list(tiers)
        merged["provenance"] = dict(provenance)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        prior_bytes = state_path.read_bytes() if state_path.exists() else b""
        if prior_bytes:
            state_path.with_suffix(state_path.suffix + ".bak").write_bytes(
                prior_bytes
            )
        _atomic_write_yaml(
            state_path,
            yaml.safe_dump(merged, sort_keys=False, allow_unicode=True),
        )
    finally:
        if lock_fd is not None:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
            lock_fd.close()
    return state_path


def orphaned_state_slugs(
    records: Dict[str, Capability],
    state_dir: Optional[Path] = None,
) -> List[tuple]:
    """Ghost-state census (capability-mutation-surface-v1 M2): every state
    overlay file whose ``id`` matches no loaded definition, as ``(path, id)``
    pairs. The compose path warn-skips ghosts per-load (R-B1 — never poison
    the merge); this is the DETECTION surface callers use to reconcile them.
    Unparseable/non-mapping files are not orphans (they are the R-B1 invalid
    class, already logged loud at compose time) and are skipped here."""
    sd = Path(state_dir) if state_dir is not None else capability_state_dir()
    if not sd.is_dir():
        return []
    orphans: List[tuple] = []
    for path in sorted(sd.glob("*.yaml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(doc, dict):
            continue
        rid = doc.get("id")
        if isinstance(rid, str) and rid.strip() and rid not in records:
            orphans.append((path, rid))
    return orphans


def set_publication_state(
    record_id: str,
    value: bool,
    *,
    directory: Optional[Path] = None,
    state_dir: Optional[Path] = None,
) -> str:
    """The SOLE sanctioned writer for the operator publication-autonomy grant
    (``governance.publication.unattended``) — write-routing-coherence-v1 fix-part-3.

    Sets ``publication: {unattended: <value>}`` on the record's
    ``~/.grove/capabilities/state`` file, preserving any other Capability-state
    keys (``model_binding`` / ``lifecycle`` / ``lineage`` / ``added_intents`` /
    ``force_always``) already there. Same lock + atomic + ``.bak`` discipline as
    :func:`set_admission_overlay`.

    STATE-OVERLAY-ONLY / REPO-WRITE-INCAPABLE: the repo definition is consulted
    READ-ONLY (to validate *record_id* exists); the ONLY path written is
    ``_state_path_for_id(record_id, state_dir)`` under
    :func:`capability_state_dir`. There is no code route from here to
    ``config/capabilities/`` — the grant is deploy-immune, so the ``git reset
    --hard`` misfire class (a grant patched into the deployed definition, then
    silently reverted on the next deploy) is structurally closed.

    The state filename is resolved by the SAME resolver the reader
    (:func:`publication_unattended_authorized`) uses — ``_state_path_for_id``
    (id ``.`` → ``__``) — so a record id like ``skill.fleet.forge-jobsearch``
    canonically lands in ``skill__fleet__forge-jobsearch.yaml`` (NOT the
    definition file's ``skill__fleet__forge_jobsearch.yaml``); write and read
    agree on the file by construction.

    WRITE-STRICT (fail loud): rejects a blank *record_id* and a non-bool *value*
    (``bool`` is checked explicitly — no truthy/falsy coercion; a scalar grant is
    set, never inferred). Raises :class:`CapabilityLoadError` when no definition
    carries *record_id*.

    Returns ``"applied"`` or ``"deferred"`` (lock contended — caller retries)."""
    if not isinstance(record_id, str) or not record_id.strip():
        raise ValueError(
            "set_publication_state: record_id must be a non-empty string"
        )
    # A REAL bool only. bool is a subclass of int, so an explicit isinstance check
    # rejects 1/0/"true" — the ANDON: never force-fit a coercion onto the grant.
    if not isinstance(value, bool):
        raise ValueError(
            "set_publication_state: value must be a real bool (True/False), got "
            f"{type(value).__name__}"
        )
    record_id = record_id.strip()

    if directory is not None:
        search_dirs = [Path(directory)]
    else:
        search_dirs = [default_capabilities_dir(), grove_home_capabilities_dir()]
    path = None
    for d in search_dirs:
        if d.is_dir():
            path = _record_path_for_id(record_id, d)
            if path is not None:
                break
    if path is None:
        raise CapabilityLoadError(
            f"set_publication_state: no capability record with id {record_id!r} in "
            f"{[str(d) for d in search_dirs]}"
        )

    state_path = _state_path_for_id(record_id, state_dir or capability_state_dir())

    def _apply() -> str:
        prior: Dict[str, Any] = {}
        if state_path.exists():
            try:
                loaded = yaml.safe_load(state_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    prior = loaded
            except yaml.YAMLError:
                prior = {}  # torn prior; .bak below retains the bytes
        merged = dict(prior)
        merged["id"] = record_id
        # Whole-block set: `publication` has exactly one allowlisted sub-key
        # (`unattended`), so there is no sub-key merge to force-fit onto the bool.
        merged["publication"] = {"unattended": value}
        state_path.parent.mkdir(parents=True, exist_ok=True)
        prior_bytes = state_path.read_bytes() if state_path.exists() else b""
        if prior_bytes:
            state_path.with_suffix(state_path.suffix + ".bak").write_bytes(prior_bytes)
        _atomic_write_yaml(
            state_path,
            yaml.safe_dump(merged, sort_keys=False, allow_unicode=True),
        )
        return "applied"

    if fcntl is None:  # pragma: no cover - non-POSIX best-effort
        return _apply()

    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_suffix(".yaml.lock")
    fd = open(lock_path, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            return "deferred"
        try:
            return _apply()
        finally:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    finally:
        fd.close()


# ── skill-adoption-v1 C4 — approval-time payload pin (state overlay) ──────────


def _sha256_hex(data: bytes) -> str:
    """Full lowercase hex sha256 of *data* — the pin format shared by the writer
    (:func:`sovereignty.promote`) and :func:`verify_payload_hash`."""
    return hashlib.sha256(data).hexdigest()


def set_approved_payload_hash(
    record_id: str,
    payload_sha256: str,
    *,
    directory: Optional[Path] = None,
    state_dir: Optional[Path] = None,
) -> str:
    """The SOLE sanctioned writer for the approval-time payload pin
    (``approved_payload_sha256``) — skill-adoption-v1 C4.

    Writes ``approved_payload_sha256: <hex>`` onto the record's
    ``~/.grove/capabilities/state`` file, preserving every other state key
    (``model_binding`` / ``lifecycle`` / ``lineage`` / ``added_intents`` /
    ``force_always`` / ``publication``) already there. Same lock + atomic +
    ``.bak`` discipline as :func:`set_publication_state`.

    STATE-OVERLAY-ONLY / REPO-WRITE-INCAPABLE (parity with the publication
    writer): the repo definition is consulted READ-ONLY (to validate *record_id*
    exists); the ONLY path written is the state file. The pin is deploy-immune.

    WRITE-STRICT (fail loud): rejects a blank *record_id* / *payload_sha256*.
    Raises :class:`CapabilityLoadError` when no definition carries *record_id*.
    Returns ``"applied"`` or ``"deferred"`` (lock contended — caller retries)."""
    if not isinstance(record_id, str) or not record_id.strip():
        raise ValueError("set_approved_payload_hash: record_id must be non-empty")
    if not isinstance(payload_sha256, str) or not payload_sha256.strip():
        raise ValueError(
            "set_approved_payload_hash: payload_sha256 must be a non-empty string"
        )

    if directory is not None:
        search_dirs = [Path(directory)]
    else:
        search_dirs = [default_capabilities_dir(), grove_home_capabilities_dir()]
    path = None
    for d in search_dirs:
        if d.is_dir():
            path = _record_path_for_id(record_id, d)
            if path is not None:
                break
    if path is None:
        raise CapabilityLoadError(
            f"set_approved_payload_hash: no capability record with id "
            f"{record_id!r} in {[str(d) for d in search_dirs]}"
        )

    state_path = _state_path_for_id(record_id, state_dir or capability_state_dir())

    def _apply() -> str:
        prior: Dict[str, Any] = {}
        if state_path.exists():
            try:
                loaded = yaml.safe_load(state_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    prior = loaded
            except yaml.YAMLError:
                prior = {}  # torn prior; .bak below retains the bytes
        merged = dict(prior)
        merged["id"] = record_id
        merged["approved_payload_sha256"] = payload_sha256
        state_path.parent.mkdir(parents=True, exist_ok=True)
        prior_bytes = state_path.read_bytes() if state_path.exists() else b""
        if prior_bytes:
            state_path.with_suffix(state_path.suffix + ".bak").write_bytes(prior_bytes)
        _atomic_write_yaml(
            state_path,
            yaml.safe_dump(merged, sort_keys=False, allow_unicode=True),
        )
        return "applied"

    if fcntl is None:  # pragma: no cover - non-POSIX best-effort
        return _apply()

    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_suffix(".yaml.lock")
    fd = open(lock_path, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            return "deferred"
        try:
            return _apply()
        finally:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    finally:
        fd.close()


def approved_payload_hash_for(
    record_id: str, *, state_dir: Optional[Path] = None
) -> Optional[str]:
    """The pinned ``approved_payload_sha256`` for *record_id*, or ``None``.

    Fail-closed read (parity with :func:`publication_unattended_authorized`): a
    missing state file, a torn/invalid state file, or an absent pin all return
    ``None`` — never a partial or guessed hash."""
    state_path = _state_path_for_id(record_id, state_dir or capability_state_dir())
    if not state_path.exists():
        return None
    try:
        _rid, doc = _read_state_file(state_path)
    except _StateFileInvalid:
        return None
    pin = doc.get("approved_payload_sha256")
    return pin if isinstance(pin, str) and pin.strip() else None


def verify_payload_hash(record: Capability, *, state_dir: Optional[Path] = None) -> bool:
    """Does *record*'s ACTIVE SKILL.md still match its approval-time pin?

    Reads the pin from the state overlay and the CURRENT active SKILL.md bytes,
    and compares sha256. FAIL-CLOSED: a missing pin, a missing/unreadable
    payload, or any mismatch returns ``False`` — an unpinned or mutated payload
    never verifies. The C2 load path (Phase 2) gates on this. No consumer is
    wired this phase."""
    pin = approved_payload_hash_for(record.id, state_dir=state_dir)
    if pin is None:
        return False
    from grove.skills import active_path

    slug = record.id.rsplit(".", 1)[-1]
    skill_md = active_path(slug) / "SKILL.md"
    try:
        payload = skill_md.read_bytes()
    except OSError:
        return False
    return _sha256_hex(payload) == pin


# ── state WRITE path (fleet-hygiene-sweep P2) ────────────────────────────
#
# The three runtime writers (set_model_binding / transition_record /
# update_lifecycle_fields) target the STATE overlay, NEVER the bundled
# definition — that is the whole point: definitions are read-only to the
# runtime so deploy-by-reset cannot destroy operator state. Each writer reads
# the COMPOSED record (definition + current state, R-A9), applies its mutation,
# and writes the COMPLETE allowlisted snapshot to the state file (so
# decision_log replacement is lossless — the seed + all prior entries ride the
# composed record forward). Same lock + atomic + .bak discipline as before, at
# the new target.


def _state_path_for_id(cap_id: str, state_dir: Path) -> Path:
    """The state file for *cap_id*: ``<state_dir>/<id with . -> __>.yaml`` — the
    mint filename idiom, so the file is human-locatable. The ``id`` INSIDE the
    file is authoritative for the loader; the filename is cosmetic."""
    return Path(state_dir) / f"{cap_id.replace('.', '__')}.yaml"


def _state_snapshot_dict(cap: Capability) -> Dict[str, Any]:
    """Serialize the allowlisted MUTABLE surface of *cap* to a state dict.

    The complete snapshot every write: ``model_binding`` (null when unpinned —
    an honest clear), the four lifecycle mutables, and the full decision_log.
    Reuses ``to_dict`` for the exact per-block shapes the loader re-parses."""
    d = cap.to_dict()
    snapshot: Dict[str, Any] = {"id": cap.id}
    snapshot["model_binding"] = d.get("model_binding")  # None when unpinned
    lc = d["lifecycle"]
    snapshot["lifecycle"] = {k: lc[k] for k in _STATE_LIFECYCLE_KEYS if k in lc}
    snapshot["lineage"] = {"decision_log": d["lineage"]["decision_log"]}
    return snapshot


def _compose_for_write(def_path: Path, state_path: Path) -> Capability:
    """Read the COMPOSED record for a writer (R-A9): the definition overlaid
    with its CURRENT state file, if any. A corrupt existing state file fails
    LOUD here — a writer must never silently clobber unreadable operator state
    (distinct from the loader's read-side R-B1 fallback, which keeps the record
    loadable; a write demands the state be legible first)."""
    cap = Capability.from_yaml(def_path.read_text(encoding="utf-8"))
    if state_path.exists():
        _rid, state = _read_state_file(state_path)  # raises _StateFileInvalid loud
        cap = _compose_state(cap, state)
    return cap


def _write_state_snapshot(cap: Capability, state_path: Path) -> bytes:
    """Atomically write *cap*'s allowlisted snapshot to *state_path* (.bak of any
    prior bytes first, for the caller's restore-on-failure). Returns the prior
    bytes (b'' when the file is new) so the caller can roll back."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    prior = state_path.read_bytes() if state_path.exists() else b""
    if prior:
        state_path.with_suffix(state_path.suffix + ".bak").write_bytes(prior)
    import yaml as _yaml

    snapshot = _state_snapshot_dict(cap)
    # operator-mutable-admission-v1 P1 — carry the ADDITIVE admission keys forward.
    # This writer owns only the Capability-state surface; it must NOT erase the
    # operator's added_intents / force_always written by set_admission_overlay to
    # the SAME state file (one sovereignty seam). A torn prior falls through to a
    # Capability-only snapshot — the .bak retains the operator bytes for recovery.
    # skill-adoption-v1 C4 — approved_payload_sha256 rides the SAME carry-forward:
    # a routine lifecycle write (use_count bump, refine) must NOT drop the pin, or
    # the C2 load path would fail-closed on an unmutated skill.
    if prior:
        try:
            prior_doc = _yaml.safe_load(prior)
            if isinstance(prior_doc, dict):
                for _k in ("added_intents", "force_always", "approved_payload_sha256"):
                    if _k in prior_doc:
                        snapshot[_k] = prior_doc[_k]
        except _yaml.YAMLError:
            pass

    _atomic_write_yaml(
        state_path,
        _yaml.safe_dump(snapshot, sort_keys=False, allow_unicode=True),
    )
    return prior


def _validate_binding_uniqueness(records: Dict[str, Capability]) -> None:
    """Strict 1:1 tool-to-record ownership (GRV-009 E5 Amendment A4).

    A collection-level post-load pass: scans every record's ``bindings.tools``
    and fails loud — naming both owning records and the colliding tool — if any
    tool name is claimed by two records. Inert until the C-BACKFILL / C-VERBS
    records populate bindings; the invariant exists from the schema commit so the
    resolution swap (C-RESOLVE) can trust single-owner attribution.
    """
    owner: Dict[str, str] = {}
    for rid in sorted(records):
        for tool in records[rid].bindings.tools:
            if tool in owner:
                raise CapabilityLoadError(
                    f"binding collision: tool {tool!r} is claimed by both "
                    f"{owner[tool]!r} and {rid!r} — A4 requires strict 1:1 "
                    f"tool-to-record ownership"
                )
            owner[tool] = rid


def _configurable_toolset_keys() -> FrozenSet[str]:
    """The known CONFIGURABLE_TOOLSETS keys, imported lazily.

    GRV-009 E5 C-SEAM4 — the import is deferred to call time (not module top) so
    the capability layer carries no import-time dependency on the CLI layer; no
    circular coupling. ``tools_config`` does not import the capability layer, so
    by the time the post-load pass runs both modules are fully resolved.
    """
    from hermes_cli.tools_config import CONFIGURABLE_TOOLSETS
    return frozenset(key for key, *_ in CONFIGURABLE_TOOLSETS)


def _validate_toolset_keys(records: Dict[str, Capability]) -> FrozenSet[str]:
    """The D2<->D3 mutual check (GRV-009 E5 C-SEAM4) — ONE post-load pass.

    Two directions, two dispositions (per the locked design):

    * **record -> key (fail loud):** a record whose ``bindings.toolset_key`` is
      non-null but not a known CONFIGURABLE_TOOLSETS key is a binding to a
      phantom toolset — raise :class:`CapabilityLoadError` naming the record, the
      bad key, and the known set. (Hosted-MCP records carry ``toolset_key: null``
      and are skipped — they have no CONFIGURABLE_TOOLSETS key by design.)

    * **key -> record (reported):** a CONFIGURABLE_TOOLSETS key that no record
      yet governs is a migration-coverage gap (D4 verb backfill closes it), not a
      corruption — returned for the caller to report, never raised. Returning it
      (rather than logging here) keeps the pass pure and deterministically
      testable.
    """
    valid = _configurable_toolset_keys()
    governed: set[str] = set()
    for rid in sorted(records):
        tk = records[rid].bindings.toolset_key
        if tk is None:
            continue
        if tk not in valid:
            raise CapabilityLoadError(
                f"{rid}: bindings.toolset_key {tk!r} is not a known "
                f"CONFIGURABLE_TOOLSETS key — known: {sorted(valid)} "
                f"(defined in hermes_cli/tools_config.py::CONFIGURABLE_TOOLSETS)"
            )
        governed.add(tk)
    return valid - frozenset(governed)


def _report_uncovered_toolsets(uncovered: FrozenSet[str]) -> None:
    """Report (log once per distinct gap) the CONFIGURABLE_TOOLSETS keys that no
    capability record governs yet — the migration-coverage signal D4 drives to
    zero. Non-fatal by design (see :func:`_validate_toolset_keys`)."""
    if not uncovered or uncovered in _reported_uncovered:
        return
    _reported_uncovered.add(uncovered)
    logger.warning(
        "[grove.capability_registry] %d CONFIGURABLE_TOOLSETS key(s) have no "
        "governing capability record yet (D4 verb backfill pending): %s",
        len(uncovered),
        sorted(uncovered),
    )


def _load_records_from_dir(target: Path) -> Dict[str, Capability]:
    """Load + dry-run-validate every ``*.yaml`` in one dir (no collection-level
    validation, no empty-check). Raises :class:`CapabilityLoadError` on an
    unreadable / malformed / invalid / duplicate-id record."""
    if not target.is_dir():
        raise CapabilityLoadError(f"capabilities directory not found: {target}")

    records: Dict[str, Capability] = {}
    for path in sorted(target.glob("*.yaml")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CapabilityLoadError(f"{path.name}: unreadable ({exc})") from exc

        # Dry-run validation (Amendment A3): full construction triggers
        # Capability.validate(); a malformed YAML or invalid field raises here,
        # naming the field, and we wrap it with the filename.
        try:
            cap = Capability.from_yaml(text)
        except Exception as exc:
            raise CapabilityLoadError(f"{path.name}: {exc}") from exc

        if cap.id in records:
            raise CapabilityLoadError(
                f"{path.name}: duplicate capability id {cap.id!r} — already "
                f"loaded from another record file"
            )
        records[cap.id] = cap
    return records


def load_capabilities(directory: Optional[Path] = None) -> Dict[str, Capability]:
    """Load and dry-run-validate every ``*.yaml`` record.

    With no *directory*, loads the repo bundled records and overlays the
    machine-local ``<GROVE_HOME>/capabilities`` dir (GRV-009 E6b C1 installed
    records) on top. An explicit *directory* loads exactly that dir (no overlay)
    — the path tests and ``transition_record`` use for isolation.

    Returns an id -> :class:`Capability` mapping. Raises
    :class:`CapabilityLoadError` (fail loud) on any unreadable, malformed,
    invalid, or duplicate-id record. Never returns a partial registry.

    COLLISION RULE: an id present in BOTH the repo dir and the GROVE_HOME overlay
    is a write-once violation (the dedup guard should have prevented the mint) —
    it raises LOUDLY rather than silently shadowing. No last-glob-wins.
    """
    if directory is not None:
        records = _load_records_from_dir(Path(directory))
    else:
        records = _load_records_from_dir(default_capabilities_dir())
        overlay = grove_home_capabilities_dir()
        if overlay.is_dir():
            for cap_id, cap in _load_records_from_dir(overlay).items():
                if cap_id in records:
                    raise CapabilityLoadError(
                        f"capability id {cap_id!r} exists in BOTH the repo "
                        f"registry and the machine-local overlay ({overlay}) — "
                        f"write-once violation; the install dedup guard should "
                        f"have prevented this mint. Remove the overlay duplicate "
                        f"— no silent shadowing."
                    )
                records[cap_id] = cap

    if not records:
        raise CapabilityLoadError(
            f"no capability records found in {default_capabilities_dir()}"
            if directory is None else f"no capability records found in {directory}"
        )

    # fleet-hygiene-sweep — layer node-local STATE (model_binding, lifecycle
    # mutables, decision_log) over the DEFINITIONS. Skipped for an explicit
    # *directory* load (test/transition isolation, parity with the whole-file
    # overlay skip above). Absent state dir → no-op. Per-record R-B1 fallback.
    if directory is None:
        _compose_state_overlay(records)

    # A4 collection-level invariant — strict 1:1 tool ownership across records.
    _validate_binding_uniqueness(records)

    # D2<->D3 mutual check (C-SEAM4): record toolset_keys must be real (fail
    # loud); uncovered CONFIGURABLE_TOOLSETS keys are reported (non-fatal).
    _report_uncovered_toolsets(_validate_toolset_keys(records))

    # skill-adoption-v1 C1 — resolve the effective primacy map RESILIENTLY. Never
    # raises: a malformed claim (out-of-subset intent) or a collision (two ENABLED
    # records claiming one class) degrades the map and files an Andon, but the
    # gateway loads. The computed map is cached for primary_skill_for_intent().
    global _PRIMACY_MAP
    _PRIMACY_MAP, _primacy_violations = compute_primacy_map(records)
    _file_primacy_violations(_primacy_violations)
    # skill-adoption-v1 C2 — surface primacy claims that can never inject (no
    # body_hash anchor) so the dark state is never silent.
    _warn_primacy_dark(records)

    return records


# ── skill-adoption-v1 C1 — intent primacy (canonical skill per intent class) ──
#
# A record MAY claim primacy for an intent class it also declares in
# trigger.intents (the subset relation). At most one ENABLED record holds primacy
# for a class; the loader resolves the map RESILIENTLY (subset-violating claims
# dropped, collisions demote ALL claimants) and NEVER fails to boot over primacy
# config. The strict reject belongs to the write path (primacy_write_violations).

# The effective {intent_class -> primary skill slug} map from the most recent
# load_capabilities call. Empty until the first load. Consumers read it via
# primary_skill_for_intent(); no consumer is wired in this phase.
_PRIMACY_MAP: Dict[str, str] = {}


def compute_primacy_map(
    records: Dict[str, Capability],
) -> "tuple[Dict[str, str], List[Dict[str, Any]]]":
    """Resolve the effective primacy map from *records*. PURE — no side effects.

    Returns ``(primacy, violations)`` where ``primacy`` is ``{intent_class ->
    slug}`` for every class held by exactly one ENABLED record, and ``violations``
    is a list of Andon payload dicts (``reason`` = ``"subset_violation"`` or
    ``"collision"``) the caller files. Resolution rules:

      * Only records in EXECUTABLE_STATES may hold primacy (a quarantined /
        retired skill never governs an intent class).
      * A ``primary_intents`` entry NOT in the record's ``trigger.intents`` is an
        invalid subset claim: that intent is dropped and a ``subset_violation``
        recorded (the record keeps any valid claims).
      * A class claimed by two-or-more ENABLED records is a collision: ALL
        claimants are demoted (the class holds no primary this load) and a
        ``collision`` recorded. No tie-breakers.

    Deterministic: records are scanned in sorted-id order and violations emit in
    a stable order so the Andon stream and tests are reproducible."""
    claims: Dict[str, List[tuple]] = {}  # intent_class -> [(record_id, slug), …]
    violations: List[Dict[str, Any]] = []
    for cid, cap in sorted(records.items()):
        if cap.lifecycle.state not in EXECUTABLE_STATES:
            continue
        primary = cap.trigger.primary_intents
        if not primary:
            continue
        declared = set(cap.trigger.intents)
        slug = cid.rsplit(".", 1)[-1]
        for intent in primary:
            if intent not in declared:
                violations.append(
                    {
                        "reason": "subset_violation",
                        "intent_class": intent,
                        "record_id": cid,
                        "slug": slug,
                        "declared_intents": sorted(declared),
                    }
                )
                continue
            claims.setdefault(intent, []).append((cid, slug))
    primacy: Dict[str, str] = {}
    for intent in sorted(claims):
        holders = claims[intent]
        if len(holders) == 1:
            primacy[intent] = holders[0][1]
        else:
            violations.append(
                {
                    "reason": "collision",
                    "intent_class": intent,
                    "record_ids": sorted(h[0] for h in holders),
                    "slugs": sorted(h[1] for h in holders),
                }
            )
    return primacy, violations


def primary_skill_for_intent(intent_class: str) -> Optional[str]:
    """The slug of the skill holding primacy for *intent_class*, or ``None``.

    Reads the map cached by the most recent :func:`load_capabilities`. ``None``
    means no record holds primacy for the class — either none claimed it, or a
    collision demoted every claimant. Consumed by the C2 skill_payload provider
    and the C3 dispatcher recompose keying."""
    return _PRIMACY_MAP.get(intent_class)


def primacy_write_violations(
    records: Dict[str, Capability], candidate: Capability
) -> List[str]:
    """STRICT pre-persist gate for a sanctioned record writer. PURE.

    Returns a list of human-readable violation strings (empty ≡ writable) for
    *candidate* evaluated against the currently-loaded *records*:

      (i)  ``primary_intents`` not a subset of ``candidate.trigger.intents``;
      (ii) a primacy collision — *candidate* is ENABLED and claims a class an
           OTHER enabled record already holds.

    Unlike the loader (resilient demote-and-continue), this rejects: a writer
    calls it and refuses to persist when the list is non-empty. No consumer is
    wired this phase — parity with primary_skill_for_intent."""
    problems: List[str] = []
    declared = set(candidate.trigger.intents)
    out_of_subset = [i for i in candidate.trigger.primary_intents if i not in declared]
    if out_of_subset:
        problems.append(
            f"primary_intents {sorted(out_of_subset)} not a subset of "
            f"trigger.intents {sorted(declared)}"
        )
    if candidate.lifecycle.state in EXECUTABLE_STATES:
        held: Dict[str, List[str]] = {}
        for cid, cap in records.items():
            if cid == candidate.id or cap.lifecycle.state not in EXECUTABLE_STATES:
                continue
            cap_declared = set(cap.trigger.intents)
            for intent in cap.trigger.primary_intents:
                if intent in cap_declared:
                    held.setdefault(intent, []).append(cid)
        for intent in candidate.trigger.primary_intents:
            if intent in declared and intent in held:
                problems.append(
                    f"primacy collision on intent {intent!r}: already claimed by "
                    f"{sorted(held[intent])}"
                )
    return problems


def assert_primacy_writable(
    candidate: Capability,
    *,
    directory: Optional[Path] = None,
    target: Optional[Path] = None,
) -> None:
    """The sanctioned-persist-path strict primacy gate (skill-adoption-v1 C5c).

    Raises :class:`CapabilityLoadError` when persisting *candidate* would create an
    out-of-subset primacy claim or a collision against the currently ENABLED
    registry. A candidate that declares NO ``primary_intents`` short-circuits
    before any registry load — zero cost on the normal mint path. *directory* /
    *target* mirror the minter's dedup resolution so the check reads the SAME
    registry the write lands in.

    Wraps :func:`primacy_write_violations` (the pure checker); the writer calls
    this immediately before it persists, so a violating record never reaches
    disk."""
    if not candidate.trigger.primary_intents:
        return
    if directory is None:
        registry = load_capabilities()
    elif target is not None and any(Path(target).glob("*.yaml")):
        registry = load_capabilities(target)
    else:
        registry = {}
    problems = primacy_write_violations(registry, candidate)
    if problems:
        raise CapabilityLoadError(
            f"assert_primacy_writable: primacy write rejected for "
            f"{candidate.id!r} — {'; '.join(problems)}"
        )


def _file_primacy_violations(violations: "List[Dict[str, Any]]") -> None:
    """File one ``skill_primacy_collision`` Andon per primacy violation.

    Component-filer pattern (capability_binding_mutation precedent): no CLI
    session of its own, so events land under a ``cli-<utc-timestamp>`` sentinel.
    Error-log floor — the load has already succeeded; a filing failure must not
    misreport it. Empty list → no-op."""
    if not violations:
        return
    try:
        from datetime import datetime, timezone

        from grove.kaizen_ledger import KaizenLedger

        session_id = "cli-" + datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%S%fZ"
        )
        ledger = KaizenLedger(session_id=session_id)
        for v in violations:
            ledger.record("skill_primacy_collision", **v)
    except Exception as file_exc:  # noqa: BLE001 — filing leg, log floor stands
        logger.error(
            "[capability_registry] skill_primacy_collision filing failed "
            "(the load itself SUCCEEDED): %r",
            file_exc,
        )


# ── skill-adoption-v1 C2 — definition payload integrity (body_hash anchor) ────


def definition_payload_status(record: Capability) -> Optional[bool]:
    """The ``context.payload`` integrity verdict for *record* against its committed
    ``lifecycle.body_hash`` anchor. PURE.

    * ``None``  — the record carries no ``body_hash`` (NOT eligible for payload
      injection; the caller skips QUIETLY — absence is config state, not a
      violation).
    * ``True``  — ``body_hash`` present and matches ``sha256(context.payload)``.
    * ``False`` — present but mismatched (an integrity violation the caller
      Andons).

    Uses the canonical :func:`_body_hash` producer so the format matches every
    minted record's anchor (``sha256:<16 hex>``) — the committed value and this
    check hash the SAME bytes (the FULL ``context.payload``, pre-frontmatter-
    strip)."""
    body_hash = record.lifecycle.body_hash
    if not body_hash:
        return None
    payload = record.context.payload or ""
    return _body_hash(payload) == body_hash


def file_skill_payload_integrity_violation(
    slug: str, record_id: str, reason: str
) -> None:
    """File one ``skill_payload_integrity_violation`` Andon (component-filer
    pattern; ``cli-<utc>`` sentinel session). *reason* is ``"body_hash"`` or
    ``"promotion_pin"``. Error-log floor — a filing failure must not crash the
    compose path (the payload is already withheld)."""
    try:
        from datetime import datetime, timezone

        from grove.kaizen_ledger import KaizenLedger

        session_id = "cli-" + datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%S%fZ"
        )
        KaizenLedger(session_id=session_id).record(
            "skill_payload_integrity_violation",
            slug=slug,
            record_id=record_id,
            reason=reason,
        )
    except Exception as file_exc:  # noqa: BLE001 — filing leg, log floor stands
        logger.error(
            "[capability_registry] skill_payload_integrity_violation filing "
            "failed (the payload was withheld regardless): %r",
            file_exc,
        )


def _warn_primacy_dark(records: Dict[str, Capability]) -> None:
    """No-silent-degradation guard (skill-adoption-v1 C2): an ENABLED record that
    claims primacy for an intent it declares but carries NO ``lifecycle.body_hash``
    can never pass the C2 injection gate — its primacy is DARK. Warn once per load,
    naming the slug, so a primacy claim that can never inject stays visible."""
    for cid, cap in sorted(records.items()):
        if cap.lifecycle.state not in EXECUTABLE_STATES:
            continue
        declared = set(cap.trigger.intents)
        claims_something = any(i in declared for i in cap.trigger.primary_intents)
        if claims_something and not cap.lifecycle.body_hash:
            logger.warning(
                "[capability_registry] primacy dark: %s — claims primacy "
                "(primary_intents=%s) but carries no lifecycle.body_hash, so the "
                "skill_payload block can never inject. Commit a body_hash to "
                "enable payload injection.",
                cid.rsplit(".", 1)[-1],
                sorted(i for i in cap.trigger.primary_intents if i in declared),
            )


# ─────────────────────────────────────────────────────────────────────────────
# GRV-009 E6b C1 — the SOLE capability-record write path (write-once invariant).
#
# Until E6b the registry was load-only. ``transition_record`` is the only
# function that mutates a record on disk, and it does so under a per-record
# advisory lock with an atomic replace. It never blocks and never throws on
# contention: a non-blocking lock that is already held returns DEFERRED so the
# caller (the curator) retries on its next interval. Legality is pre-checked
# before ``Capability.transition()``, so a terminal/managed record returns
# SKIPPED rather than raising.
#
# Lock + atomic-write primitives mirror tools/skill_usage.py:66 (_usage_file_
# lock, fcntl.flock LOCK_EX) and :340 (save_usage, tempfile+fsync+os.replace).
# ─────────────────────────────────────────────────────────────────────────────

TRANSITION_APPLIED = "applied"      # transition legal + written
TRANSITION_DEFERRED = "deferred"    # lock contended — caller retries next interval
TRANSITION_SKIPPED = "skipped"      # illegal/terminal edge — pre-checked, no write


class TransitionResult(NamedTuple):
    status: str                # one of TRANSITION_APPLIED / _DEFERRED / _SKIPPED
    record: Optional[Any]      # the grove.capability.TransitionRecord when APPLIED


def _record_path_for_id(cap_id: str, target: Path) -> Optional[Path]:
    """Locate the ``*.yaml`` whose top-level ``id:`` equals *cap_id*.

    Parses only the id key (yaml.safe_load), not the full Capability, so the
    scan stays cheap for the common no-match files.
    """
    for path in sorted(target.glob("*.yaml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(doc, dict) and doc.get("id") == cap_id:
            return path
    return None


def _atomic_write_yaml(path: Path, text: str) -> None:
    """tempfile + fsync + os.replace — mirrors tools/skill_usage.save_usage:340."""
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=".cap_", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _transition_locked(
    def_path: Path,
    state_path: Path,
    to_state: LifecycleState,
    actor: str,
    reason: str,
    evidence: Optional[List[str]],
    lifecycle_fields: Dict[str, Any],
) -> TransitionResult:
    """Compose the record (def + state) under the held lock, transition, write
    the STATE snapshot (fleet-hygiene-sweep P2). The transition appends to the
    COMPOSED decision_log, so the snapshot carries seed + all prior entries +
    the new one forward losslessly (R-A9)."""
    cap = _compose_for_write(def_path, state_path)
    current = cap.lifecycle.state
    # Legality pre-check: a terminal/managed record (no legal exits) is SKIPPED,
    # never raised — the curator no-throw contract.
    if to_state not in LEGAL_TRANSITIONS.get(current, frozenset()):
        return TransitionResult(TRANSITION_SKIPPED, None)

    record = cap.transition(to_state, actor=actor, reason=reason, evidence=evidence)
    for key, value in lifecycle_fields.items():
        if not hasattr(cap.lifecycle, key):
            raise CapabilityLoadError(
                f"transition_record: unknown lifecycle field {key!r}"
            )
        setattr(cap.lifecycle, key, value)

    _write_state_snapshot(cap, state_path)
    return TransitionResult(TRANSITION_APPLIED, record)


def transition_record(
    cap_id: str,
    to_state: LifecycleState | str,
    *,
    actor: str,
    reason: str,
    evidence: Optional[List[str]] = None,
    directory: Optional[Path] = None,
    state_dir: Optional[Path] = None,
    **lifecycle_fields: Any,
) -> TransitionResult:
    """Mutate a capability record's lifecycle state — writing the STATE overlay
    (fleet-hygiene-sweep P2), never the bundled definition.

    Acquires a non-blocking per-record advisory lock, reads the COMPOSED record
    (definition + current state), validates + applies the transition, and writes
    the state snapshot atomically. ``lifecycle_fields`` (e.g. ``use_count=…``,
    ``last_used=…``, ``pinned=…``) are applied alongside the state change.

    Returns a :class:`TransitionResult`:
      * APPLIED  — transition legal and written (``.record`` is the TransitionRecord)
      * DEFERRED — the lock was contended; caller retries next interval (no write)
      * SKIPPED  — the edge is illegal/terminal; pre-checked, no write

    Raises :class:`CapabilityLoadError` only when no record carries *cap_id*.
    """
    if not isinstance(to_state, LifecycleState):
        to_state = LifecycleState(to_state)

    # Resolve the record's file. With no explicit directory, search BOTH the repo
    # bundled dir AND the machine-local GROVE_HOME overlay — agent records
    # (proposed/active, installed/managed) live in the overlay, bundled records
    # in the repo. The write lands in whichever dir holds the record.
    if directory is not None:
        search_dirs = [Path(directory)]
    else:
        search_dirs = [default_capabilities_dir(), grove_home_capabilities_dir()]
    path = None
    for d in search_dirs:
        if d.is_dir():
            path = _record_path_for_id(cap_id, d)
            if path is not None:
                break
    if path is None:
        raise CapabilityLoadError(
            f"transition_record: no capability record with id {cap_id!r} in "
            f"{[str(d) for d in search_dirs]}"
        )

    state_path = _state_path_for_id(cap_id, state_dir or capability_state_dir())

    if fcntl is None:  # pragma: no cover - non-POSIX best-effort
        return _transition_locked(
            path, state_path, to_state, actor, reason, evidence, lifecycle_fields
        )

    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_suffix(".yaml.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(lock_path, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            # Contended by an active turn writing the same record — defer, never
            # block, never throw.
            return TransitionResult(TRANSITION_DEFERRED, None)
        try:
            return _transition_locked(
                path, state_path, to_state, actor, reason, evidence,
                lifecycle_fields,
            )
        finally:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    finally:
        fd.close()


def update_lifecycle_fields(
    cap_id: str,
    *,
    directory: Optional[Path] = None,
    state_dir: Optional[Path] = None,
    **fields: Any,
) -> bool:
    """Write NON-state lifecycle fields (e.g. ``pinned``) to a record's STATE
    overlay (fleet-hygiene-sweep P2), never the bundled definition — the
    registry write path for the CLI pin toggle and any telemetry-on-record.

    Reads the COMPOSED record, applies the fields, writes the state snapshot
    under the same per-record advisory lock. Returns True when written, False
    on lock contention (caller may retry). Raises :class:`CapabilityLoadError`
    when no record carries *cap_id* or an unknown lifecycle field is given.
    """
    if directory is not None:
        search_dirs = [Path(directory)]
    else:
        search_dirs = [default_capabilities_dir(), grove_home_capabilities_dir()]
    path = None
    for d in search_dirs:
        if d.is_dir():
            path = _record_path_for_id(cap_id, d)
            if path is not None:
                break
    if path is None:
        raise CapabilityLoadError(
            f"update_lifecycle_fields: no capability record with id {cap_id!r} in "
            f"{[str(d) for d in search_dirs]}"
        )

    state_path = _state_path_for_id(cap_id, state_dir or capability_state_dir())

    def _apply() -> bool:
        cap = _compose_for_write(path, state_path)
        for key, value in fields.items():
            if not hasattr(cap.lifecycle, key):
                raise CapabilityLoadError(
                    f"update_lifecycle_fields: unknown lifecycle field {key!r}"
                )
            setattr(cap.lifecycle, key, value)
        _write_state_snapshot(cap, state_path)
        return True

    if fcntl is None:  # pragma: no cover - non-POSIX best-effort
        return _apply()

    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_suffix(".yaml.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(lock_path, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            return False  # contended — caller may retry
        try:
            return _apply()
        finally:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    finally:
        fd.close()


# ─────────────────────────────────────────────────────────────────────────────
# binding-governance-surfaces-v1 — CapabilityBindingWriter.
#
# ``set_model_binding`` is the ONE sanctioned writer for the model_binding
# field on kind=skill records (GATE-A P1: no writer existed; transition_record
# and update_lifecycle_fields are lifecycle-scoped by construction). Sibling of
# ``transition_record``: same per-record advisory lock, same tempfile+fsync+
# os.replace atomic write. Differences, deliberate:
#   * resolution is by skill NAME through resolve_skill_record() — the
#     canonical slug-tail resolver — and is RE-VERIFIED inside the held lock
#     (a registry that shifted between resolve and lock refuses, never writes
#     the wrong record);
#   * lock contention raises loud (BindingWriteError) instead of deferring —
#     the caller is an operator action or a proposal apply, not the curator's
#     retry loop; a silent no-op here would be a fail-silent;
#   * the writer files its OWN ledger audit event (capability_binding_mutation)
#     on success — adjudication R5: do not replicate the tier-swap audit
#     weakness (backup + logger only).
# No hot-reload step: the registry is read per-call, so the next
# load_capabilities() sees the new binding.
# ─────────────────────────────────────────────────────────────────────────────


class BindingWriteResult(NamedTuple):
    path: Path
    record_id: str
    previous_binding: Optional[Dict[str, Any]]
    new_binding: Optional[Dict[str, Any]]


_BINDING_KEYS = frozenset({"type", "tier", "model"})


def _binding_to_dict(mb: Any) -> Optional[Dict[str, Any]]:
    """Present-key-only dict form of a ModelBinding (mirrors Capability.to_dict)."""
    if mb is None:
        return None
    d: Dict[str, Any] = {"type": mb.type}
    if mb.tier is not None:
        d["tier"] = mb.tier
    if mb.model is not None:
        d["model"] = mb.model
    return d


def _file_binding_mutation_event(
    *,
    skill: str,
    record_id: str,
    previous_binding: Optional[Dict[str, Any]],
    new_binding: Optional[Dict[str, Any]],
    surface: str,
    proposal_id: Optional[str],
) -> None:
    """File the writer's own audit event (R5 — the writer audits itself).

    Component-filer pattern (skill_binding refusal precedent): no CLI session
    of its own, so the event lands under a ``cli-<utc-timestamp>`` sentinel
    session. Error-log floor: the mutation has already landed atomically when
    this runs, so a filing failure must not misreport the write as failed —
    it logs at ERROR and stands.
    """
    try:
        from datetime import datetime, timezone

        from grove.kaizen_ledger import KaizenLedger

        session_id = "cli-" + datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%S%fZ"
        )
        KaizenLedger(session_id=session_id).record(
            "capability_binding_mutation",
            skill=skill,
            record_id=record_id,
            previous_binding=previous_binding,
            new_binding=new_binding,
            surface=surface,
            proposal_id=proposal_id,
        )
    except Exception as file_exc:  # noqa: BLE001 — filing leg, log floor stands
        logger.error(
            "[capability_registry] capability_binding_mutation filing failed "
            "(mutation itself SUCCEEDED): %r",
            file_exc,
        )


def set_model_binding(
    name: str,
    binding: Optional[Dict[str, Any]],
    *,
    surface: str,
    proposal_id: Optional[str] = None,
) -> BindingWriteResult:
    """Set or clear ``model_binding`` on the kind=skill record governing *name*.

    The sole sanctioned model_binding writer. ``binding=None`` clears the
    field; a dict (``{"type": ..., "tier": ..., "model": ...}``) sets it.
    *surface* names the mutation origin (``portal`` / ``proposal_apply`` / …)
    and lands in the audit event verbatim; *proposal_id* joins the event to a
    Kaizen proposal when the write is a proposal apply.

    Sequence: resolve → lock → re-verify resolution inside the lock → backup
    ``.bak`` → mutate → validate (``cap.validate()`` + catalog membership for
    ``type=model``) → atomic replace → ledger event. Any failure after backup
    restores the original bytes and re-raises.

    Raises :class:`BindingWriteError` on refusal (unresolved/ambiguous name,
    inside-lock resolution mismatch, lock contention, malformed binding dict,
    validation or catalog-membership failure).
    """
    if binding is not None:
        if not isinstance(binding, dict) or not binding:
            raise BindingWriteError(
                f"set_model_binding: binding must be None or a non-empty dict; "
                f"got {binding!r}"
            )
        unknown = set(binding) - _BINDING_KEYS
        if unknown:
            raise BindingWriteError(
                f"set_model_binding: unknown binding keys {sorted(unknown)!r}; "
                f"allowed: {sorted(_BINDING_KEYS)}"
            )

    res = resolve_skill_record(name)
    if res.status == "ambiguous":
        raise BindingWriteError(
            f"set_model_binding: skill name {name!r} is ambiguous — its slug "
            f"matches multiple capability records: {', '.join(res.matches)}. "
            f"Refusing to write."
        )
    if res.status != "resolved" or res.record_id is None:
        raise BindingWriteError(
            f"set_model_binding: no capability record governs skill name "
            f"{name!r} — a binding without a record is dead config. Refusing "
            f"to write."
        )
    record_id = res.record_id

    search_dirs = [default_capabilities_dir(), grove_home_capabilities_dir()]
    path = None
    for d in search_dirs:
        if d.is_dir():
            path = _record_path_for_id(record_id, d)
            if path is not None:
                break
    if path is None:
        raise BindingWriteError(
            f"set_model_binding: resolved record {record_id!r} has no backing "
            f"file in {[str(d) for d in search_dirs]}"
        )

    # fleet-hygiene-sweep P2 — the write TARGET is the state overlay, not the
    # bundled definition (`path`). The definition is read-only to this writer;
    # its role now is only to compose the current effective binding for the
    # audit event's `previous`.
    state_path = _state_path_for_id(record_id, capability_state_dir())

    def _locked_write() -> BindingWriteResult:
        # Re-verify INSIDE the held lock: the registry may have shifted between
        # the pre-lock resolve and lock acquisition (record renamed, collision
        # introduced). Same record, still unique, or refuse.
        res2 = resolve_skill_record(name)
        if res2.status != "resolved" or res2.record_id != record_id:
            raise BindingWriteError(
                f"set_model_binding: resolution changed under the lock — "
                f"pre-lock {record_id!r}, in-lock status={res2.status!r} "
                f"record_id={res2.record_id!r}. Refusing to write."
            )

        from grove.capability import ModelBinding

        # R-A9 — read the COMPOSED record (definition + current state) so the
        # snapshot carries lifecycle + decision_log forward losslessly and
        # `previous` reflects the effective binding, not just the definition.
        cap = _compose_for_write(path, state_path)
        previous = _binding_to_dict(cap.model_binding)
        new_mb = (
            None
            if binding is None
            else ModelBinding(
                type=binding.get("type"),
                tier=binding.get("tier"),
                model=binding.get("model"),
            )
        )
        cap.model_binding = new_mb
        # Mutation is post-construction — validate explicitly (kind=skill
        # guard, type/tier/model shape).
        try:
            cap.validate()
        except ValueError as exc:
            raise BindingWriteError(
                f"set_model_binding: proposed binding failed record "
                f"validation: {exc}"
            ) from exc

        if new_mb is not None and new_mb.type == "model":
            from grove.config.model_catalog import load_catalog

            slugs = {m["slug"] for m in load_catalog()}
            if new_mb.model not in slugs:
                raise BindingWriteError(
                    f"set_model_binding: model {new_mb.model!r} is not in "
                    f"the model catalog — a pin to an off-catalog slug is "
                    f"dead config. Refusing to write."
                )

        prior = _write_state_snapshot(cap, state_path)  # .bak + atomic
        # Verify the write composes back cleanly; roll back on any corruption.
        try:
            _compose_for_write(path, state_path)
        except BaseException:
            if prior:
                state_path.write_bytes(prior)
            else:
                state_path.unlink(missing_ok=True)
            raise

        new = _binding_to_dict(new_mb)
        _file_binding_mutation_event(
            skill=name,
            record_id=record_id,
            previous_binding=previous,
            new_binding=new,
            surface=surface,
            proposal_id=proposal_id,
        )
        logger.info(
            "[capability_registry] model_binding written to state overlay: "
            "%s %s -> %s (surface=%s)",
            record_id,
            previous,
            new,
            surface,
        )
        return BindingWriteResult(state_path, record_id, previous, new)

    if fcntl is None:  # pragma: no cover - non-POSIX best-effort
        return _locked_write()

    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_suffix(".yaml.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(lock_path, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            raise BindingWriteError(
                f"set_model_binding: record {record_id!r} is locked by another "
                f"writer — retry when the contending write completes"
            )
        try:
            return _locked_write()
        finally:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    finally:
        fd.close()


# ─────────────────────────────────────────────────────────────────────────────
# GRV-009 E6b C1 — static-registration hook: mint read-only installed/managed
# records at the install perimeter. INFRASTRUCTURE, not faucet — these records
# inline the installed SKILL.md and govern no tools (write-once, no mutation
# surface). They are curator-exempt (MANAGED is terminal). Dedup-guarded: a mint
# is a no-op when the registry already holds the skill's id (so the bundled 92
# provenance:migrated records and re-installs are never overwritten).
#
# Zone (operator lock): inherit RED/YELLOW from the SKILL.md frontmatter; a
# self-declared GREEN, a silent record, or an unparseable zone all fall back to
# YELLOW. Never GREEN, never default-RED.
# ─────────────────────────────────────────────────────────────────────────────


def _slug(text: str) -> str:
    """Lowercase, non-alphanumeric runs -> single hyphen, trimmed."""
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def _skill_name_slug(name: str) -> str:
    """The canonical slug-tail of a skill name (skill-invocation-path-integrity-v1).

    THE one slug computation for every skill-name resolver: take the last
    ``/``-segment (category-qualified ``fleet/forge-jobsearch``), then the last
    ``:``-segment, then slugify. Flat, category-qualified, and colon-qualified
    shapes all resolve to the same slug.
    """
    return _slug(name.rsplit("/", 1)[-1].rsplit(":", 1)[-1])


def _skill_matches(name: str) -> List[tuple]:
    """Every kind=skill record whose id trailing segment matches *name*'s slug.

    The shared scan behind :func:`resolve_skill_record` and the legacy
    single-result helpers — one slug computation, one match rule, no forks.
    Returns ``(record_id, Capability)`` pairs sorted by id (deterministic).
    An unloadable registry returns ``[]`` (parity with the legacy helpers'
    ``None``; the invoke-path guard treats no-record as legacy-allow).
    """
    from grove.capability import CapabilityKind

    slug = _skill_name_slug(name)
    if not slug:
        return []
    try:
        caps = load_capabilities()
    except CapabilityLoadError:
        return []
    return sorted(
        (
            (cid, cap)
            for cid, cap in caps.items()
            if cap.kind is CapabilityKind.SKILL and cid.rsplit(".", 1)[-1] == slug
        ),
        key=lambda pair: pair[0],
    )


class SkillResolution(NamedTuple):
    """Result of :func:`resolve_skill_record` (skill-invocation-path-integrity-v1).

    ``status`` is one of ``"resolved"`` (exactly one record: ``record`` +
    ``record_id`` set), ``"none"`` (no record governs the name — the legacy-
    allow shape), or ``"ambiguous"`` (the slug matches >1 record id trailing
    segment; ``matches`` carries every colliding id, sorted).
    """

    status: str
    record: Optional[Capability]
    record_id: Optional[str]
    matches: tuple


def resolve_skill_record(name: str) -> SkillResolution:
    """Canonically resolve a skill name to its governing capability record.

    Slug-tail resolution (the one canonical resolver): flat, category-
    qualified (``fleet/<name>``), and colon-qualified shapes all key on the
    same slug. Exactly one match → ``resolved``; zero → ``none``; more than
    one → ``ambiguous`` with the colliding ids (the invoke path refuses;
    the boot scan logs).
    """
    matches = _skill_matches(name)
    if not matches:
        return SkillResolution("none", None, None, ())
    if len(matches) > 1:
        return SkillResolution(
            "ambiguous", None, None, tuple(cid for cid, _ in matches)
        )
    cid, cap = matches[0]
    return SkillResolution("resolved", cap, cid, (cid,))


def scan_skill_slug_collisions() -> Dict[str, List[str]]:
    """Boot-time slug-collision scan (skill-invocation-path-integrity-v1 P1).

    Groups every kind=skill record id by its trailing segment and logs each
    group with more than one member at WARNING, naming every colliding id.
    LOG-ONLY: never raises, never halts startup — a collision degrades to
    per-invoke ambiguity refusal, not a boot failure. Returns the collision
    map (slug -> sorted ids) for tests and callers that want the census.
    """
    from grove.capability import CapabilityKind

    try:
        caps = load_capabilities()
    except Exception as exc:  # noqa: BLE001 — log-only by contract
        logger.warning(
            "[capability_registry] skill slug collision scan skipped — "
            "registry unloadable: %r",
            exc,
        )
        return {}
    groups: Dict[str, List[str]] = {}
    for cid, cap in caps.items():
        if cap.kind is not CapabilityKind.SKILL:
            continue
        groups.setdefault(cid.rsplit(".", 1)[-1], []).append(cid)
    collisions = {
        slug: sorted(ids) for slug, ids in groups.items() if len(ids) > 1
    }
    for slug, ids in sorted(collisions.items()):
        logger.warning(
            "[capability_registry] skill slug collision: %r -> %s "
            "(invoke_skill refuses ambiguous resolutions for this slug)",
            slug,
            ids,
        )
    return collisions


def skill_record_id_for_name(name: str) -> Optional[str]:
    """The kind=skill record id whose name-slug matches *name*, or None.

    GRV-009 E6b C2 — the faucet (edit/patch/delete) and sovereignty
    (promote/reject/revoke) use this to find the record governing an on-disk
    skill, matching the trailing id segment (the slug). Returns None when no
    record governs the skill (external skills, pre-C2 legacy .andon proposals).
    Delegates to the shared scan; on a slug collision returns the first id in
    sorted order (deterministic; the legacy scan's first-match was load-order).
    """
    matches = _skill_matches(name)
    return matches[0][0] if matches else None


def skill_record_for_name(name: str):
    """The kind=skill :class:`Capability` whose name-slug matches *name*, or None
    (GRV-009 E6b C2-bridge — the CLI reads state/pinned from the record).
    Delegates to the shared scan; see :func:`skill_record_id_for_name` for the
    collision tiebreak."""
    matches = _skill_matches(name)
    return matches[0][1] if matches else None


def set_skill_pinned(name: str, pinned: bool) -> bool:
    """Set ``lifecycle.pinned`` on the record governing *name* (GRV-009 E6b
    C2-bridge — the CLI pin toggle, record-backed). Returns True when written,
    False when no record governs the skill (caller informs the operator)."""
    cap_id = skill_record_id_for_name(name)
    if cap_id is None:
        return False
    return update_lifecycle_fields(cap_id, pinned=bool(pinned))


def _frontmatter_value(payload: str, key: str) -> Optional[str]:
    """A single string value from a SKILL.md frontmatter block, or None."""
    if not payload.startswith("---"):
        return None
    parts = payload.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        front = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(front, dict):
        return None
    val = front.get(key)
    return str(val).strip() if val else None


def _frontmatter_zone(payload: str) -> Optional[str]:
    """The lowercased ``zone:`` from a SKILL.md frontmatter block, or None."""
    if not payload.startswith("---"):
        return None
    parts = payload.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        front = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(front, dict):
        return None
    zone = front.get("zone")
    return str(zone).strip().lower() if zone else None


def _resolve_minted_zone(payload: str):
    """Inherit RED/YELLOW from frontmatter; green/silent/invalid -> YELLOW."""
    from grove.capability import Zone

    declared = _frontmatter_zone(payload)
    if declared == "red":
        return Zone.RED
    if declared == "yellow":
        return Zone.YELLOW
    # Self-declared green, silent, or unparseable -> default-deny-but-usable.
    return Zone.YELLOW


def _body_hash(content: str) -> str:
    """sha256 body hash for ``lifecycle.body_hash``. Mirrors
    ``grove.sovereignty._sha256_short`` exactly so a future wake-match compares
    like-for-like (GRV-009 E6b C2 — populate only; reactivation DEFERRED)."""
    import hashlib

    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _mint_skill_record(
    name: str,
    category: str,
    payload: str,
    *,
    provenance,
    state,
    filename_tag: str,
    use_count: int = 0,
    directory: Optional[Path] = None,
    existing_ids: Optional[FrozenSet[str]] = None,
) -> Optional[Path]:
    """Shared minter: build + dedup-guard + atomically write a kind=skill record.

    Returns the written path, or ``None`` (dedup hit or unusable input). The
    record mints to the machine-local ``<GROVE_HOME>/capabilities`` overlay
    (ruling A); an explicit *directory* (tests) wins. ``body_hash`` is always
    populated. ``filename_tag`` marks provenance in the filename
    (``skill__<tag>__<cat>__<name>.yaml``); the record *id* is unchanged.
    """
    from grove.capability import (
        Capability,
        CapabilityKind,
        CircuitBreaker,
        Context,
        Disclosure,
        DockComposition,
        Failure,
        Lifecycle,
        SkillPresentation,
        Telemetry,
        TierRule,
        TierValidation,
        Trigger,
        TriggerDisclosure,
    )

    name_slug = _slug(name)
    if not name_slug or not (payload or "").strip():
        return None
    # E6a id convention: a categorized skill is skill.<category>.<name>; a
    # top-level skill (no category) is skill.<name>.<name>. Matching this keeps
    # the dedup guard aligned with the migrated 92 (e.g. skill.dogfood.dogfood).
    cat_slug = _slug(category) or name_slug
    cap_id = f"skill.{cat_slug}.{name_slug}"

    target = Path(directory) if directory is not None else grove_home_capabilities_dir()
    target.mkdir(parents=True, exist_ok=True)

    # Dedup against the registry the loader reads. A pre-loaded id set (batch
    # callers) avoids re-loading per skill; otherwise load once here. Fail loud
    # on an invalid registry (a real defect).
    if existing_ids is not None:
        if cap_id in existing_ids:
            return None
    elif directory is None:
        # Full registry = repo bundled + machine-local overlay.
        if cap_id in load_capabilities():
            return None
    elif any(target.glob("*.yaml")) and cap_id in load_capabilities(target):
        return None

    # fleet-hygiene-sweep R-B4 — mint gate against the STATE overlay: a new id
    # must not already carry orphaned state (a prior record's residue at the
    # same id). State without a definition is a ghost the loader ignores, but
    # minting a definition ONTO it would silently resurrect stale state — refuse.
    if directory is None:
        state_path = _state_path_for_id(cap_id, capability_state_dir())
        if state_path.exists():
            raise CapabilityLoadError(
                f"_mint_skill_record: id {cap_id!r} has an existing state file "
                f"({state_path}) but no definition — orphaned state must be "
                f"removed before minting this id (R-B4). Refusing to mint onto "
                f"stale state."
            )

    cap = Capability(
        id=cap_id,
        kind=CapabilityKind.SKILL,
        trigger=Trigger(always=True, disclosure=TriggerDisclosure.PROACTIVE),
        tier_rule=TierRule(
            eligible=[1, 2, 3],
            preferred=1,
            validation=TierValidation(confidence_threshold=0.95, shadow_window=20),
        ),
        zone=_resolve_minted_zone(payload),
        telemetry=Telemetry(feed="intent_feed"),
        context=Context(
            disclosure=Disclosure.PULL,
            payload=payload,
            dock_composition=DockComposition.NONE,
        ),
        lifecycle=Lifecycle(
            state=state,
            provenance=provenance,
            body_hash=_body_hash(payload),
            use_count=use_count,
        ),
        failure=Failure(circuit_breaker=CircuitBreaker(threshold=3, window_seconds=300)),
        skill=SkillPresentation(category=cat_slug),
    )

    # skill-adoption-v1 C5c — strict primacy pre-persist gate (Phase-1 condition:
    # primacy_write_violations must not strand). Inert-by-construction for today's
    # minters (a minted record carries Trigger(always=True) with NO primary_intents),
    # so the guard only loads the registry + checks when a candidate ACTUALLY claims
    # primacy — zero overhead on the normal mint path, LIVE for any future writer
    # that persists a primary claim.
    assert_primacy_writable(cap, directory=directory, target=target)

    path = target / f"skill__{filename_tag}__{cat_slug}__{name_slug}.yaml"
    _atomic_write_yaml(path, cap.to_yaml())
    return path


def register_installed_skill(
    name: str,
    category: str,
    payload: str,
    *,
    directory: Optional[Path] = None,
    existing_ids: Optional[FrozenSet[str]] = None,
) -> Optional[Path]:
    """Mint a read-only ``provenance:installed`` / ``lifecycle:managed`` skill
    record for a freshly installed skill — IF none exists (dedup guard).

    Returns the written record path, or ``None`` when the skill already has a
    record (idempotent) or the inputs are unusable (empty name/payload).

    ``existing_ids`` lets a batch caller (the profile-clone tree walk, the sync
    loop) pre-load the registry's ids ONCE and pass them in, avoiding an
    O(skills x registry) reload per skill.
    """
    from grove.capability import LifecycleState, Provenance

    written = _mint_skill_record(
        name, category, payload,
        provenance=Provenance.INSTALLED,
        state=LifecycleState.MANAGED,
        filename_tag="installed",
        directory=directory,
        existing_ids=existing_ids,
    )
    # GRV-010 C2b — minter provenance. This minter is operator-only (CLI /
    # boot-sync / profile-clone; no agent-loop path — GATE-A thread 3 ruling).
    # Record a stateless sovereignty_decision so every minted record has an
    # audit trail. ``log_sovereignty_decision`` writes to the telemetry logger
    # with no session/turn context, so it is safe from a bare CLI execution.
    if written is not None:
        _log_minter_provenance(
            action="skill_record_minted", skill_name=name,
            dest_path=str(written),
        )
    return written


def _log_minter_provenance(
    *, action: str, skill_name: str, dest_path: Optional[str] = None,
    scan_verdict: str = "n/a", reason: Optional[str] = None,
) -> None:
    """Emit an operator/CLI-attributed provenance record for a minter.

    Bare-CLI-context safe: ``log_sovereignty_decision`` is a stateless telemetry
    write (no SessionDB, no active turn). Failures never block the mint.
    """
    try:
        from grove.telemetry import log_sovereignty_decision
        log_sovereignty_decision(
            action=action,
            skill_name=skill_name,
            operator="operator/CLI",
            scan_verdict=scan_verdict,
            dest_path=dest_path,
            reason=reason,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[capability_registry] minter provenance log failed "
            "(non-fatal): %r", exc,
        )


# GRV-010 C2b — ``ingest_pre_faucet_skill`` deleted: it was a dead, un-audited
# minter (zero production callers; it minted an ACTIVE/executable record outside
# the proposed-quarantine gate). The pre-faucet ingest it served is long past.


def register_proposed_skill(
    name: str,
    category: str,
    payload: str,
    *,
    directory: Optional[Path] = None,
    existing_ids: Optional[FrozenSet[str]] = None,
) -> Optional[Path]:
    """Mint a ``provenance:agent_proposed`` / ``lifecycle:proposed`` record for an
    agent-generated skill — the C2 faucet (dedup-guarded).

    ``proposed`` is the SOLE authoritative quarantine state (GRV-009 E6b C2
    .andon fork ruling a): the record is **non-executable** until promoted — the
    dispatch checkpoint refuses ``state:proposed`` even though the record loads
    and the body is readable in ``.andon/`` for operator review. ``body_hash`` is
    populated for future wake-match (reactivation DEFERRED).
    """
    from grove.capability import LifecycleState, Provenance

    return _mint_skill_record(
        name, category, payload,
        provenance=Provenance.AGENT_PROPOSED,
        state=LifecycleState.PROPOSED,
        filename_tag="proposed",
        directory=directory,
        existing_ids=existing_ids,
    )


def register_skills_in_tree(
    skills_root: Path,
    *,
    directory: Optional[Path] = None,
) -> List[Path]:
    """Mint installed/managed records for every ``<cat>/<name>/SKILL.md`` under
    *skills_root* (dedup-guarded). Used by the profile-clone perimeter, which
    copies whole skill trees. Returns the list of newly written record paths.
    """
    minted: List[Path] = []
    if not skills_root.is_dir():
        return minted

    # Load the registry's ids ONCE for the whole tree (not per skill) — a
    # profile clone can carry the full skill set; a per-skill reload would make
    # profile creation time out. directory=None -> the full merged registry
    # (repo + GROVE_HOME overlay); an explicit directory -> that dir only.
    if directory is None:
        existing_ids: set = set(load_capabilities().keys())
    else:
        target = Path(directory)
        existing_ids = (
            set(load_capabilities(target).keys()) if any(target.glob("*.yaml")) else set()
        )

    for skill_md in sorted(skills_root.rglob("SKILL.md")):
        rel = skill_md.parent.relative_to(skills_root)
        # Skip hidden infrastructure dirs (.archive/.andon/.hub/.curator_backups
        # etc.) — they are not installable skills (E6a excluded them too).
        if any(part.startswith(".") for part in rel.parts):
            continue
        # <category>/<name>/SKILL.md -> category, name; bare <name>/ -> "".
        category = rel.parent.as_posix() if rel.parent.as_posix() != "." else ""
        name = rel.name
        try:
            payload = skill_md.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            written = register_installed_skill(
                name, category, payload, directory=directory,
                existing_ids=frozenset(existing_ids),
            )
        except Exception:
            # FLAG 2 (operator lock): a mint failure names the skill_id and the
            # absolute skill-body path so the operator has exact reconcile
            # coordinates — never a bare traceback.
            cat_slug = _slug(category) or _slug(name)
            logger.warning(
                "capability-record mint FAILED skill_id=skill.%s.%s body=%s "
                "— record NOT minted; reconcile manually",
                cat_slug, _slug(name), skill_md.resolve(), exc_info=True,
            )
            continue
        if written is not None:
            minted.append(written)
            # Keep the in-memory id set current so a duplicate name later in the
            # same tree dedups without a reload.
            existing_ids.add(f"skill.{_slug(category) or _slug(name)}.{_slug(name)}")
    return minted
