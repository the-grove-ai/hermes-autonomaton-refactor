"""forge-unattended-publish-v1 P1 — the strict, fail-closed publication
authorization read. Phase 1 is INERT (no caller wires this yet); these tests
pin the deny-by-default primitive and its DELIBERATE divergence from the R-B1
read-resilient STATE merge: a corrupt overlay for the record DENIES, it never
resilient-falls-back to the definition value.

Hermetic: overlays live in a tmp state dir; the 'field absent' definition is the
real forge record with its publication block stripped into a tmp defs dir.

AUTHORIZING SET (documented). The overlay is parsed by ``yaml.safe_load``
(PyYAML 6.0.3, SafeLoader, YAML 1.1). Under YAML 1.1 the bare literals
``true|True|TRUE|yes|Yes|YES|on|On|ON`` all resolve to a real Python ``bool``
True — those (and ONLY those) authorize, because the read gates on ``is True``.
That is the accepted set: genuine booleans. The fail-closed concern is
string/int/list COERCION — ``"true"``, ``"yes"``, ``1``, ``[true]`` are NOT
Python bool True (and are rejected as non-bool by ``_read_state_file``), so they
DENY. The tests below pin exactly that boundary.
"""

from pathlib import Path

import pytest
import yaml

from grove.capability_registry import (
    _state_path_for_id,
    default_capabilities_dir,
    publication_unattended_authorized,
)

RID = "skill.fleet.forge-jobsearch"
_FORGE_SRC = default_capabilities_dir() / "skill__fleet__forge_jobsearch.yaml"


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    return d


def _write_overlay(state_dir: Path, body: str) -> Path:
    """Write a STATE overlay file at the record's canonical per-id path."""
    p = _state_path_for_id(RID, state_dir)
    p.write_text(body, encoding="utf-8")
    return p


@pytest.fixture
def defs_no_publication(tmp_path):
    """A hermetic definitions dir: the real forge record with governance.
    publication stripped — the pure 'field absent' definition."""
    doc = yaml.safe_load(_FORGE_SRC.read_text(encoding="utf-8"))
    (doc.get("governance") or {}).pop("publication", None)
    d = tmp_path / "defs"
    d.mkdir()
    (d / "forge.yaml").write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return d


def test_field_absent_returns_false(defs_no_publication, state_dir):
    # No overlay + the definition carries no publication.unattended → deny.
    assert (
        publication_unattended_authorized(
            RID, directory=defs_no_publication, state_dir=state_dir
        )
        is False
    )


def test_overlay_false_returns_false(state_dir):
    _write_overlay(state_dir, f"id: {RID}\npublication:\n  unattended: false\n")
    assert publication_unattended_authorized(RID, state_dir=state_dir) is False


def test_overlay_string_false_returns_false(state_dir):
    # A string "false" is not a real bool → _StateFileInvalid → DENY (config error),
    # never coerced to truthiness.
    _write_overlay(state_dir, f'id: {RID}\npublication:\n  unattended: "false"\n')
    assert publication_unattended_authorized(RID, state_dir=state_dir) is False


def test_overlay_true_returns_true(state_dir):
    _write_overlay(state_dir, f"id: {RID}\npublication:\n  unattended: true\n")
    assert publication_unattended_authorized(RID, state_dir=state_dir) is True


def test_corrupt_overlay_returns_false_not_resilient(state_dir):
    # Unparseable YAML for THIS record's file → DENY. The R-B1 merge would drop
    # state and keep the definition value; this strict read must NOT — deny only.
    _write_overlay(state_dir, "id: {unterminated flow mapping\n")
    assert publication_unattended_authorized(RID, state_dir=state_dir) is False


def test_no_overlay_file_returns_false(state_dir):
    # Empty state dir; the real forge definition ships unattended: false → deny.
    assert publication_unattended_authorized(RID, state_dir=state_dir) is False


# ── Gemini fail-closed vector #1: truthy type-coercion must DENY, never authorize.
# Each of these is truthy-in-Python but is NOT ``is True``; _read_state_file also
# rejects a non-bool unattended as _StateFileInvalid → the strict read denies.


def test_overlay_string_true_denies(state_dir):
    # A quoted "true" is a str, not the YAML-1.1 bare boolean → DENY.
    _write_overlay(state_dir, f'id: {RID}\npublication:\n  unattended: "true"\n')
    assert publication_unattended_authorized(RID, state_dir=state_dir) is False


def test_overlay_int_one_denies(state_dir):
    # 1 parses to int (isinstance(1, bool) is False), never coerced to True → DENY.
    _write_overlay(state_dir, f"id: {RID}\npublication:\n  unattended: 1\n")
    assert publication_unattended_authorized(RID, state_dir=state_dir) is False


def test_overlay_string_yes_denies(state_dir):
    # A quoted "yes" is a str, not the bare YAML-1.1 boolean → DENY.
    _write_overlay(state_dir, f'id: {RID}\npublication:\n  unattended: "yes"\n')
    assert publication_unattended_authorized(RID, state_dir=state_dir) is False


def test_overlay_list_denies(state_dir):
    # A list (or any non-bool container) → DENY.
    _write_overlay(state_dir, f"id: {RID}\npublication:\n  unattended: [true]\n")
    assert publication_unattended_authorized(RID, state_dir=state_dir) is False
