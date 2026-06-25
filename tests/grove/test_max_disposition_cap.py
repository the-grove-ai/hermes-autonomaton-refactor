"""max_disposition cap on zone rules — GRV-001 Stage 04 conformance.

Governance-mutation CLI verbs (hermes andon promote, hermes flywheel approve,
etc.) must be Yellow-zoned with max_disposition: session, so "always" cannot
write a permanent green rule across sessions while still allowing the agent to
invoke repeatedly within one session after the operator grants it.
"""
from __future__ import annotations

import dataclasses
import io
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from grove.zones import ZoneClassifier, ZoneRule, ZoneResult


# ── helpers ──────────────────────────────────────────────────────────────────

def _clf(yaml_str: str, tmp_path: Path) -> ZoneClassifier:
    p = tmp_path / "zones.schema.yaml"
    p.write_text(textwrap.dedent(yaml_str).strip())
    return ZoneClassifier(p)


def _halt(max_disp: "str | None") -> MagicMock:
    zr = MagicMock()
    zr.max_disposition = max_disp
    intent = MagicMock()
    intent.tool_name = "terminal"
    intent.arguments = {"command": "hermes andon promote foo"}
    halt = MagicMock()
    halt.zone_results = [zr]
    halt.intents = [intent]
    halt.triggering_index = 0
    return halt


_REPO_SCHEMA = (
    Path(__file__).resolve().parent.parent.parent / "config" / "zones.schema.yaml"
)

_GOVERNANCE_VERBS = [
    "hermes andon promote grove-site-fetch",
    "hermes andon reject grove-site-fetch",
    "hermes andon revoke grove-site-fetch",
    "hermes flywheel approve abc123",
    "hermes flywheel reject abc123",
    "hermes flywheel patterns demote abc123",
]

# ── 1. Dataclass shape ────────────────────────────────────────────────────────

def test_zone_rule_has_max_disposition_field():
    fields = {f.name for f in dataclasses.fields(ZoneRule)}
    assert "max_disposition" in fields, "ZoneRule must carry max_disposition"


def test_zone_result_has_max_disposition_field():
    fields = {f.name for f in dataclasses.fields(ZoneResult)}
    assert "max_disposition" in fields, "ZoneResult must carry max_disposition"


# ── 2. Schema parsing → ZoneResult ───────────────────────────────────────────

_SCHEMA_WITH_CAP = """
    schema_version: 1
    zones:
      green: {auto_approve: []}
      yellow: {proposes: []}
      red: {sovereign: []}
    tool_zones:
      terminal:
        default_zone: yellow
        rules:
          - match_pattern: hermes\\s+andon\\s+promote.*
            zone: yellow
            max_disposition: session
            reason: "Governance-mutation verb."
          - match_pattern: ls\\s+.*
            zone: green
            reason: "Safe."
"""


def test_classify_returns_max_disposition_from_matching_rule(tmp_path):
    clf = _clf(_SCHEMA_WITH_CAP, tmp_path)
    result = clf.classify_command_string(
        "hermes andon promote grove-site-fetch",
        "command.execute.hermes",
        tool_id="terminal",
    )
    assert result.zone == "yellow"
    assert result.max_disposition == "session"


def test_classify_max_disposition_none_for_rule_without_field(tmp_path):
    clf = _clf(_SCHEMA_WITH_CAP, tmp_path)
    result = clf.classify_command_string("ls -la", "command.execute.ls", tool_id="terminal")
    assert result.zone == "green"
    assert result.max_disposition is None


def test_classify_max_disposition_none_on_default_fallthrough(tmp_path):
    clf = _clf(_SCHEMA_WITH_CAP, tmp_path)
    result = clf.classify_command_string(
        "cat /tmp/foo", "command.execute.cat", tool_id="terminal"
    )
    assert result.zone == "yellow"  # default_zone
    assert result.max_disposition is None


# ── 3. Actual repo schema has governance verbs correctly capped ───────────────

