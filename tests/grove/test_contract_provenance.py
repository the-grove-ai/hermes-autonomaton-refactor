"""skill-adoption-v1 C5 — contract-execution provenance + strict primacy persist gate.

C5a: active_primary_skill_slug (the LOADED primary, persisted in context) rides the
tool_selection event alongside matched_skill_slugs (a FRESH per-turn match) — the two
diverge on a chat turn that keeps a prior skill's payload. C5b: the governed write
path emits contract_execution when an allowed write lands in the ACTIVE skill's
declared sink (system-derived only). C5c: the sanctioned persist path rejects a
primacy collision / out-of-subset claim.
"""

from __future__ import annotations

import copy
import os
import types

import pytest
import yaml

from grove import capability_registry as reg
from grove import utils
from grove.capability import Capability
from grove.capability_registry import CapabilityLoadError
from grove.dispatcher import Dispatcher
from grove.utils import fs_utils

REPO_CAPS = reg.default_capabilities_dir()
_BASE = yaml.safe_load(
    (REPO_CAPS / "skill__fleet__researcher.yaml").read_text(encoding="utf-8")
)

_RESEARCHER_GOV = [
    ("skill.fleet.researcher",
     {"write_zone": {"staging_dir": "researcher", "canonical_dir": "researcher"}}),
]


def _cap(*, cap_id, intents, primary, state="active"):
    d = copy.deepcopy(_BASE)
    d["id"] = cap_id
    d["trigger"]["intents"] = list(intents)
    if primary is None:
        d["trigger"].pop("primary_intents", None)
    else:
        d["trigger"]["primary_intents"] = list(primary)
    d["lifecycle"]["state"] = state
    return Capability.from_dict(d)


class _FakeLedger:
    def __init__(self):
        self.events = []

    def record(self, event_type, **fields):
        self.events.append((event_type, fields))
        return {}


def _write_intent(path, call_id="c1"):
    return types.SimpleNamespace(
        tool_name="write_file", arguments={"path": path, "content": "x"},
        call_id=call_id,
    )


# ── C5a — active vs matched divergence ───────────────────────────────────────


def test_active_and_matched_diverge_on_chat_turn(monkeypatch):
    d = Dispatcher.__new__(Dispatcher)
    d.registry = None
    monkeypatch.setattr(
        reg, "primary_skill_for_intent",
        lambda i: "researcher" if i == "research" else None,
    )
    # Prior research turn loaded researcher's payload; THIS turn is chat — the C3
    # tracker persists (a None-intent turn keeps the payload in context).
    d._last_loaded_primary_slug = "researcher"
    d._current_turn_classification = types.SimpleNamespace(intent_class="conversation")
    agent = types.SimpleNamespace(valid_tool_names={"invoke_skill"})
    matched = d._matched_skill_slugs_for_turn(agent)
    active = d._last_loaded_primary_slug
    assert matched == []            # fresh match on a chat turn — nothing
    assert active == "researcher"   # persisted payload still in context
    assert active not in matched    # the two fields carry distinct semantics


# ── C5b — contract_execution provenance ──────────────────────────────────────


def _shell(monkeypatch, *, active_slug):
    d = Dispatcher.__new__(Dispatcher)
    d._last_loaded_primary_slug = active_slug
    d._current_turn_id = "sess#7"
    d._current_turn_tool_invocations = []
    monkeypatch.setattr(d, "_fleet_governance", lambda: _RESEARCHER_GOV, raising=False)
    # Isolate from the base workspace policy — exercise the fleet-sink logic only.
    monkeypatch.setattr(fs_utils, "is_write_allowed", lambda *a, **k: True)
    return d


def _sink_path():
    from hermes_constants import get_hermes_home
    return os.path.join(str(get_hermes_home()), "researcher", "brief-x.json")


