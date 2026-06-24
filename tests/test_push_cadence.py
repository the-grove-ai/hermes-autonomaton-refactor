"""kaizen-push-cadence-v1.1 — rebuild-simulating acceptance tests.

v1 shipped a cooldown that was inert in the gateway: the gateway rebuilds the
AIAgent on nearly every turn (per-turn enabled_toolsets busts the agent cache
signature), wiping the ephemeral _last_push_turn / _surfaced_proposal_ids. v1's
test missed it because it carried state across iterations in a Python variable —
silently encoding the very assumption that fails.

These tests carry NO Python state between simulated turns. The ONLY thing that
persists is the session-scoped .push_cadence.json file, exercised through the
REAL helpers. If persistence regresses, T1/T2 fail.
"""
import pytest

from tools.flywheel_review_tool import _read_push_cadence, _write_push_cadence

# Mirrors AIAgent._PUSH_COOLDOWN_TURNS.
N = 3


def _guard_suppresses(turn: int, last, n: int = N) -> bool:
    """The exact predicate from _append_pending_offer's cooldown guard."""
    return last is not None and (turn - last) < n


@pytest.fixture
def grove_home(tmp_path, monkeypatch):
    # get_hermes_home() honors GROVE_HOME; redirect the cadence file to a temp dir.
    monkeypatch.setenv("GROVE_HOME", str(tmp_path))
    return tmp_path


def test_t1_cooldown_survives_agent_rebuild(grove_home):
    """The exact case the live transcript failed: note on turn 1, then quiet.

    Each loop iteration is a FRESH agent — it reads cadence ONLY from the file,
    never from a carried-over variable. This is what v1's test could not do.
    """
    S = "sess-A"
    actions = []
    for turn in range(1, 5):
        cad = _read_push_cadence(S)  # fresh read — simulates a rebuilt agent
        if _guard_suppresses(turn, cad["last_push_turn"]):
            actions.append((turn, "SUPPRESS"))
            continue
        surfaced = cad["surfaced_ids"]
        surfaced.add(f"prop-{turn}")
        _write_push_cadence(
            S, last_push_turn=turn,
            surfaced_ids=surfaced,
            surfaced_connectors=cad["surfaced_connectors"],
        )
        actions.append((turn, "PUSH"))
    assert actions == [(1, "PUSH"), (2, "SUPPRESS"), (3, "SUPPRESS"), (4, "PUSH")]


def test_t2_dedup_survives_rebuild(grove_home):
    """A surfaced proposal stays deduped on later turns via the persisted set."""
    S = "sess-B"
    cad = _read_push_cadence(S)
    cad["surfaced_ids"].add("prop-X")
    _write_push_cadence(
        S, last_push_turn=1, surfaced_ids=cad["surfaced_ids"],
        surfaced_connectors=set(),
    )
    # Fresh read on a later turn — prop-X must still be excluded.
    assert "prop-X" in _read_push_cadence(S)["surfaced_ids"]


def test_t3_session_boundary_resets(grove_home):
    """A record from another session reads as empty — /new resets cadence."""
    _write_push_cadence(
        "sess-OLD", last_push_turn=5, surfaced_ids={"p1"},
        surfaced_connectors={"connector_failure:abc"},
    )
    cad_new = _read_push_cadence("sess-NEW")
    assert cad_new["last_push_turn"] is None
    assert cad_new["surfaced_ids"] == set()
    assert cad_new["surfaced_connectors"] == set()


def test_t4_fail_soft_unwritable(grove_home, monkeypatch):
    """A persistence failure never raises and degrades toward may-surface."""
    bad = grove_home / "this_is_a_file"
    bad.write_text("x")  # GROVE_HOME now points at a FILE — mkdir/write will fail
    monkeypatch.setenv("GROVE_HOME", str(bad))
    # Must not raise.
    _write_push_cadence(
        "s", last_push_turn=1, surfaced_ids=set(), surfaced_connectors=set(),
    )
    cad = _read_push_cadence("s")
    # Fail-open: empty cadence → cooldown guard sees last=None → push proceeds.
    assert cad["last_push_turn"] is None
    assert cad["surfaced_ids"] == set()


def test_t5_connector_field_preserved_by_proposal_write(grove_home):
    """The proposal path must not clobber surfaced_connectors (separate surface)."""
    S = "sess-C"
    _write_push_cadence(
        S, last_push_turn=1, surfaced_ids=set(),
        surfaced_connectors={"connector_failure:deadbeef"},
    )
    cad = _read_push_cadence(S)
    # Simulate a proposal push preserving connectors as the real code does.
    _write_push_cadence(
        S, last_push_turn=2, surfaced_ids={"prop-Y"},
        surfaced_connectors=cad["surfaced_connectors"],
    )
    after = _read_push_cadence(S)
    assert after["surfaced_connectors"] == {"connector_failure:deadbeef"}
    assert after["surfaced_ids"] == {"prop-Y"}