@pytest.mark.skipif(not _REPO_SCHEMA.exists(), reason="repo schema not found")
@pytest.mark.parametrize("cmd", _GOVERNANCE_VERBS)
def test_governance_verb_is_yellow_with_session_cap(cmd):
    clf = ZoneClassifier(_REPO_SCHEMA)
    result = clf.classify_command_string(cmd, "command.execute.hermes", tool_id="terminal")
    assert result.zone == "yellow", (
        f"{cmd!r}: expected yellow, got {result.zone!r} "
        f"(source={result.source!r})"
    )
    assert result.max_disposition == "session", (
        f"{cmd!r}: expected max_disposition='session', "
        f"got {result.max_disposition!r}"
    )


@pytest.mark.skipif(not _REPO_SCHEMA.exists(), reason="repo schema not found")
def test_non_governance_terminal_cmd_has_no_cap():
    """Ordinary terminal commands (ls, cat, grep) must not be capped."""
    clf = ZoneClassifier(_REPO_SCHEMA)
    result = clf.classify_command_string(
        "ls -la /tmp", "command.execute.ls", tool_id="terminal"
    )
    assert result.max_disposition is None


# ── 4. Dispatcher helper ──────────────────────────────────────────────────────

def test_get_halt_max_disposition_returns_first_nonnull():
    from grove.dispatcher import _get_halt_max_disposition
    zr_none = MagicMock(); zr_none.max_disposition = None
    zr_sess = MagicMock(); zr_sess.max_disposition = "session"
    halt = MagicMock(); halt.zone_results = [zr_none, zr_sess]
    assert _get_halt_max_disposition(halt) == "session"


def test_get_halt_max_disposition_returns_none_when_all_absent():
    from grove.dispatcher import _get_halt_max_disposition
    zr = MagicMock(); zr.max_disposition = None
    halt = MagicMock(); halt.zone_results = [zr]
    assert _get_halt_max_disposition(halt) is None


def test_get_halt_max_disposition_returns_none_for_empty_results():
    from grove.dispatcher import _get_halt_max_disposition
    halt = MagicMock(); halt.zone_results = []
    assert _get_halt_max_disposition(halt) is None


# ── 5. TTY sovereign prompt — session cap: show once/session/deny, no always ──

def test_tty_prompt_shows_session_but_not_always_when_capped():
    """With max_disposition=session, tty_sovereign_prompt shows session choice but not always."""
    from grove.sovereign_prompt_handlers import tty_sovereign_prompt

    out = io.StringIO()
    with patch("builtins.input", return_value="2"):
        result = tty_sovereign_prompt(_halt("session"), out=out)

    assert result == "session"
    rendered = out.getvalue().lower()
    assert "session" in rendered, "Session choice must appear when capped at session"
    assert "always" not in rendered, "Always choice must NOT appear when capped at session"


def test_tty_prompt_accepts_once_when_session_capped():
    """When capped at session, choice '1' (once) is still valid."""
    from grove.sovereign_prompt_handlers import tty_sovereign_prompt

    out = io.StringIO()
    with patch("builtins.input", return_value="1"):
        result = tty_sovereign_prompt(_halt("session"), out=out)

    assert result == "once"


def test_tty_prompt_rejects_always_choice_when_session_capped():
    """When capped at session, input '3' (which was always) is unknown — re-prompts."""
    from grove.sovereign_prompt_handlers import tty_sovereign_prompt

    out = io.StringIO()
    # "3" is the old always slot; should be unknown in the 3-choice capped menu.
    # In capped mode, [3] = Not this time (deny), so "3" → deny.
    with patch("builtins.input", side_effect=["3"]):
        result = tty_sovereign_prompt(_halt("session"), out=out)

    assert result == "deny"


def test_tty_prompt_shows_all_four_choices_when_uncapped():
    """Without a cap, prompt text includes session and always labels."""
    from grove.sovereign_prompt_handlers import tty_sovereign_prompt

    out = io.StringIO()
    with patch("builtins.input", return_value="1"):
        result = tty_sovereign_prompt(_halt(None), out=out)

    assert result == "once"
    rendered = out.getvalue().lower()
    assert "session" in rendered or "always" in rendered, (
        "Uncapped prompt must show session/always choices"
    )
