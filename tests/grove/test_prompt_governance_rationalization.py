"""prompt-governance-rationalization-v1 — the prompt no longer teaches the agent
governance architecture, so it stops confabulating governance refusals.

Scope (operator Ruling 3): the operator-owned identity bundle (constitution /
soul / register / affordances) is OUT of scope — it is the operator's declarative
governance, Jidoka-tier, edited by the operator, never by this sprint. These
tests cover only the editable ``prompt_builder.py`` / ``fs_utils.py`` surfaces.

The strip was deliberately surgical (rulings): ``grove_agent_help`` loses its
architecture-mechanism lines, ``SYSTEM_SELF_AWARENESS`` gains the four-sentence
act-don't-predict directive (F3 lubricant clause untouched), and
``GOVERNED_PATH_MESSAGE`` is rewritten to state the fact honestly without
reciting governance architecture or inventing a remediation command. The
un-scoped tool-guidance blocks (escalation/kanban/file-writing) were NOT touched
and keep their wording, so the composed-prompt scan uses the SPEC's exact
forbidden-phrase list, not an over-broad sweep.
"""
from __future__ import annotations

from agent.prompt_builder import (
    GROVE_AGENT_HELP_GUIDANCE as G,
    SYSTEM_SELF_AWARENESS as S,
)
from grove.utils.fs_utils import GOVERNED_PATH_MESSAGE
from grove.prompt import build_default_composer

# The F3 lubricant clause carries governance vocab BY DESIGN — a negative
# constraint telling the agent NEVER to say these words to the operator. KEEP
# (SPEC step 7). Vocabulary scans must exclude it.
_F3_CLAUSE = (
    "Never use terms like Andon, Dispatcher, sovereignty, zone, or "
    "execute_code when speaking to the operator."
)


# ── GROVE_AGENT_HELP: architecture mechanism stripped, directives kept ──


def test_grove_agent_help_architecture_mechanism_stripped():
    low = G.lower()
    for term in ("permission ledger", "dispatcher", "security zoning",
                 "structural boundaries", "governance configuration"):
        assert term not in low, term


def test_grove_agent_help_keeps_behavioral_directives():
    assert "You are an advisor" in G
    assert "you MUST emit the corresponding tool call immediately" in G
    assert "Do not warn the user about permissions, zones, or halts" in G
    assert "You act; the system governs" in G
    assert "granted workspace access" in G
    assert "Always call read_file" in G                # hotfix: absolute read directive
    assert "Never refuse a read based on the path" in G
    assert "skill_view" in G


# ── SYSTEM_SELF_AWARENESS: four-sentence directive added, F3 intact ──


def test_system_self_awareness_has_absolute_read_directive():
    # Hotfix: the "broad read" wording still invited the model to reason about
    # permissions and confabulate a refusal on a GRANTED workspace. Absolute
    # directive — always attempt the read; the enforcement layer is sole
    # authority (this stays true against the reject_governed_agent_read wall:
    # the agent attempts, the wall may block, the agent reports the result).
    assert "NEVER refuse a file read based on the path" in S
    assert "ALWAYS call read_file" in S
    assert "enforcement layer decides what is protected" in S
    assert "Always attempt the action" in S
    assert "never predict whether it will succeed" in S


def test_f3_lubricant_clause_and_hard_rule_preserved():
    # SPEC step 7 — F3 is KEEP, untouched; HARD RULE consolidated by prompt-dedup.
    assert _F3_CLAUSE in S
    assert "Paused is not failed" in S
    assert "HARD RULE" in S


def test_system_self_awareness_architecture_vocab_only_in_f3():
    # Strip the F3 clause; the remainder must carry NO architecture mechanism.
    remainder = S.replace(_F3_CLAUSE, "").lower()
    for term in ("sovereignty", "dispatcher", "execute_code",
                 "governance boundary", "five-stage pipeline"):
        assert term not in remainder, term


# ── GOVERNED_PATH_MESSAGE: honest, no architecture, no fictional command ──


def test_governed_path_message_rewritten_without_architecture():
    # secrets-only-wall-v1: the message now states the protected-secret fact and
    # affirms that everything else (incl. all of ~/.grove) is writable.
    low = GOVERNED_PATH_MESSAGE.lower()
    assert "is protected" in low
    assert "cannot be written" in low
    assert "do not attempt alternative write methods" in low
    # Removed: architecture framing + the SPEC's own confabulated command.
    assert "governance boundary" not in low
    assert "grant write" not in low
    assert "execute_code" not in low


# ── read_file schema: no governance / restriction language ──


def test_read_file_schema_has_no_governance_language():
    from tools.file_tools import READ_FILE_SCHEMA
    blob = (READ_FILE_SCHEMA["description"] + " "
            + str(READ_FILE_SCHEMA["parameters"])).lower()
    for term in ("governed", "governance", "sovereignty", "zone",
                 "permission", "restricted"):
        assert term not in blob, term


# ── Enforcement intact: governed write still blocked with the new message ──


def test_governed_write_still_blocked_with_new_message():
    import os
    import pytest
    from hermes_constants import get_hermes_home
    from agent.file_safety import reject_governed_agent_write
    # Build the path under the ACTIVE grove home (the grove suite isolates
    # GROVE_HOME to a tmp tree), so the check fires regardless of where home is.
    # A scope-defining surface is never a granted workspace (defense-in-depth),
    # so this is deterministically governed regardless of workspaces.yaml.
    # secrets-only-wall-v1: zones.schema.yaml is now non-secret (writable), so to
    # exercise the still-blocked wall we target a SECRET under the active grove
    # home (.env) — operator credentials remain protected with no approval path.
    governed = os.path.join(get_hermes_home(), ".env")
    with pytest.raises(PermissionError) as exc:
        reject_governed_agent_write(governed)
    assert str(exc.value) == GOVERNED_PATH_MESSAGE
    assert "is protected" in str(exc.value)


# ── Composed prompt: agent-architecture sections free of the SPEC's forbidden
#    phrases (identity bundle excluded per Ruling 3; F3 clause excluded) ──


def test_composed_editable_sections_free_of_forbidden_phrases():
    composer = build_default_composer(config=None)
    composed = composer.compose(
        valid_tool_names={"write_file", "read_file", "memory", "skill_view",
                          "approve_proposal", "escalate", "kanban_show"},
        model="", provider="", platform="cli", session_id="govrat",
        skip_context_files=True, load_soul_identity=False,
        memory_enabled=False, user_profile_enabled=False,
        pass_session_id=False, tier_context_blocks=None,
    )
    # Ruling 3: the operator-owned `identity` section carries declarative
    # sovereignty vocab BY DESIGN — out of scope, skip it. The SPEC's exact
    # forbidden-phrase list (NOT "dispatcher", which survives in un-scoped
    # escalation/kanban guidance).
    forbidden = ("sovereignty", "green zone", "yellow zone", "red zone",
                 "five-stage pipeline", "governance boundary")
    for label, text in composed.sections.items():
        if label == "identity":
            continue
        scan = text.replace(_F3_CLAUSE, "").lower()
        for term in forbidden:
            assert term not in scan, f"{term!r} leaked into section {label!r}"
