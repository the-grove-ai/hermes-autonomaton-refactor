"""write-routing-coherence-v1 fix-part-3 — set_publication_state: the sanctioned,
deploy-immune, repo-write-incapable writer for the publication-autonomy grant.

Pins the three SPEC guarantees plus the guards:
  (a) write → read round-trip with NO restart (the reader is live/lazy per record);
  (b) repo-write-incapable — the ONLY path written is under the state dir; the repo
      definition is byte-unchanged and no file is created beside it;
  (c) the id-form footgun is handled canonically — a hyphen record id resolves to
      the hyphen state filename (via the SAME _state_path_for_id the reader uses),
      NOT the underscore definition filename.
"""

from __future__ import annotations

import yaml
import pytest

from grove.capability_registry import (
    set_publication_state,
    publication_unattended_authorized,
    CapabilityLoadError,
    default_capabilities_dir,
    load_capabilities,
)

# The record id whose definition file uses underscores in the tail
# (skill__fleet__forge_jobsearch.yaml) but whose id uses a hyphen — the exact
# footgun from the misfire.
REC_ID = "skill.fleet.forge-jobsearch"
DEF_FILENAME = "skill__fleet__forge_jobsearch.yaml"       # dots→__, tail underscore
STATE_FILENAME = "skill__fleet__forge-jobsearch.yaml"     # dots→__, tail HYPHEN


@pytest.fixture
def dirs(tmp_path):
    """Isolated (definition_dir, state_dir). The definition carries only the id
    key — set_publication_state's existence check parses only ``id:``."""
    def_dir = tmp_path / "definitions"
    def_dir.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (def_dir / DEF_FILENAME).write_text(
        yaml.safe_dump({"id": REC_ID, "kind": "skill"}), encoding="utf-8"
    )
    return def_dir, state_dir


# ── (a) write → read round-trip, no restart ──────────────────────────────────
def test_write_read_round_trip_no_restart(dirs):
    def_dir, state_dir = dirs
    assert publication_unattended_authorized(
        REC_ID, directory=def_dir, state_dir=state_dir
    ) is False  # absent ≡ deny

    assert set_publication_state(
        REC_ID, True, directory=def_dir, state_dir=state_dir
    ) == "applied"
    assert publication_unattended_authorized(
        REC_ID, directory=def_dir, state_dir=state_dir
    ) is True  # live/lazy read — no process restart

    # revoke round-trips too
    assert set_publication_state(
        REC_ID, False, directory=def_dir, state_dir=state_dir
    ) == "applied"
    assert publication_unattended_authorized(
        REC_ID, directory=def_dir, state_dir=state_dir
    ) is False


# ── (b) repo-write-incapable ─────────────────────────────────────────────────
def test_repo_write_incapable(dirs):
    def_dir, state_dir = dirs
    def_file = def_dir / DEF_FILENAME
    before_bytes = def_file.read_bytes()
    before_listing = sorted(p.name for p in def_dir.iterdir())

    set_publication_state(REC_ID, True, directory=def_dir, state_dir=state_dir)

    # the definition is byte-identical and no sibling file was created next to it
    assert def_file.read_bytes() == before_bytes
    assert sorted(p.name for p in def_dir.iterdir()) == before_listing
    # the ONLY thing written lives under the state dir
    written = sorted(p.name for p in state_dir.iterdir())
    assert STATE_FILENAME in written
    assert all(not name.endswith(DEF_FILENAME) for name in written)


# ── (c) id-form footgun resolves canonically ─────────────────────────────────
def test_id_form_resolves_hyphen_state_filename(dirs):
    def_dir, state_dir = dirs
    set_publication_state(REC_ID, True, directory=def_dir, state_dir=state_dir)
    # the hyphen state filename (matching the reader), NOT the underscore def name
    assert (state_dir / STATE_FILENAME).is_file()
    assert not (state_dir / DEF_FILENAME).exists()
    # and the id INSIDE the file is authoritative
    doc = yaml.safe_load((state_dir / STATE_FILENAME).read_text())
    assert doc["id"] == REC_ID
    assert doc["publication"] == {"unattended": True}


# ── guards ───────────────────────────────────────────────────────────────────
def test_non_bool_value_rejected_no_coercion(dirs):
    # ANDON: a scalar grant is set, never coerced. bool is an int subclass, so
    # 1/0/"true" must be refused loud.
    def_dir, state_dir = dirs
    for bad in (1, 0, "true", None):
        with pytest.raises(ValueError):
            set_publication_state(REC_ID, bad, directory=def_dir, state_dir=state_dir)
    assert not (state_dir / STATE_FILENAME).exists()  # nothing written on refusal


def test_unknown_id_raises(dirs):
    def_dir, state_dir = dirs
    with pytest.raises(CapabilityLoadError):
        set_publication_state(
            "skill.fleet.does-not-exist", True, directory=def_dir, state_dir=state_dir
        )


def test_preserves_prior_state_keys(dirs):
    # A prior state file (e.g. a model_binding pin) must survive a publication grant.
    def_dir, state_dir = dirs
    (state_dir / STATE_FILENAME).write_text(
        yaml.safe_dump({"id": REC_ID, "model_binding": {"type": "model", "model": "z-ai/glm-5.2"}}),
        encoding="utf-8",
    )
    set_publication_state(REC_ID, True, directory=def_dir, state_dir=state_dir)
    doc = yaml.safe_load((state_dir / STATE_FILENAME).read_text())
    assert doc["model_binding"] == {"type": "model", "model": "z-ai/glm-5.2"}
    assert doc["publication"] == {"unattended": True}


# ── wiring: capability record + tool zone ────────────────────────────────────
def test_capability_record_admits_tool():
    caps = load_capabilities(directory=default_capabilities_dir())
    rec = caps.get("publication_grant_write")
    assert rec is not None
    assert getattr(rec.zone, "value", str(rec.zone)) == "yellow"
    assert "set_publication_state" in rec.bindings.tools


def test_tool_classifies_yellow():
    from grove.dispatcher import Dispatcher
    import grove.zones as zones
    zones.initialize()  # load the repo schema (incl. the new tool_zones entry)
    intent = type("I", (), {
        "tool_name": "set_publication_state",
        "arguments": {"record_id": REC_ID, "unattended": True},
    })()
    zr = Dispatcher._classify_one_intent(intent, None)
    assert zr.zone == "yellow"


def test_tool_wrapper_rejects_non_bool():
    from tools.publication_grant_tool import set_publication_state as tool
    out = tool(REC_ID, 1)  # int, not bool
    assert "boolean" in out.lower() or "error" in out.lower()
