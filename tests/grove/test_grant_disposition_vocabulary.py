"""sovereign-disposition-hotfix-v1 — grant disposition vocabulary alignment.

The grant-persistence layer once wrote the disposition token ``"standing"``,
which the execution gate (``grove/dispatcher.py`` Allow-branch, the
``disposition not in ("once","session","always")`` check) rejects. A persisted
"Always" grant replayed on a follow-up turn therefore raised ``ValueError`` at
the gate. ``"standing"`` was a write-only orphan: nothing consumed it.

These tests pin the alignment — both mint paths and the loader default now emit
``"always"`` (the gate-accepted token), a minted grant survives replay through
``_resolve_governance_grant`` into the gate, and no source file re-binds the
disposition field to the orphan token. The ``source="standing"`` provenance
field is a DIFFERENT axis and is deliberately left untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from grove.dispatcher import AndonHalt, Dispatcher
from grove.grant_recognition import GrantToken
from grove.intents import ToolIntent
from grove.zones import ZoneResult

# The execution gate's accepted set (grove/dispatcher.py Allow-branch). A grant
# whose disposition is outside this set raises at replay.
_GATE_ACCEPTED = ("once", "session", "always")


def _audio_transcriber_halt() -> AndonHalt:
    """A native governance-mutation halt shaped like the real audio-transcriber
    Always grant: ``andon_promote`` on ``skill_name=audio-transcriber`` resolves
    to the standing-grant store (scope=audio-transcriber, write_class=andon_promote).
    """
    intents = [ToolIntent(
        tool_name="andon_promote",
        arguments={"skill_name": "audio-transcriber"},
        call_id="c1",
    )]
    zr = [ZoneResult(zone="red", matched_rule="governance", source="default")]
    return AndonHalt(intents=intents, zone_results=zr, triggering_index=0)


@pytest.fixture
def tmp_grant_store(monkeypatch, tmp_path: Path):
    """A GrantStore bound to a tmp grants.yaml, installed as the process store
    for both dispatcher mint and replay call sites (both import get_grant_store
    from grove.grants at call time)."""
    import grove.grants as _grants

    store = _grants.GrantStore(grants_path=tmp_path / "grants.yaml")
    monkeypatch.setattr(_grants, "get_grant_store", lambda *a, **k: store)
    return store


# ── mint path 1: GrantStore.add_standing_grant source-normalization ──────────

def test_add_standing_grant_normalizes_nonstanding_source_to_always(tmp_path: Path):
    """A grant arriving with a non-'standing' source is normalized to a standing
    grant; the disposition it is stored with is the gate-accepted 'always', and
    the provenance axis (source) is set to 'standing' — NOT conflated."""
    import grove.grants as _grants

    store = _grants.GrantStore(grants_path=tmp_path / "grants.yaml")
    store.add_standing_grant(GrantToken(
        source="operator_telegram",   # non-'standing' → triggers normalization
        scope="audio-transcriber",
        write_class="andon_promote",
        disposition="once",
        authorized_by="operator",
    ))
    stored = store.get_grant("audio-transcriber", "andon_promote")
    assert stored is not None
    assert stored.disposition == "always"       # gate-accepted, not the orphan token
    assert stored.source == "standing"          # provenance axis preserved (HR2)


# ── mint path 2: dispatcher _add_standing_grant_from_halt ────────────────────

def test_dispatcher_mint_stores_always_disposition(tmp_grant_store):
    """The operator-'Always' mint path persists 'always', not 'standing'."""
    d = Dispatcher(sovereign_prompt_handler=lambda halt: "always")
    d._add_standing_grant_from_halt(_audio_transcriber_halt())

    stored = tmp_grant_store.get_grant("audio-transcriber", "andon_promote")
    assert stored is not None
    assert stored.disposition == "always"


# ── loader default ───────────────────────────────────────────────────────────

def test_loader_defaults_missing_disposition_to_always(tmp_path: Path):
    """A persisted record with no disposition field loads as 'always' (gate-
    accepted), not the orphan 'standing'."""
    import grove.grants as _grants

    grants_yaml = tmp_path / "grants.yaml"
    grants_yaml.write_text(
        "schema_version: '1.0'\n"
        "grants:\n"
        "- id: grant-nodispo\n"
        "  source: standing\n"
        "  scope: audio-transcriber\n"
        "  write_class: andon_promote\n"
        "  issued_at: '2026-07-18T22:55:22+00:00'\n"
        "  authorized_by: sovereignty_prompt\n"
        "  revoked: false\n",
        encoding="utf-8",
    )
    loaded = _grants.GrantStore(grants_path=grants_yaml).get_grant(
        "audio-transcriber", "andon_promote",
    )
    assert loaded is not None
    assert loaded.disposition == "always"


# ── REGRESSION: mint → persist → replay → execution gate ─────────────────────

def test_minted_grant_replays_through_execution_gate(tmp_grant_store):
    """The test that was missing: a persisted Always grant, resolved on a
    follow-up turn via _resolve_governance_grant, carries a disposition the
    execution gate accepts — so replay no longer raises. Pre-fix the mint stored
    'standing' and this assertion (and the live gate) failed."""
    d = Dispatcher(sovereign_prompt_handler=lambda halt: "always")
    halt = _audio_transcriber_halt()

    # Turn N — operator taps "Always": mint + persist.
    d._add_standing_grant_from_halt(halt)

    # Turn N+1 — same halt, no new UI: the stored grant is resolved back...
    resolved = d._resolve_governance_grant(halt)
    assert resolved is not None
    # ...and its disposition passes the Allow-branch gate (would ValueError otherwise).
    assert resolved.disposition in _GATE_ACCEPTED


# ── invariant guard: the orphan token is not re-bound anywhere ───────────────

def test_no_source_binds_disposition_to_standing():
    """No file under grove/ assigns the disposition field the orphan 'standing'
    token (in either quote style). Guards against reintroduction. The
    source='standing' provenance value is a different axis and is not matched
    (it never shares a line with 'disposition')."""
    grove_root = Path(__file__).resolve().parents[2] / "grove"
    offenders = []
    for py in grove_root.rglob("*.py"):
        for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if "disposition" in line and ('"standing"' in line or "'standing'" in line):
                offenders.append(f"{py.relative_to(grove_root)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "disposition still bound to the orphan 'standing' token:\n" + "\n".join(offenders)
    )
