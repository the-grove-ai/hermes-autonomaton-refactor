"""Affordances loader + runtime capability introspection.

Sprint 23 (soul-affordances-register-v1) extends Sprint 07's identity
composition with the **affordances** layer — declarative + introspected
capability landscape that eliminates turn-1 capability rediscovery.

Two surfaces:

* ``load_affordances(home)`` — graceful-tier read of
  ``~/.grove/affordances.md``, first-run-seeded from
  ``config/identity/affordances.md``. Missing reference template is
  Jidoka (the install is structurally incomplete per D1); missing
  operator copy is graceful (seeded silently from the reference).

* ``introspect_capabilities()`` — read-only enumeration of live state:
  connected MCP servers (from ``config.yaml``), Cognitive Router tiers
  (from ``routing.config.yaml``), available slash commands (from
  ``COMMAND_REGISTRY``), cellar index status. Never invokes MCPs,
  never instantiates the router, never forces a cellar build.
  Defensive — returns "(none)" / "(unavailable)" prose for missing
  state rather than raising. Sprint 23 D2.

The composition layer (``grove.identity.load_identity()``) calls both
during the stable-tier build per Sprint 23 GATE-A decision (composer
orchestrates introspection, not the run_agent caller). The result is
two new fields on ``IdentityComposition`` — ``affordances`` (static)
and ``capabilities`` (live) — composed in D5 order immediately after
the register overlay.

Sprint 23 explicitly does NOT auto-write introspection diffs back to
``~/.grove/affordances.md`` — that's Kaizen detector territory
(v0.2). Static content reflects the operator's curated description;
introspection reflects the moment; both compose; the operator
reconciles.

Introspection-vs-governance asymmetry
=====================================
Two failure styles in this module, deliberately distinct:

* **Governance checks** (``load_affordances`` when reference template
  is missing, ``grove.register.validate_canon_present``,
  ``grove.register.load_register``, ``grove.identity.load_identity``)
  are **Jidoka-tier** and raise ``IdentityError`` on missing canon.
  The install is structurally incomplete; the Autonomaton refuses
  to start.

* **Introspection helpers** (``_enumerate_mcps``, ``_enumerate_tiers``,
  ``_enumerate_slash_commands``, ``_cellar_status``) are
  **reporting surfaces**. They degrade to ``"(unavailable)"`` /
  ``"(none)"`` prose on read failures rather than breaking
  composition. The reasoning: a broken ``routing.config.yaml`` should
  not prevent the operator from starting a session to fix it.
  Introspection IS reporting, not governance.

The asymmetry is the right design but not implicit — document it
once at module top so the Sprint 24b reviewer (or future-you in six
months) sees why the same module has two failure styles.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Optional

from grove.identity import IdentityError

logger = logging.getLogger(__name__)


# ── Path resolution ────────────────────────────────────────────────────────


def _reference_dir() -> Path:
    """Return ``config/identity/`` in the repo — the first-run template source."""
    return Path(__file__).resolve().parent.parent / "config" / "identity"


# ── load_affordances ───────────────────────────────────────────────────────


def load_affordances(home: Path) -> Optional[str]:
    """Load the operator's affordances.md, seeding from reference on first run.

    Graceful tier (Sprint 23 D1): missing operator copy is seeded
    silently from ``config/identity/affordances.md`` and the operator
    gets a generic capability picture they can edit. Composition
    continues either way.

    Jidoka tier (Sprint 23 D1): missing REFERENCE template means the
    install is structurally incomplete — the system cannot seed an
    operator copy and has no affordances at all. Raise
    ``IdentityError`` to fail loud.

    Returns:
        The affordances content (stripped) or ``None`` if the
        operator's file exists but is empty (graceful — composition
        continues without).

    Raises:
        IdentityError: when the reference template is missing AND
            the operator copy does not exist or cannot be read.
    """
    op_path = Path(home) / "affordances.md"
    ref_path = _reference_dir() / "affordances.md"

    if op_path.exists():
        content = _read(op_path)
        if content:
            return content
        # Operator file exists but is empty / unreadable. Fall through
        # to seed-from-reference only if the reference is present.
        # If both fail, return None (graceful) — the operator's empty
        # file is their choice; we do not overwrite it from reference.
        logger.warning(
            "[affordances] %s exists but is empty/unreadable; "
            "composing without the affordances layer.",
            op_path,
        )
        return None

    # No operator copy. Need the reference template to seed.
    if not ref_path.exists():
        raise IdentityError(
            f"Affordances reference template is missing at {ref_path}. "
            f"The install is structurally incomplete — the Autonomaton "
            f"cannot seed an operator copy and has no affordances. "
            f"See https://the-grove.ai/standards/001"
        )

    # Seed operator copy from reference, then read.
    home.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(ref_path, op_path)
    except OSError as exc:
        logger.error(
            "[affordances] failed to seed %s from %s: %r",
            op_path, ref_path, exc,
        )
        # Seeding failed; fall through to reading the reference
        # directly so composition still gets affordances content.
        return _read(ref_path)
    logger.info("[affordances] seeded %s from %s", op_path, ref_path)
    return _read(op_path)


# ── introspect_capabilities ────────────────────────────────────────────────


def introspect_capabilities() -> str:
    """Build the live-capabilities prose block for the system prompt.

    Read-only. Never invokes an MCP server, never instantiates the
    Cognitive Router, never triggers a cellar rebuild. Every
    enumeration helper is defensive — when a source is missing or
    unreadable, the helper reports "(none)" / "(unavailable)" in the
    prose rather than raising. Sprint 23 D2.

    The output composes immediately after the static
    ``affordances.md`` block (D5 order). Orientation precedes state.
    """
    lines: list[str] = []
    lines.append("# Live Capabilities (this session)")
    lines.append("")
    lines.append(
        "The static `affordances.md` above gives semantic orientation. "
        "This block reports what's actually wired up right now."
    )
    lines.append("")

    # MCP servers
    lines.append("## Connected MCP servers")
    mcps = _enumerate_mcps()
    if mcps:
        for name, brief in mcps:
            lines.append(f"- **{name}** — {brief}")
    else:
        lines.append("- (none configured)")
    lines.append("")

    # Router tiers
    lines.append("## Cognitive Router tiers")
    tiers = _enumerate_tiers()
    if tiers:
        for tier_name, desc in tiers:
            lines.append(f"- **{tier_name}** — {desc}")
    else:
        lines.append("- (routing.config.yaml unavailable)")
    lines.append("")

    # Slash commands (grouped by category)
    lines.append("## Available slash commands")
    cmds_by_category = _enumerate_slash_commands()
    if cmds_by_category:
        for cat in sorted(cmds_by_category.keys()):
            verbs = ", ".join(f"/{name}" for name in cmds_by_category[cat])
            lines.append(f"- **{cat}**: {verbs}")
    else:
        lines.append("- (COMMAND_REGISTRY unavailable)")
    lines.append("")

    # Cellar status
    lines.append("## Cellar")
    lines.append(f"- {_cellar_status()}")

    return "\n".join(lines)


# ── Enumeration helpers (read-only, defensive) ────────────────────────────


def _enumerate_mcps() -> list[tuple[str, str]]:
    """List configured MCP servers from config.yaml.

    Read-only: walks the dict; never contacts the servers. Surfaces
    name + command + first few args; deliberately omits the ``env``
    block (may contain secrets like API tokens).

    Secrets discipline: ``mcp_servers.*.env`` (NOTION_TOKEN, API keys,
    OAuth tokens) must NOT enter the system prompt prose — the prompt
    is sent to the model on every turn and may surface in tool logs,
    UI panels, or shared traces. Surface name + command + args only.
    """
    try:
        from hermes_cli.config import load_config
        config = load_config()
        servers = config.get("mcp_servers") or {}
    except Exception as exc:
        logger.debug("[affordances] could not read mcp_servers: %r", exc)
        return []
    if not isinstance(servers, dict):
        return []

    result: list[tuple[str, str]] = []
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            continue
        cmd = str(cfg.get("command") or "?")
        args = cfg.get("args") or []
        if not isinstance(args, list):
            args = []
        brief = f"`{cmd}`"
        if args:
            shown = " ".join(repr(a) for a in args[:3])
            brief += f" {shown}"
            if len(args) > 3:
                brief += f" (+{len(args) - 3} more args)"
        result.append((str(name), brief))
    result.sort(key=lambda x: x[0])
    return result


def _enumerate_tiers() -> list[tuple[str, str]]:
    """Enumerate the four Cognitive Router tiers + their model bindings.

    Read-only: parses ``routing.config.yaml`` directly (operator copy
    preferred, falls back to repo template). Never instantiates
    ``CognitiveRouter`` — that would bind the classifier and emit
    telemetry, both unwanted side effects for introspection.
    """
    import yaml

    op_path = Path.home() / ".grove" / "routing.config.yaml"
    ref_path = (
        Path(__file__).resolve().parent.parent
        / "config" / "routing.config.yaml"
    )

    chosen: Optional[Path] = None
    if op_path.exists():
        chosen = op_path
    elif ref_path.exists():
        chosen = ref_path

    if chosen is None:
        return []

    try:
        with open(chosen) as fh:
            data = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.debug(
            "[affordances] could not parse %s: %r", chosen, exc
        )
        return []

    routing = data.get("routing") or {}
    tiers = routing.get("tier_preferences") or {}
    if not isinstance(tiers, dict):
        return []

    result: list[tuple[str, str]] = []
    for tier_name in sorted(tiers.keys()):
        cfg = tiers[tier_name] or {}
        if not isinstance(cfg, dict):
            continue
        handler = cfg.get("handler")
        if handler:
            desc = f"{handler} (handler)"
        else:
            provider = cfg.get("provider", "?")
            model = cfg.get("model", "?")
            desc = f"{model} ({provider})"
        result.append((str(tier_name), desc))
    return result


def _enumerate_slash_commands() -> dict[str, list[str]]:
    """Enumerate slash commands available in CLI sessions, grouped by category.

    Reads ``COMMAND_REGISTRY`` (a module-level list, no side effects to
    iterate). Filters out gateway-only commands since this introspection
    composes for direct-session use; a future broadcast-context
    introspection can override.
    """
    try:
        from hermes_cli.commands import COMMAND_REGISTRY
    except ImportError as exc:
        logger.debug("[affordances] COMMAND_REGISTRY not importable: %r", exc)
        return {}

    by_category: dict[str, list[str]] = defaultdict(list)
    for cmd in COMMAND_REGISTRY:
        if getattr(cmd, "gateway_only", False):
            continue
        category = getattr(cmd, "category", "Other") or "Other"
        by_category[category].append(getattr(cmd, "name", "?"))
    for cat in by_category:
        by_category[cat].sort()
    return dict(by_category)


def _cellar_status() -> str:
    """Report cellar index path + readiness + document count.

    Read-only: opens the sqlite db with ``mode=ro`` URI so a missing
    db is reported, never created. Doc count comes from
    ``cellar_meta`` (one row per indexed file). Defensive against any
    failure — returns a human-readable status string, never raises.
    """
    try:
        from grove.cellar import CellarIndex
    except ImportError as exc:
        return f"(cellar module unavailable: {exc!r})"
    try:
        idx = CellarIndex()
        path = idx.index_path
    except Exception as exc:
        return f"(cellar index path unavailable: {exc!r})"

    if not path.exists():
        return (
            f"path `{path}` — not yet indexed "
            f"(run `hermes index rebuild` to build)"
        )

    # Read-only count from cellar_meta.
    try:
        uri = f"file:{path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM cellar_meta"
            ).fetchone()
            count = int(row[0]) if row else 0
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        return f"path `{path}` — index file exists but unreadable ({exc!r})"
    except Exception as exc:
        return f"path `{path}` — status unavailable ({exc!r})"

    return f"path `{path}` — {count} document(s) indexed"


# ── Internals ──────────────────────────────────────────────────────────────


def _read(path: Path) -> Optional[str]:
    """Read a file; return stripped content, or None if empty/unreadable."""
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning("[affordances] could not read %s: %r", path, exc)
        return None
    return content or None
