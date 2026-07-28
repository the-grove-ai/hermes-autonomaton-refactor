"""Instance-file health classification, malformed→loud graduation, and the F2
repair entry point (instance-cold-start-parity-v1 P2; GATE-B F4 + F2).

ONE classifier, driven by the cold-start contract, over the GRADUATED SET —
exactly three files this sprint (config.yaml, grants.yaml, write_workspaces.yaml),
marked ``graduated: true`` in ``config/cold_start.yaml``. Absent ≠ malformed in
both directions.

Categories (GATE-B F4):
  * ABSENT      — the file is not there. The caller keeps its own absence_class
                  (config → DEFAULT_CONFIG, grants → silent [], write_workspaces
                  → loud-empty). NOT a fault.
  * OK          — parses AND the top-level shape matches what the live reader
                  consumes. Expected top-level type per reader (all ``mapping``):
                    - config.yaml       hermes_cli/config.py load_config —
                                        ``isinstance(user_config, dict)``
                    - grants.yaml       grove/grants.py:60 — ``data.get("grants")``
                    - write_workspaces  grove/utils/fs_utils.py:659 —
                                        ``data.get("write_workspaces")``
                  Minimal shape check only; deep/range validation is FILED.
  * MALFORMED   — 0-byte, parses-to-None, parse error, or wrong top-level shape.
                  A CONTENT fault.
  * UNREADABLE  — permission/IO fault. An ENVIRONMENT fault — distinct category,
                  distinct message, distinct root cause from MALFORMED.

Every governed error names the file, the category, and the repair invocation.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, List, Optional

import yaml

logger = logging.getLogger(__name__)

# The F2 repair entry point — a boot-independent CLI subcommand. Named in every
# governed error so the operator always knows the recovery invocation.
REPAIR_INVOCATION = "hermes repair-instance"

# Top-level shape name → the Python type the live reader requires.
_SHAPE_TYPES = {"mapping": dict, "sequence": list}


class FileHealth(str, Enum):
    ABSENT = "absent"
    OK = "ok"
    MALFORMED = "malformed"
    UNREADABLE = "unreadable"


class InstanceFileError(RuntimeError):
    """A graduated instance file is MALFORMED or UNREADABLE — a governed loud
    fault (F4). Never silently defaulted."""


@dataclass
class FileClassification:
    name: str          # instance-relative name, e.g. "config.yaml"
    path: Path         # absolute path classified
    health: FileHealth
    evidence: Optional[str]   # the diagnostic line for MALFORMED / UNREADABLE
    data: Any                 # parsed content when OK; None otherwise

    @property
    def is_fault(self) -> bool:
        return self.health in (FileHealth.MALFORMED, FileHealth.UNREADABLE)


# ── the classifier ───────────────────────────────────────────────────────────


def classify_instance_file(path: os.PathLike, expected_shape: str, *, name: str) -> FileClassification:
    """Classify one instance file into ABSENT / OK / MALFORMED / UNREADABLE.

    Parses for classification ONLY (contained — every parse fault is caught and
    turned into a category, never propagated). Absence is detected with
    ``lexists`` so a dangling symlink is a fault (UNREADABLE), NOT a clean
    absence.
    """
    p = Path(path)
    py_type = _SHAPE_TYPES.get(expected_shape, dict)

    if not os.path.lexists(p):
        return FileClassification(name, p, FileHealth.ABSENT, None, None)

    try:
        raw = p.read_bytes()
    except PermissionError as exc:
        return FileClassification(name, p, FileHealth.UNREADABLE, f"permission denied: {exc}", None)
    except OSError as exc:
        # broken symlink, IsADirectory, or transient IO — an environment fault.
        return FileClassification(name, p, FileHealth.UNREADABLE, f"unreadable ({exc.__class__.__name__}): {exc}", None)

    if len(raw) == 0:
        return FileClassification(name, p, FileHealth.MALFORMED, "0-byte file (empty)", None)

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark is not None else ""
        return FileClassification(name, p, FileHealth.MALFORMED, f"YAML parse error{where}: {exc}", None)

    if data is None:
        return FileClassification(
            name, p, FileHealth.MALFORMED,
            "parses to None (comments-only or empty document — no mapping)", None,
        )
    if not isinstance(data, py_type):
        return FileClassification(
            name, p, FileHealth.MALFORMED,
            f"top-level is {type(data).__name__}, expected {expected_shape}", None,
        )
    return FileClassification(name, p, FileHealth.OK, None, data)


def _single_file_message(c: FileClassification) -> str:
    return (
        f"instance file {c.name} is {c.health.value.upper()} — {c.evidence}. "
        f"(at {c.path}) The Autonomaton refuses to run on a corrupt instance "
        f"file. Run `{REPAIR_INVOCATION}` to quarantine and reseed it."
    )


def classify_or_raise_present(path: os.PathLike, expected_shape: str, *, name: str) -> FileClassification:
    """Reader helper (D2): the caller has already done its OWN absence check and
    believes the file is present. Classify it; raise :class:`InstanceFileError`
    on MALFORMED/UNREADABLE (the graduation), else return the classification. A
    rare TOCTOU ABSENT is returned with ``data=None`` — the caller's
    ``c.data or {}`` idiom degrades to its absence default."""
    c = classify_instance_file(path, expected_shape, name=name)
    if c.is_fault:
        raise InstanceFileError(_single_file_message(c))
    return c


# ── contract-driven graduated set ────────────────────────────────────────────


def _graduated_entries(contract_path: Optional[os.PathLike] = None) -> List[dict]:
    from grove.cold_start import _load_contract

    contract = _load_contract(Path(contract_path) if contract_path is not None else None)
    return [e for e in contract["entries"] if e.get("graduated")]


def classify_graduated(
    home: Optional[os.PathLike] = None, contract_path: Optional[os.PathLike] = None
) -> List[FileClassification]:
    """Classify every graduated file under *home* (default ``get_hermes_home()``)."""
    from hermes_constants import get_hermes_home

    home_path = Path(home) if home is not None else get_hermes_home()
    out: List[FileClassification] = []
    for e in _graduated_entries(contract_path):
        rel = str(e["path"])
        out.append(
            classify_instance_file(
                home_path / rel, e.get("expected_shape", "mapping"), name=rel
            )
        )
    return out


def preflight_graduated_files(
    home: Optional[os.PathLike] = None, contract_path: Optional[os.PathLike] = None
) -> List[FileClassification]:
    """Gateway boot Andon (D3): classify the graduated set; raise a loud
    :class:`InstanceFileError` naming every MALFORMED/UNREADABLE file and the
    repair invocation. Returns the classifications on success (no faults)."""
    results = classify_graduated(home, contract_path)
    faults = [c for c in results if c.is_fault]
    if faults:
        lines = "\n".join(f"  - {c.name}: {c.health.value.upper()} — {c.evidence}" for c in faults)
        raise InstanceFileError(
            f"cold-start preflight: {len(faults)} graduated instance file(s) are "
            f"malformed or unreadable — the gateway will not boot on a corrupt "
            f"instance:\n{lines}\n\nRun `{REPAIR_INVOCATION}` to quarantine and "
            f"reseed the affected file(s)."
        )
    return results


# ── F2 repair: detect → propose → confirm → quarantine + reseed ──────────────


def _utc_stamp() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def quarantine_dir(home: Path) -> Path:
    """The declared quarantine location — a contract operator-state entry, so
    residue is sanctioned by declaration, not future drift archaeology."""
    return home / "quarantine"


def quarantine_destination(home: Path, name: str, stamp: str) -> Path:
    # Flatten any subdir in the name (none in the graduated set today) so the
    # residue lands directly under quarantine/.
    leaf = Path(name).name
    return quarantine_dir(home) / f"{leaf}.{stamp}"


@dataclass
class RepairPlan:
    faults: List[FileClassification]

    @property
    def has_faults(self) -> bool:
        return bool(self.faults)


def detect(home: Optional[os.PathLike] = None, contract_path: Optional[os.PathLike] = None) -> RepairPlan:
    """Detection — parses instance files for CLASSIFICATION ONLY (contained);
    never depends on a successful parse, so it runs even when every file is
    corrupt (F2 hard-ban compliant)."""
    faults = [c for c in classify_graduated(home, contract_path) if c.is_fault]
    return RepairPlan(faults=faults)


def run_repair(
    *,
    home: Optional[os.PathLike] = None,
    contract_path: Optional[os.PathLike] = None,
    confirm: Callable[[RepairPlan], bool],
    out: Callable[[str], None] = print,
) -> dict:
    """Full repair flow (D4): detect → report → PROPOSE quarantine+reseed →
    explicit confirmation → SYSTEM quarantine (atomic move) + reseed via the F1
    materializer (seed-on-absence; no second writer). Operator declines → zero
    writes. Returns a summary dict.
    """
    from hermes_constants import get_hermes_home

    home_path = Path(home) if home is not None else get_hermes_home()
    cpath = Path(contract_path) if contract_path is not None else None

    plan = detect(home_path, cpath)
    if not plan.has_faults:
        out("Instance health: all graduated files OK. Nothing to repair.")
        return {"faults": 0, "quarantined": [], "confirmed": False}

    out(f"Instance health: {len(plan.faults)} file(s) need repair:")
    for c in plan.faults:
        out(f"  • {c.name} — {c.health.value.upper()}: {c.evidence}")
    stamp = _utc_stamp()
    out("\nProposed: quarantine each file, then reseed from the definition "
        "(seed-on-absence). Quarantine location:")
    for c in plan.faults:
        out(f"  • {c.name} → {quarantine_destination(home_path, c.name, stamp)}")

    if not confirm(plan):
        out("Declined — no changes made.")
        return {"faults": len(plan.faults), "quarantined": [], "confirmed": False}

    # SYSTEM performs the writes. Quarantine first (atomic move), then reseed.
    qdir = quarantine_dir(home_path)
    qdir.mkdir(parents=True, exist_ok=True)
    quarantined = []
    for c in plan.faults:
        dest = quarantine_destination(home_path, c.name, stamp)
        os.rename(str(c.path), str(dest))  # atomic move; the file is now ABSENT
        quarantined.append({"name": c.name, "quarantined_to": str(dest)})
        out(f"Quarantined {c.name} → {dest}")

    # Reseed via the F1 materializer THROUGH THE PLAIN PATH — no write-forcing.
    # Each quarantined file is now ABSENT, so ordinary seed-on-absence restores
    # the definition-seeded ones; seed_source: none ones (grants.yaml) simply
    # return to their legitimate-absent state. The quarantine moves mutated the
    # instance, so the memoized report is stale — invalidate it, then plain
    # materialize re-runs and re-seeds. never-overwrite is untouched.
    from grove.cold_start import invalidate_cache, materialize_instance

    invalidate_cache(home_path)
    materialize_instance(home_path)

    # Confirm green.
    after = detect(home_path, cpath)
    if after.has_faults:
        remaining = ", ".join(f"{c.name} ({c.health.value})" for c in after.faults)
        out(f"WARNING: still faulted after reseed: {remaining}")
    else:
        out("Reseed complete — instance health is now clean.")
    return {
        "faults": len(plan.faults),
        "quarantined": quarantined,
        "confirmed": True,
        "clean_after": not after.has_faults,
    }
