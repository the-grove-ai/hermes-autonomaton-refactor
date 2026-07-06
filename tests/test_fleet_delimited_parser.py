"""Phase-1 tests: the parameterized delimited emit PARSER (forge-fleet-package-emission-v1).

The forge fleet worker used to hand-author a single JSON blob as its final message;
a résumé body carrying an unescaped ``"`` (e.g. ``"god-object"``) produced invalid
JSON that no parser tolerance could rescue (byte-confirmed on runs f157eb558b and 5
siblings — all complete, none truncated). Path B replaces the JSON contract with a
delimited, sentinel-framed, per-file emit parsed by a strict line-by-line state
machine (NO regex). This file exercises that parser in ISOLATION — synthetic
delimited text, no prompt, no model.

Contract under test — ``_extract_fleet_package(messages, tag, sink, required_files)``
returns ``(package{"slug","files"} | None, reason)``:
  * clean multi-file parse (slug recovered from meta.json's body);
  * every malformed transition → ``(None, <reason>)`` — never a partial/garbled stage;
  * per-run ``tag`` (caller passes ``run_id[:8]``) frames the sentinels so a body line
    that spoofs a marker with the WRONG tag is treated as text, not a marker.
"""

from __future__ import annotations

import json
import os

from grove.fleet import worker_entry


# ── helpers ──────────────────────────────────────────────────────────────────

TAG = "f157eb55"  # a short-hex per-run tag (run_id[:8])
REQUIRED = {"resume.md", "cover-letter.md", "meta.json"}


def _block(name: str, body: str, tag: str = TAG) -> str:
    return f"@@@FILE_START: {name} [{tag}]@@@\n{body}\n@@@FILE_END: {name} [{tag}]@@@"


def _meta(slug: str = "260706-acme-vp") -> str:
    return json.dumps(
        {"row_id": "row-123", "company": "Acme", "role": "VP", "slug": slug}
    )


def _clean_text(tag: str = TAG, slug: str = "260706-acme-vp") -> str:
    return "\n".join(
        [
            _block("resume.md", "# Jane Doe\nBuilt platforms.", tag),
            _block("cover-letter.md", "Dear Acme,\n\nStrong fit.\n\nJane", tag),
            _block("meta.json", _meta(slug), tag),
        ]
    )


def _msgs(text: str):
    return [{"role": "assistant", "content": text}]


def _run(text: str, sink="/tmp/sink"):
    return worker_entry._extract_fleet_package(_msgs(text), TAG, sink, REQUIRED)


def _reason(text: str) -> str:
    pkg, reason = _run(text)
    assert pkg is None, f"expected fail-loud, got a package: {pkg!r}"
    return reason.split(":", 1)[0]


# ── clean parse ──────────────────────────────────────────────────────────────


def test_clean_three_file_parse_recovers_slug_from_meta():
    pkg, reason = _run(_clean_text())
    assert reason is None
    assert pkg["slug"] == "260706-acme-vp"          # slug from meta.json body, not a top-level field
    assert set(pkg["files"]) == REQUIRED
    assert pkg["files"]["resume.md"] == "# Jane Doe\nBuilt platforms."


def test_clean_parse_ignores_prose_outside_blocks():
    # The founding failure had "I have everything needed. Building the fleet package
    # now.\n\nRow: …" preamble. Prose OUTSIDE any block is structurally ignored.
    text = "I have everything needed. Building the fleet package now.\n\n" + _clean_text()
    pkg, reason = _run(text)
    assert reason is None and pkg["slug"] == "260706-acme-vp"


def test_clean_parse_survives_unescaped_quotes_in_body():
    # THE regression the sprint exists to kill: a body full of literal double-quotes
    # and raw newlines is transported verbatim — no JSON escaping, so no parse break.
    quoted = 'Re-architected the harness, removing a "god-object" defect.\nGhost-authored "Lean AI".'
    text = "\n".join(
        [
            _block("resume.md", quoted),
            _block("cover-letter.md", 'She said "hi".'),
            _block("meta.json", _meta()),
        ]
    )
    pkg, reason = _run(text)
    assert reason is None
    assert pkg["files"]["resume.md"] == quoted     # bytes preserved, quotes and all


# ── fail-loud transitions (state machine) ────────────────────────────────────


def test_empty_filename_fails_loud():
    text = "\n".join([f"@@@FILE_START:  [{TAG}]@@@", "body", f"@@@FILE_END:  [{TAG}]@@@"])
    assert _reason(text) == "empty-filename"


def test_duplicate_filename_fails_loud():
    text = "\n".join([_block("resume.md", "a"), _block("resume.md", "b"), _block("meta.json", _meta())])
    assert _reason(text) == "duplicate-file"


