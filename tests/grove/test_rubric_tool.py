"""artifact-review-v1 P5 — operator authoring tool pins (scripts/rubric_tool.py).

mint copies a version with the hash cleared; stamp fills missing hashes and
REFUSES to overwrite an existing one (the immutability guard); the output is
loader-valid.
"""
from __future__ import annotations

import importlib.util
import textwrap
from pathlib import Path

import pytest

from grove.fleet.rubric_registry import (
    Criterion,
    compute_content_hash,
    load_rubric_registry,
)

_TOOL_PATH = Path(__file__).resolve().parents[2] / "scripts" / "rubric_tool.py"
_spec = importlib.util.spec_from_file_location("rubric_tool", _TOOL_PATH)
rubric_tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rubric_tool)


_GOOD_HASH = compute_content_hash(
    0.8, [Criterion(id="c1", type="llm", definition="a criterion")]
)


def _write(tmp_path, *, with_hash: bool) -> Path:
    hash_line = f'    content_hash: "{_GOOD_HASH}"\n' if with_hash else ""
    p = tmp_path / "rubrics.yaml"
    p.write_text(
        "version: 1\nrubrics:\n  stub-class@1:\n    default_threshold: 0.8\n"
        + hash_line
        + '    criteria:\n      - id: c1\n        type: llm\n        definition: "a criterion"\n',
        encoding="utf-8",
    )
    return p


def test_stamp_fills_missing_hash(tmp_path):
    p = _write(tmp_path, with_hash=False)
    y, doc = rubric_tool._load(p)
    stamped = rubric_tool.stamp(doc)
    assert stamped == ["stub-class@1"]
    assert doc["rubrics"]["stub-class@1"]["content_hash"] == _GOOD_HASH


def test_stamp_refuses_to_overwrite_existing_hash(tmp_path):
    p = _write(tmp_path, with_hash=True)
    y, doc = rubric_tool._load(p)
    stamped = rubric_tool.stamp(doc)
    assert stamped == []  # nothing missing → nothing stamped
    assert doc["rubrics"]["stub-class@1"]["content_hash"] == _GOOD_HASH


def test_stamp_warns_but_leaves_mismatched_hash(tmp_path, capsys):
    p = tmp_path / "rubrics.yaml"
    p.write_text(
        'version: 1\nrubrics:\n  stub-class@1:\n    default_threshold: 0.8\n'
        '    content_hash: "deadbeef"\n'
        '    criteria:\n      - id: c1\n        type: llm\n        definition: "a criterion"\n',
        encoding="utf-8",
    )
    y, doc = rubric_tool._load(p)
    stamped = rubric_tool.stamp(doc)
    assert stamped == []
    # NOT overwritten — mint is the only path to change a published meaning.
    assert doc["rubrics"]["stub-class@1"]["content_hash"] == "deadbeef"
    assert "Refusing to overwrite" in capsys.readouterr().err


def test_mint_copies_and_clears_hash(tmp_path):
    p = _write(tmp_path, with_hash=True)
    y, doc = rubric_tool._load(p)
    rubric_tool.mint(doc, "stub-class@1", "stub-class@2")
    new = doc["rubrics"]["stub-class@2"]
    assert "content_hash" not in new
    assert [c["id"] for c in new["criteria"]] == ["c1"]


def test_mint_rejects_existing_and_missing(tmp_path):
    p = _write(tmp_path, with_hash=True)
    y, doc = rubric_tool._load(p)
    with pytest.raises(KeyError, match="already exists"):
        rubric_tool.mint(doc, "stub-class@1", "stub-class@1")
    with pytest.raises(KeyError, match="not in"):
        rubric_tool.mint(doc, "no-such@9", "new@1")


def test_mint_then_stamp_is_loader_valid(tmp_path):
    """The full authoring path: mint a new version, stamp it, and the loader
    accepts the result (hash verifies)."""
    p = _write(tmp_path, with_hash=True)
    y, doc = rubric_tool._load(p)
    rubric_tool.mint(doc, "stub-class@1", "stub-class@2")
    rubric_tool.stamp(doc)
    rubric_tool._save(y, doc, p)
    reg = load_rubric_registry(p)  # would raise on a hash mismatch
    assert reg.get("stub-class@2") is not None
    assert reg.get("stub-class@1") is not None
