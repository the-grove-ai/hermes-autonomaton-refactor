"""Cold-start instance materializer (instance-cold-start-parity-v1 P1).

Brings a fresh ``$GROVE_HOME`` to a minimal-viable state at boot, driven
ENTIRELY by the declarative contract at ``config/cold_start.yaml`` — there are
no dir/file path literals in this module (Pattern v1.3 §IV.I: declarative
governance, no hardcoded behavior).

Siting rationale: this lives in ``grove/`` beside its collaborators
(``grove/manifest.py``, ``grove/identity.py``, ``grove/zones.py``,
``grove/dock/``, ``grove/grants.py``). Each of those consumes one slice of the
instance-state surface this module seeds, and each resolves the instance root
the same way — ``get_hermes_home()``. The materializer is the boot-time peer
that guarantees the surface exists before they read it.

Pinned semantics (GATE-B F1, non-negotiable):
  * Every file creation goes through a tmp opened ``O_CREAT|O_EXCL``; every
    write is tmp + ``os.rename`` (atomic). A pre-existing tmp is refused loudly
    (stale crash artifact or planted file), never clobbered.
  * Symlinks are refused (``is_symlink`` / lexists) — the materializer never
    writes THROUGH a symlink to an attacker-chosen target.
  * Seed-on-absence ONLY. An operator's file is NEVER overwritten.
  * Idempotent: a second run on a complete instance writes nothing.
  * Four-case marker/adoption logic (see :func:`_classify`).
  * Missing/placeholder OpenRouter key at first boot yields a governed,
    actionable signal naming the env var — not a downstream provider 401.
  * Honors ``GROVE_HOME`` throughout via ``get_hermes_home()``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_COLD_START_VERSION = 1
_MARKER_NAME = ".grove_instance"
_VALID_ABSENCE_CLASSES = frozenset(
    {"silent_empty", "loud_empty", "graceful_none", "fail_loud", "seed_on_absence"}
)
_VALID_KINDS = frozenset({"dir", "file"})
_VALID_OWNERS = frozenset({"definition-seeded", "operator-state"})
# P2 F4 — top-level shapes a graduated file's live reader may require.
_VALID_SHAPES = frozenset({"mapping", "sequence"})

# Process-level guard: the materializer is invoked from load_config, which runs
# on effectively every CLI/gateway operation. The first call per home does the
# work; subsequent calls for the same home return the cached report with zero
# filesystem touches. Re-run with bypass_cache=True, or drop an entry via
# invalidate_cache()/_reset_cache().
_MATERIALIZED: Dict[str, "MaterializeReport"] = {}


class ColdStartError(RuntimeError):
    """A cold-start invariant was violated — refuse loudly, never degrade."""


@dataclass
class MaterializeReport:
    """What one materialization did. ``wrote_anything`` is the idempotency oracle."""

    home: str
    state: str  # "fresh" | "adopt" | "marked"
    created_dirs: List[str] = field(default_factory=list)
    seeded_files: List[str] = field(default_factory=list)
    skipped_present: List[str] = field(default_factory=list)
    marker_written: bool = False

    @property
    def wrote_anything(self) -> bool:
        return bool(self.created_dirs or self.seeded_files or self.marker_written)


# ── repo / contract resolution ───────────────────────────────────────────────


def _repo_root() -> Path:
    """The repo root — ``grove/cold_start.py`` is one level under it."""
    return Path(__file__).resolve().parent.parent


def _contract_path() -> Path:
    return _repo_root() / "config" / "cold_start.yaml"


def _load_contract(contract_path: Optional[Path]) -> Dict[str, Any]:
    """Parse + validate the cold-start contract. Fail loud on any defect."""
    path = Path(contract_path) if contract_path is not None else _contract_path()
    if not path.exists():
        raise ColdStartError(f"cold_start contract not found at {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ColdStartError(f"cold_start contract at {path} is not a mapping")
    if data.get("version") != _COLD_START_VERSION:
        raise ColdStartError(
            f"cold_start contract at {path}: unsupported version "
            f"{data.get('version')!r} (expected {_COLD_START_VERSION})"
        )
    sig = data.get("grove_shape_signature")
    if not isinstance(sig, list) or not sig:
        raise ColdStartError(
            f"cold_start contract at {path}: grove_shape_signature must be a "
            f"non-empty list"
        )
    entries = data.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ColdStartError(f"cold_start contract at {path}: entries must be a non-empty list")
    for i, e in enumerate(entries):
        where = f"cold_start contract entry [{i}]"
        if not isinstance(e, dict):
            raise ColdStartError(f"{where} is not a mapping")
        for req in ("path", "kind", "seed_source", "absence_class", "owner"):
            if req not in e:
                raise ColdStartError(f"{where} missing required field {req!r}")
        if e["kind"] not in _VALID_KINDS:
            raise ColdStartError(f"{where} kind {e['kind']!r} not in {sorted(_VALID_KINDS)}")
        if e["absence_class"] not in _VALID_ABSENCE_CLASSES:
            raise ColdStartError(
                f"{where} absence_class {e['absence_class']!r} not in "
                f"{sorted(_VALID_ABSENCE_CLASSES)}"
            )
        if e["owner"] not in _VALID_OWNERS:
            raise ColdStartError(f"{where} owner {e['owner']!r} not in {sorted(_VALID_OWNERS)}")
        # N1 — a declared seed_source MUST exist in the repo, or the contract is
        # a broken promise: a fresh install would silently lack the file.
        src = e["seed_source"]
        if src and src != "none":
            src_abs = _repo_root() / src
            if not src_abs.exists():
                raise ColdStartError(
                    f"{where}: seed_source {src!r} does not exist in the repo "
                    f"({src_abs}) — the contract references a missing template"
                )
        # P2 F4 — optional graduation fields. A graduated file needs an
        # expected_shape the classifier can enforce; only files graduate.
        if "graduated" in e and not isinstance(e["graduated"], bool):
            raise ColdStartError(f"{where} graduated must be a bool, got {e['graduated']!r}")
        if e.get("graduated"):
            if e["kind"] != "file":
                raise ColdStartError(f"{where} graduated is file-only (kind={e['kind']!r})")
            shape = e.get("expected_shape")
            if shape not in _VALID_SHAPES:
                raise ColdStartError(
                    f"{where} graduated entry needs expected_shape in "
                    f"{sorted(_VALID_SHAPES)}, got {shape!r}"
                )
    return data


# ── atomic, symlink-refusing filesystem primitives (F1) ──────────────────────


def _refuse_symlink(path: Path) -> None:
    """Refuse to act on a path that is a symlink (broken or not). lexists-class."""
    if path.is_symlink():
        raise ColdStartError(
            f"cold_start: refusing to materialize through symlink at {path} — "
            f"the target could be anywhere; this is not a safe seed site"
        )


def _secure_path(path: Path, managed: bool) -> None:
    """Set owner-only (0700) perms on a materialized dir, mirroring the retired
    ``hermes_cli.config._secure_dir``: skipped in managed mode (the NixOS module
    sets group-shared perms via umask), honors ``GROVE_HOME_MODE``, no-op-safe on
    platforms without POSIX chmod semantics."""
    if managed:
        return
    override = os.environ.get("GROVE_HOME_MODE")
    try:
        os.chmod(path, int(override, 8) if override else 0o700)
    except (OSError, ValueError):
        pass


def _atomic_seed(dst: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Write *data* to *dst* atomically: tmp opened O_CREAT|O_EXCL, then
    os.rename. A pre-existing tmp (stale crash artifact / planted file) makes
    ``os.open`` raise ``FileExistsError`` — we let it propagate as a loud refusal
    rather than clobber it. The tmp name is pid-scoped so concurrent boots on
    different processes don't collide.

    Atomicity is provided by ``os.rename`` (a reader sees either no file or the
    complete file) — F1's requirement. We deliberately do NOT ``fsync``: the
    write closes (flushing to the OS page cache) before the rename, which is
    enough for atomicity, and fsync-per-seed adds no correctness here (a crash
    before the boot seed completes simply re-materializes on next boot) while
    imposing heavy disk-contention cost when the suite creates thousands of
    per-test instances."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.parent / f".{dst.name}.cold-start.{os.getpid()}.tmp"
    # O_EXCL: refuse a pre-existing tmp. Kept OUTSIDE the try so its collision
    # does not trip the tmp-cleanup finally (that tmp is not ours to remove).
    fd = os.open(str(tmp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)  # flushed to the OS on close, before the rename below
        os.rename(str(tmp), str(dst))  # atomic within the dir
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)


def _ensure_dir(path: Path, report: MaterializeReport, managed: bool) -> None:
    _refuse_symlink(path)
    if path.is_dir():
        return
    if path.exists():
        raise ColdStartError(
            f"cold_start: {path} exists and is not a directory — refusing to "
            f"materialize a required dir over a file"
        )
    path.mkdir(parents=True, exist_ok=True)
    _secure_path(path, managed)
    report.created_dirs.append(str(path))


def _seed_file(src: Path, dst: Path, report: MaterializeReport, managed: bool) -> None:
    _refuse_symlink(dst)
    if dst.exists():
        # Present → seed-on-absence contract: never overwrite an operator file.
        report.skipped_present.append(str(dst))
        return
    if not src.exists():
        raise ColdStartError(f"cold_start: seed source missing for {dst}: {src}")
    # Managed (NixOS) installs share state across the hermes group → 0660; a
    # sovereign single-operator install keeps seeded files owner-only (0600).
    _atomic_seed(dst, src.read_bytes(), mode=0o660 if managed else 0o600)
    report.seeded_files.append(str(dst))


# ── classification (four-case marker/adoption) ───────────────────────────────


def _grove_shaped(home: Path, signature: List[str]) -> bool:
    return any((home / s).exists() for s in signature)


def _is_empty_dir(home: Path) -> bool:
    return home.is_dir() and not any(home.iterdir())


def _classify(home: Path, marker: Path, signature: List[str]) -> str:
    """Four cases (F1):
      * marker present                         → "marked"  (seed-if-absent)
      * home absent or empty                   → "fresh"   (materialize + marker)
      * non-empty, no marker, grove-shaped     → "adopt"   (marker + seed-if-absent)
      * non-empty, no marker, NOT grove-shaped → refuse loudly (misconfig)
    """
    if marker.is_symlink():
        raise ColdStartError(f"cold_start: {marker} is a symlink — refusing (tampered marker)")
    if marker.exists():
        return "marked"
    if not home.exists() or _is_empty_dir(home):
        return "fresh"
    if _grove_shaped(home, signature):
        return "adopt"
    return "refuse"


# ── marker (versioned, contract-stamped) ─────────────────────────────────────


def _contract_sha256(contract_path: Optional[Path]) -> str:
    import hashlib

    path = Path(contract_path) if contract_path is not None else _contract_path()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _marker_bytes(contract: Dict[str, Any], contract_path: Optional[Path]) -> bytes:
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    body = (
        "# .grove_instance — cold-start materializer marker (do not edit)\n"
        f"cold_start_version: {_COLD_START_VERSION}\n"
        f"contract_version: {contract.get('version')}\n"
        f"contract_sha256: {_contract_sha256(contract_path)}\n"
        "schema: instance-cold-start-parity-v1\n"
        f"materialized_at: {now}\n"
    )
    return body.encode("utf-8")


def _write_marker(
    marker: Path,
    contract: Dict[str, Any],
    contract_path: Optional[Path],
    report: MaterializeReport,
    managed: bool,
) -> None:
    _refuse_symlink(marker)
    if marker.exists():
        return
    _atomic_seed(marker, _marker_bytes(contract, contract_path), mode=0o660 if managed else 0o600)
    report.marker_written = True


# ── OpenRouter key signal (F5c) ──────────────────────────────────────────────


def check_openrouter_key() -> Optional[str]:
    """Return a governed, actionable signal if no usable key is configured in the
    environment, else None. Names the env var; never surfaces a provider 401."""
    try:
        from hermes_cli.auth import has_usable_secret
    except Exception:  # noqa: BLE001 — fallback keeps cold-start self-contained
        def has_usable_secret(value: Any, **_: Any) -> bool:  # type: ignore[misc]
            return isinstance(value, str) and len(value.strip()) >= 4

    for var in ("OPENROUTER_API_KEY", "OPENAI_API_KEY"):
        if has_usable_secret(os.environ.get(var)):
            return None
    return (
        "cold_start: no usable OpenRouter API key found in the environment. Set "
        "OPENROUTER_API_KEY (or OPENAI_API_KEY) to a valid key before the "
        "Autonomaton's first model call — otherwise that call fails with a "
        "provider 401. This is a configuration signal, not an error."
    )


# ── managed-mode accommodation ───────────────────────────────────────────────


def _is_managed() -> bool:
    """Lazy import to avoid a hermes_cli.config <-> grove.cold_start import cycle
    (config.load_config invokes the materializer)."""
    try:
        from hermes_cli.config import is_managed

        return is_managed()
    except Exception:  # noqa: BLE001
        return False


# ── public entrypoint ────────────────────────────────────────────────────────


def invalidate_cache(home: Optional[os.PathLike] = None) -> None:
    """Drop the memoized materialization for *home* (or all homes if None).

    The per-home cache assumes the instance filesystem is unchanged since the
    last run. A caller that mutates the instance out-of-band (e.g. the repair
    flow quarantines a file) must invalidate so the next plain
    ``materialize_instance`` re-runs and re-seeds via the ordinary
    seed-on-absence path — no write-forcing parameter required."""
    if home is None:
        _MATERIALIZED.clear()
    else:
        _MATERIALIZED.pop(str(Path(home)), None)


def materialize_instance(
    home: Optional[os.PathLike] = None,
    *,
    bypass_cache: bool = False,
    contract_path: Optional[os.PathLike] = None,
) -> MaterializeReport:
    """Materialize the minimal-viable instance under *home* (default:
    ``get_hermes_home()``, honoring ``GROVE_HOME``). Returns a
    :class:`MaterializeReport`. Cheap and memoized per-home.

    F1 four-case guard (UNCONDITIONAL on every path, CLI shim included): a
    non-empty, unmarked, non-Grove-shaped home is a GROVE_HOME misconfiguration
    and is REFUSED loudly — zero provisioning writes into a case-4 directory.
    There is no lenient bypass; the loud refuse is the governed halt P0.2
    demands (warn-then-proceed is not a governed halt).

    ``bypass_cache`` (default False) skips ONLY the per-home process
    memoization (below), so ``_run`` executes again — it is a cache control,
    NOT a write control. It cannot override never-overwrite: the seed/dir/marker
    writers each check ``exists()`` unconditionally, independent of this flag.
    Used by tests that re-run against the same home; production repair uses
    :func:`invalidate_cache` + the plain path instead.
    """
    home_path = Path(home) if home is not None else get_hermes_home()
    key = str(home_path)
    if not bypass_cache and key in _MATERIALIZED:
        return _MATERIALIZED[key]

    cpath = Path(contract_path) if contract_path is not None else None
    managed = _is_managed()
    if managed and not home_path.exists():
        # Managed (NixOS) installs create GROVE_HOME via the activation script.
        # Its absence is an operator/provisioning fault, not ours to paper over.
        raise ColdStartError(
            f"GROVE_HOME {home_path} does not exist. Run 'sudo nixos-rebuild switch' first."
        )
    old_umask = os.umask(0o007) if managed else None
    try:
        report = _run(home_path, cpath, managed)
    finally:
        if managed and old_umask is not None:
            os.umask(old_umask)

    _MATERIALIZED[key] = report
    return report


def _run(home: Path, contract_path: Optional[Path], managed: bool) -> MaterializeReport:
    contract = _load_contract(contract_path)
    signature = list(contract["grove_shape_signature"])
    marker = home / _MARKER_NAME
    state = _classify(home, marker, signature)
    report = MaterializeReport(home=str(home), state=state)

    if state == "refuse":
        # F1 case 4 — UNCONDITIONAL. Refuse everywhere (CLI shim included) with
        # an actionable message: the resolved dir, the expected signature set,
        # and that GROVE_HOME likely points at the wrong place. No writes occur.
        raise ColdStartError(
            f"cold_start: refusing to materialize into {home} — it is non-empty, "
            f"carries no {_MARKER_NAME} marker, and is not Grove-shaped (none of the "
            f"signature entries {signature} are present). GROVE_HOME almost certainly "
            f"points at the wrong directory. Set GROVE_HOME to a real instance (or an "
            f"empty dir), or clear this one. No files were written."
        )

    # Only reachable for a genuinely absent home in non-managed mode (managed
    # raised above; adopt/marked imply the dir already exists).
    created_home = not home.exists()
    if created_home:
        home.mkdir(parents=True, exist_ok=True)
        _secure_path(home, managed)
        report.created_dirs.append(str(home))

    for entry in contract["entries"]:
        _materialize_entry(home, entry, report, managed)

    # Stamp the marker for a fresh dir or an adopted one; "marked" already has it.
    if state in ("fresh", "adopt"):
        _write_marker(marker, contract, contract_path, report, managed)

    # F5c key check is NOT done here — it is a separate gateway-boot step
    # (check_openrouter_key, called from run_gateway per the D3 sequence:
    # materialize -> preflight -> key check). Materialization stays key-agnostic.
    return report


def _materialize_entry(
    home: Path, entry: Dict[str, Any], report: MaterializeReport, managed: bool
) -> None:
    rel = str(entry["path"]).rstrip("/")
    dst = home / rel
    if entry["kind"] == "dir":
        _ensure_dir(dst, report, managed)
        return
    # kind == file
    src = entry.get("seed_source")
    if not src or src == "none":
        # Documented, not materialized — absence is legitimate (see absence_class).
        return
    _seed_file(_repo_root() / src, dst, report, managed)


def _reset_cache() -> None:
    """Test hook: clear the process-level materialization guard."""
    _MATERIALIZED.clear()
