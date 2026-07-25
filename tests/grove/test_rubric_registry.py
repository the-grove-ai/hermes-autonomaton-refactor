"""artifact-review-v1 — rubric registry LOADER unit pins (stub files).

The house declarative-config idiom (absent → default silent, invalid → bare
ValueError loud, uncached) plus the R-14 content-hash immutability guard.
The REAL config/rubrics.yaml + records are guarded in
test_rubric_registry_integration.py; these use tmp stub files.
"""
from __future__ import annotations

import textwrap

import pytest

from grove.fleet.rubric_registry import (
    Criterion,
    compute_content_hash,
    load_rubric_registry,
)


def _valid_registry_text(threshold=0.8, definition="a criterion") -> str:
    crit = [Criterion(id="c1", type="llm", definition=definition)]
    digest = compute_content_hash(threshold, crit)
    return textwrap.dedent(
        f"""\
        version: 1
        rubrics:
          stub-class@1:
            default_threshold: {threshold}
            content_hash: "{digest}"
            criteria:
              - id: c1
                type: llm
                definition: "{definition}"
        """
    )


def _write(tmp_path, text):
    p = tmp_path / "rubrics.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_loads_valid_registry(tmp_path):
    reg = load_rubric_registry(_write(tmp_path, _valid_registry_text()))
    r = reg.get("stub-class@1")
    assert r is not None
    assert r.default_threshold == 0.8
    assert [c.id for c in r.criteria] == ["c1"]
    assert r.content_hash == compute_content_hash(
        0.8, [Criterion(id="c1", type="llm", definition="a criterion")]
    )


def test_absent_file_returns_empty_registry(tmp_path):
    reg = load_rubric_registry(tmp_path / "does_not_exist.yaml")
    assert reg.rubrics == {}


def test_absent_block_returns_empty_registry(tmp_path):
    reg = load_rubric_registry(_write(tmp_path, "version: 1\n"))
    assert reg.rubrics == {}


def test_content_hash_mismatch_fails_loud(tmp_path):
    # R-14 — tamper the definition but keep the old hash: an in-place edit of a
    # published version must fail loud, not load a different meaning under @1.
    text = _valid_registry_text(definition="TAMPERED criterion")
    good_hash = compute_content_hash(
        0.8, [Criterion(id="c1", type="llm", definition="a criterion")]
    )
    text = text.replace(
        f'content_hash: "{compute_content_hash(0.8, [Criterion(id="c1", type="llm", definition="TAMPERED criterion")])}"',
        f'content_hash: "{good_hash}"',
    )
    with pytest.raises(ValueError, match="content_hash mismatch"):
        load_rubric_registry(_write(tmp_path, text))


def test_missing_content_hash_fails_loud(tmp_path):
    text = textwrap.dedent(
        """\
        version: 1
        rubrics:
          stub-class@1:
            default_threshold: 0.8
            criteria:
              - id: c1
                type: llm
                definition: "a criterion"
        """
    )
    with pytest.raises(ValueError, match="content_hash"):
        load_rubric_registry(_write(tmp_path, text))


def test_unsupported_criterion_type_fails_loud(tmp_path):
    text = textwrap.dedent(
        """\
        version: 1
        rubrics:
          stub-class@1:
            default_threshold: 0.8
            content_hash: "x"
            criteria:
              - id: c1
                type: deterministic
                definition: "a criterion"
        """
    )
    with pytest.raises(ValueError, match="unsupported"):
        load_rubric_registry(_write(tmp_path, text))


def test_duplicate_criterion_ids_fail_loud(tmp_path):
    text = textwrap.dedent(
        """\
        version: 1
        rubrics:
          stub-class@1:
            default_threshold: 0.8
            content_hash: "x"
            criteria:
              - id: c1
                type: llm
                definition: "one"
              - id: c1
                type: llm
                definition: "two"
        """
    )
    with pytest.raises(ValueError, match="duplicate criterion id"):
        load_rubric_registry(_write(tmp_path, text))


def test_resolve_unknown_key_raises_keyerror(tmp_path):
    reg = load_rubric_registry(_write(tmp_path, _valid_registry_text()))
    with pytest.raises(KeyError, match="unknown rubric_ref"):
        reg.resolve("no-such-class@9")