def test_end_without_start_fails_loud():
    text = f"@@@FILE_END: resume.md [{TAG}]@@@"
    assert _reason(text) == "end-without-start"


def test_start_before_end_is_missing_end():
    # another START opens while resume.md is still open (its END never arrived)
    text = "\n".join(
        [f"@@@FILE_START: resume.md [{TAG}]@@@", "body", f"@@@FILE_START: cover-letter.md [{TAG}]@@@", "b2", f"@@@FILE_END: cover-letter.md [{TAG}]@@@"]
    )
    assert _reason(text) == "missing-end"


def test_mismatched_end_name_fails_loud():
    text = "\n".join([f"@@@FILE_START: resume.md [{TAG}]@@@", "body", f"@@@FILE_END: cover-letter.md [{TAG}]@@@"])
    assert _reason(text) == "mismatched-end"


def test_empty_body_fails_loud():
    text = "\n".join([f"@@@FILE_START: resume.md [{TAG}]@@@", "   ", f"@@@FILE_END: resume.md [{TAG}]@@@"])
    assert _reason(text) == "empty-body"


def test_unterminated_file_at_eof_fails_loud():
    text = f"@@@FILE_START: resume.md [{TAG}]@@@\nbody with no END"
    assert _reason(text) == "unterminated"


def test_no_files_fails_loud():
    assert _reason("just prose, no blocks at all") == "no-files"


def test_unsafe_filename_fails_loud():
    text = _block("../../etc/passwd", "pwned")
    assert _reason(text) == "unsafe-filename"


def test_unsafe_filename_dotdot_fails_loud():
    text = _block("..", "x")
    assert _reason(text) == "unsafe-filename"


def test_missing_required_files_fails_loud():
    # only resume.md + meta.json; cover-letter.md absent
    text = "\n".join([_block("resume.md", "a"), _block("meta.json", _meta())])
    assert _reason(text) == "missing-required-files"


def test_bad_meta_invalid_json_fails_loud():
    text = "\n".join(
        [_block("resume.md", "a"), _block("cover-letter.md", "b"), _block("meta.json", "{ not valid json")]
    )
    assert _reason(text) == "bad-meta"


def test_bad_meta_missing_slug_fails_loud():
    meta = json.dumps({"row_id": "r", "company": "Acme", "role": "VP"})  # no slug key
    text = "\n".join([_block("resume.md", "a"), _block("cover-letter.md", "b"), _block("meta.json", meta)])
    assert _reason(text) == "bad-meta"


def test_bad_meta_invalid_slug_fails_loud():
    text = "\n".join(
        [_block("resume.md", "a"), _block("cover-letter.md", "b"), _block("meta.json", _meta(slug="Not A Slug"))]
    )
    assert _reason(text) == "bad-meta"


# ── fence stripping ──────────────────────────────────────────────────────────


def test_fence_strip_markdown_wrapped_body():
    body = "```markdown\n# Jane Doe\nBuilt platforms.\n```"
    text = "\n".join([_block("resume.md", body), _block("cover-letter.md", "c"), _block("meta.json", _meta())])
    pkg, reason = _run(text)
    assert reason is None
    assert pkg["files"]["resume.md"] == "# Jane Doe\nBuilt platforms."   # fences stripped


def test_fence_strip_bare_and_text_variants():
    for fence in ("```", "```text", "```json"):
        body = f"{fence}\ncontent line\n```"
        text = "\n".join([_block("resume.md", body), _block("cover-letter.md", "c"), _block("meta.json", _meta())])
        pkg, reason = _run(text)
        assert reason is None, f"fence {fence!r} not stripped: {reason}"
        assert pkg["files"]["resume.md"] == "content line"


# ── per-run tag: collision resistance ────────────────────────────────────────


def test_wrong_tag_marker_in_body_is_text_not_a_marker():
    # A résumé that literally contains a sentinel-looking line with the WRONG tag must
    # NOT be honored as a marker — it is body text, and the real blocks still parse.
    spoof = f"@@@FILE_START: evil.md [deadbeef]@@@"
    body = f"# Jane Doe\n{spoof}\nMore prose."
    text = "\n".join([_block("resume.md", body), _block("cover-letter.md", "c"), _block("meta.json", _meta())])
    pkg, reason = _run(text)
    assert reason is None
    assert spoof in pkg["files"]["resume.md"]        # the spoof line survived as text


def test_same_line_start_and_fence_is_not_a_start():
    # START prefix but the line does NOT end with the tagged suffix (a fence follows on
    # the same line) → is_start False → the line is ignored prose → no blocks → no-files.
    text = f"@@@FILE_START: resume.md [{TAG}]@@@```markdown"
    assert _reason(text) == "no-files"
