"""The sole sanctioned writer for ``routing.config.yaml`` (portal-model-swap-v1).

Every mutation of the operator routing config funnels through this module.
Both the flywheel consolidation approver (``flywheel_cli._approve_consolidation``,
refactored in Phase 2) and the portal model-swap handler call it, so the
pipeline — BACKUP → ruamel round-trip LOAD → MUTATE → sandbox-VALIDATE via a
fresh ``CognitiveRouter`` → atomic REPLACE → HOT-RELOAD — lives in exactly one
place (C1; GRV-008 § III: one sanctioned writer of ``routing.config.yaml``).

Why ruamel and not pyyaml: the operator file is comment-dense (a banner, per-tier
prose, a commented-out local-binding block, inline notes). The router READ path
uses ``yaml.safe_load`` (comment-blind, harmless once parsed), but a naive
``yaml.safe_dump`` round-trip would strip every comment and reflow the file.
This writer uses ``ruamel.yaml`` round-trip with the same settings the prior
sanctioned writer used, so comments survive (C8 / AC-8).

Concurrency (C2): the module-level singleton holds an ``asyncio.Lock``. Every
async mutation acquires it before the read and releases after the atomic replace
and reload, so three rapid portal taps serialize cleanly and no two writers race
the same file. The sync core (:meth:`RoutingConfigWriter.apply_mutation`) is the
lower-level primitive sync callers (the flywheel CLI) reuse directly.

Pointer-atomic swap (C6) needs no new machinery: the Dispatcher binds a turn's
tier ONCE at turn start from a frozen ``TierConfig`` and never re-consults the
router mid-turn, so an in-flight turn finishes on the OLD config. This writer
only calls ``reload()`` after the atomic replace; the NEXT turn picks up the new
binding.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from ruamel.yaml import YAML

logger = logging.getLogger(__name__)


class ConfigValidationError(Exception):
    """A proposed ``routing.config.yaml`` mutation was rejected (C3).

    Raised when the requested change is structurally impossible (unknown tier,
    missing model, no ``previous_model`` to revert to) or when the mutated
    config fails to construct a sandbox ``CognitiveRouter``. In every case the
    live file is left exactly as it was (restored from backup) — no partial
    state. The portal handler catches this and renders an inline error fragment
    via ``_html_fragment``; the tier card stays put, no 500, no traceback.
    """


@dataclass(frozen=True)
class TierSwapResult:
    """Outcome of :meth:`RoutingConfigWriter.swap_tier_model`.

    ``status`` is ``"swapped"`` (the binding changed and was written) or
    ``"noop"`` (the tier was ALREADY bound to the requested model — nothing was
    written; the live file is byte- and mtime-identical). A no-op is not an
    error: the portal surfaces it as info (ledger-eventtype-hygiene-v1 Change 3).
    """

    status: str  # "swapped" | "noop"
    tier: str
    model: str


def _default_config_path() -> Path:
    """The operator routing config — ``$GROVE_HOME/routing.config.yaml``.

    Resolved the same way ``flywheel_cli._operator_config_path`` resolves it,
    so this writer mutates exactly the file the live router reads.
    """
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "routing.config.yaml"


def _default_machine_path() -> Path:
    """The machine overlay — ``$GROVE_HOME/routing.autonomaton.yaml``.

    Matches ``flywheel_cli._machine_config_path``. Passed to the sandbox router
    so validation merges operator + machine exactly as the live router does.
    """
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "routing.autonomaton.yaml"


def _default_reload() -> None:
    """Hot-reload the live Cognitive Router so the new binding takes effect.

    Mirrors ``flywheel_cli._reload_default_router`` (inlined to keep this module
    free of the CLI import chain). No live router — an offline tool, a fresh
    test process — is not an error: the operator file is the source of truth,
    re-read at next init.
    """
    import grove.router as _router_mod

    router = _router_mod._default_router
    if router is None:
        logger.info(
            "[routing_writer] config written; no live router to hot-reload "
            "(applies on next init)"
        )
        return
    router.reload()


def _ruamel() -> YAML:
    """A ruamel round-trip parser tuned to ``routing.config.yaml``'s layout.

    Same settings the prior sanctioned writer (``_approve_consolidation``) used,
    so the comment-dense file round-trips without reflow.
    """
    yaml_rt = YAML()
    yaml_rt.preserve_quotes = True
    yaml_rt.indent(mapping=2, sequence=4, offset=2)
    yaml_rt.width = 100
    return yaml_rt


def _tier_entry(data: Any, tier: str) -> Any:
    """Return the ``tier_preferences[tier]`` mapping, or raise loudly.

    Rejects unknown tiers and handler-backed tiers (e.g. T0, which carries no
    ``model``) with a ``ConfigValidationError`` the portal surfaces inline.
    """
    if not isinstance(data, dict):
        raise ConfigValidationError("routing.config.yaml did not parse to a mapping")
    routing = data.get("routing")
    if not isinstance(routing, dict):
        raise ConfigValidationError("routing.config.yaml has no 'routing' mapping")
    tier_prefs = routing.get("tier_preferences")
    if not isinstance(tier_prefs, dict):
        raise ConfigValidationError("routing.config.yaml has no 'tier_preferences'")
    entry = tier_prefs.get(tier)
    if not isinstance(entry, dict):
        raise ConfigValidationError(
            f"tier {tier!r} is not a model-bound tier in tier_preferences"
        )
    return entry


def _file_routing_mutation_event(label: str, config_path: str) -> None:
    """routing-scope-wall-v1 R-W4 — the writer audits itself (mirrors
    grove.capability_registry._file_binding_mutation_event).

    routing.config.yaml is a scope-defining authority surface; the sole
    sanctioned writer must leave a Kaizen-ledger trail. Component-filer pattern:
    RoutingConfigWriter has no CLI session of its own, so the event lands under a
    ``cli-<utc-timestamp>`` sentinel session. Error-log floor: this runs AFTER the
    mutation has landed atomically, so a filing failure must not misreport the
    write as failed — it logs at ERROR and stands.
    """
    try:
        from datetime import datetime, timezone

        from grove.kaizen_ledger import KaizenLedger

        session_id = "cli-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        KaizenLedger(session_id=session_id).record(
            "routing_config_mutation",
            label=label,
            config_path=config_path,
            surface_class="scope_defining",
        )
    except Exception as file_exc:  # noqa: BLE001 — filing leg, log floor stands
        logger.error(
            "[routing_writer] routing_config_mutation filing failed "
            "(mutation itself SUCCEEDED): %r",
            file_exc,
        )


class RoutingConfigWriter:
    """Single-writer pipeline for ``routing.config.yaml``.

    Construct one per config path. The module-level singleton (:func:`get_writer`)
    is the one the portal and the flywheel CLI share; tests construct their own
    against a temp file with an injected ``reload_fn``.
    """

    def __init__(
        self,
        config_path: Path,
        *,
        machine_path: Optional[Path] = None,
        reload_fn: Optional[Callable[[], None]] = None,
    ) -> None:
        self._config_path = Path(config_path)
        # Resolve the machine overlay once; the sandbox router validates the
        # merged operator+machine config exactly as the live router would.
        self._machine_path = (
            Path(machine_path) if machine_path is not None else _default_machine_path()
        )
        self._reload_fn = reload_fn if reload_fn is not None else _default_reload
        # C2 — one lock per writer; every async mutation serializes on it.
        self._lock = asyncio.Lock()

    # ----- public async API (the portal calls these) -------------------------

    async def swap_tier_model(self, tier: str, new_slug: str) -> "TierSwapResult":
        """Bind ``tier`` to ``new_slug``, preserving the old model for one undo.

        Acquires the lock, then through the shared pipeline: copies the current
        ``model`` to ``previous_model`` and sets ``model = new_slug`` (C5 — one
        level of undo, not a stack). Returns a :class:`TierSwapResult`. Raises
        ``ConfigValidationError`` if the tier carries no model or the mutated
        config fails sandbox validation; the live file is untouched in either case.

        ledger-eventtype-hygiene-v1 Change 3 — a NO-OP swap (the tier is already
        bound to ``new_slug``) is not an error. It is caught by a READ-ONLY
        pre-check BEFORE ``apply_mutation``, so it writes NOTHING (no backup, no
        atomic replace — the file's bytes and mtime are untouched) and returns
        ``status="noop"``; the portal renders that as info, not an error surface.
        """
        new_slug = (new_slug or "").strip()
        if not new_slug:
            raise ConfigValidationError("swap requires a non-empty model slug")

        async with self._lock:
            # No-op pre-check — read-only. Also validates tier/model presence
            # (raises ConfigValidationError), so an unknown tier or a model-less
            # tier still fails loud exactly as before, without a write.
            current = self._current_tier_model(tier)
            if current == new_slug:
                return TierSwapResult(status="noop", tier=tier, model=new_slug)

            def mutate(data: Any) -> None:
                entry = _tier_entry(data, tier)
                old = entry.get("model")
                if not old:
                    raise ConfigValidationError(
                        f"tier {tier!r} has no current model to swap from"
                    )
                entry["previous_model"] = old
                entry["model"] = new_slug

            self.apply_mutation(mutate, label=f"swap {tier} -> {new_slug}")
            return TierSwapResult(status="swapped", tier=tier, model=new_slug)

    async def revert_tier_model(self, tier: str) -> None:
        """Undo the last swap: exchange ``model`` and ``previous_model`` (C5/AC-6).

        Runs through the same write path as swap. After a revert, ``previous_model``
        holds the model just reverted away from, so a second tap re-applies the
        swap (a single toggle, not a stack). Raises ``ConfigValidationError`` if no
        ``previous_model`` is recorded for the tier.
        """

        def mutate(data: Any) -> None:
            entry = _tier_entry(data, tier)
            prev = entry.get("previous_model")
            if not prev:
                raise ConfigValidationError(
                    f"tier {tier!r} has no previous_model to revert to"
                )
            entry["previous_model"] = entry.get("model")
            entry["model"] = prev

        async with self._lock:
            self.apply_mutation(mutate, label=f"revert {tier}")

    # ----- sync core (sync callers — the flywheel CLI — reuse this) ----------

    def apply_mutation(
        self, mutate: Callable[[Any], None], *, label: str = "mutate routing config"
    ) -> None:
        """BACKUP → load → ``mutate`` → sandbox-validate → atomic replace → reload.

        The lower-level write primitive. ``mutate`` receives the ruamel-loaded
        document and edits it in place; it may raise ``ConfigValidationError`` to
        reject the change before anything is written. Any failure after the backup
        is taken restores the operator file to its pre-mutation bytes and re-raises
        (fail loud, no partial state). On success the live router is hot-reloaded.

        Sync by design: the async ``swap``/``revert`` wrap this under the lock;
        the flywheel CLI (offline, single-threaded) calls it directly.
        """
        op_path = self._config_path
        if not op_path.exists():
            raise ConfigValidationError(
                f"routing config not found at {op_path}; cannot {label}"
            )

        # Step 1 — BACKUP the operator file bytes.
        backup = op_path.read_bytes()
        bak_path = op_path.with_suffix(op_path.suffix + ".bak")
        bak_path.write_bytes(backup)

        try:
            # Step 2 — ruamel round-trip LOAD.
            yaml_rt = _ruamel()
            with open(op_path, encoding="utf-8") as fh:
                data = yaml_rt.load(fh)

            # Step 3 — MUTATE in place (may reject with ConfigValidationError).
            mutate(data)

            # Step 4 — sandbox-VALIDATE before touching the live file (C3).
            self._sandbox_validate(yaml_rt, data)

            # Step 5 — atomic REPLACE (temp + os.replace on the same filesystem).
            op_tmp = op_path.with_suffix(op_path.suffix + ".tmp")
            with open(op_tmp, "w", encoding="utf-8") as fh:
                yaml_rt.dump(data, fh)
            os.replace(op_tmp, op_path)

            # Step 6 — HOT-RELOAD (C6); the next turn picks up the binding.
            self._reload_fn()
        except Exception:
            # Restore the operator file to its pre-mutation state, then fail loud.
            op_path.write_bytes(backup)
            raise

        # R-W4 — self-audit AFTER the mutation has landed atomically. Outside the
        # try so a ledger failure never triggers the restore (the write succeeded);
        # the error-log floor inside the filer handles a down ledger.
        _file_routing_mutation_event(label, str(op_path))
        logger.info("[routing_writer] applied: %s (%s)", label, op_path)

    # ----- internals ---------------------------------------------------------

    def _current_tier_model(self, tier: str) -> str:
        """The model currently bound to ``tier``, read READ-ONLY from the operator
        file. Raises ``ConfigValidationError`` if the config is missing, the tier
        is absent, or the tier carries no model. Backs ``swap_tier_model``'s no-op
        pre-check so a same-model swap returns without touching the file."""
        op_path = self._config_path
        if not op_path.exists():
            raise ConfigValidationError(
                f"routing config not found at {op_path}; cannot read tier {tier!r}"
            )
        yaml_rt = _ruamel()
        with open(op_path, encoding="utf-8") as fh:
            data = yaml_rt.load(fh)
        entry = _tier_entry(data, tier)
        old = entry.get("model")
        if not old:
            raise ConfigValidationError(
                f"tier {tier!r} has no current model to swap from"
            )
        return old

    def _sandbox_validate(self, yaml_rt: YAML, data: Any) -> None:
        """Construct a throwaway ``CognitiveRouter`` from the mutated config.

        Writes the candidate document to a temp file and instantiates a router
        against it (merged with the machine overlay, as live). If construction
        raises — bad schema, unparseable tier, missing required field — the change
        is rejected as a ``ConfigValidationError`` and the live file never moves.
        """
        from grove.router import CognitiveRouter

        with tempfile.NamedTemporaryFile(
            "w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as tmp:
            yaml_rt.dump(data, tmp)
            sandbox_path = tmp.name
        try:
            CognitiveRouter(Path(sandbox_path), machine_path=self._machine_path)
        except Exception as exc:
            raise ConfigValidationError(
                f"proposed routing.config.yaml failed sandbox validation: {exc}"
            ) from exc
        finally:
            os.unlink(sandbox_path)


# ----- module-level singleton -------------------------------------------------

_writer: Optional[RoutingConfigWriter] = None


def get_writer() -> RoutingConfigWriter:
    """Return the shared ``RoutingConfigWriter`` (lazy-init on first use).

    Bound to ``$GROVE_HOME/routing.config.yaml``. The portal handler and the
    refactored flywheel approver both go through this one instance, so its
    ``asyncio.Lock`` serializes every config mutation in the process (C2).
    """
    global _writer
    if _writer is None:
        _writer = RoutingConfigWriter(_default_config_path())
    return _writer