def test_contract_execution_fires_on_in_sink_write(monkeypatch):
    d = _shell(monkeypatch, active_slug="researcher")
    ledger = _FakeLedger()
    out = d._enforce_write_confinement([_write_intent(_sink_path())], None, ledger)
    assert out is None  # write allowed, batch proceeds
    ce = [e for e in ledger.events if e[0] == "contract_execution"]
    assert len(ce) == 1
    assert ce[0][1]["slug"] == "researcher"
    assert ce[0][1]["turn_id"] == "sess#7"
    assert ce[0][1]["path"] == _sink_path()


def test_no_contract_execution_on_ambient_write(monkeypatch):
    d = _shell(monkeypatch, active_slug="researcher")
    from hermes_constants import get_hermes_home
    ambient = os.path.join(str(get_hermes_home()), "notes", "scratch.txt")
    ledger = _FakeLedger()
    out = d._enforce_write_confinement([_write_intent(ambient)], None, ledger)
    assert out is None
    assert not [e for e in ledger.events if e[0] == "contract_execution"]


def test_no_contract_execution_when_no_payload_loaded(monkeypatch):
    d = _shell(monkeypatch, active_slug=None)  # no primary payload in context
    ledger = _FakeLedger()
    out = d._enforce_write_confinement([_write_intent(_sink_path())], None, ledger)
    assert out is None
    assert not [e for e in ledger.events if e[0] == "contract_execution"]


def test_staging_owner_slug_resolves_active_sink(monkeypatch):
    # The system-derived provenance primitive: the researcher sink maps to
    # "researcher"; an unrelated path maps to None.
    assert fs_utils.staging_owner_slug(_sink_path(), _RESEARCHER_GOV) == "researcher"
    from hermes_constants import get_hermes_home
    other = os.path.join(str(get_hermes_home()), "elsewhere", "x")
    assert fs_utils.staging_owner_slug(other, _RESEARCHER_GOV) is None


# ── C5c — strict primacy persist gate ────────────────────────────────────────


def test_assert_primacy_writable_passes_clean(tmp_path):
    cand = _cap(cap_id="skill.fleet.w", intents=["writing"], primary=["writing"])
    # No colliding record in an empty target dir → writable.
    reg.assert_primacy_writable(cand, directory=tmp_path, target=tmp_path)


def test_assert_primacy_writable_rejects_subset(tmp_path):
    cand = _cap(cap_id="skill.fleet.w", intents=["research"],
                primary=["research", "coding"])  # coding out-of-subset
    with pytest.raises(CapabilityLoadError, match="subset"):
        reg.assert_primacy_writable(cand, directory=tmp_path, target=tmp_path)


def test_assert_primacy_writable_rejects_collision(tmp_path):
    existing = _cap(cap_id="skill.fleet.x", intents=["research"], primary=["research"])
    (tmp_path / "skill__fleet__x.yaml").write_text(
        yaml.safe_dump(existing.to_dict(), sort_keys=False), encoding="utf-8")
    cand = _cap(cap_id="skill.fleet.y", intents=["research"], primary=["research"])
    with pytest.raises(CapabilityLoadError, match="collision"):
        reg.assert_primacy_writable(cand, directory=tmp_path, target=tmp_path)


def test_no_primary_short_circuits(tmp_path, monkeypatch):
    # A candidate with no primary claim never loads the registry (zero cost).
    called = {"n": 0}
    orig = reg.load_capabilities
    monkeypatch.setattr(reg, "load_capabilities",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or orig(*a, **k))
    cand = _cap(cap_id="skill.fleet.z", intents=["research"], primary=None)
    reg.assert_primacy_writable(cand, directory=tmp_path, target=tmp_path)
    assert called["n"] == 0  # short-circuited before any load


def test_mint_normal_path_is_inert(tmp_path):
    # A real mint (Trigger(always=True), no primary_intents) persists cleanly —
    # the wired gate never fires on the normal path.
    from grove.capability import LifecycleState, Provenance
    from grove.capability_registry import _mint_skill_record
    written = _mint_skill_record(
        "mintok", "productivity", "---\nname: mintok\n---\nbody\n",
        provenance=Provenance.INSTALLED, state=LifecycleState.MANAGED,
        filename_tag="installed", directory=tmp_path,
    )
    assert written is not None and written.exists()
