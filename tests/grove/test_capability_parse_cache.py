"""capability-parse-cache-v1 — the registry parses each record file once per
file version, and never shares record objects between loads."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

import grove.capability_registry as cr

_REPO_CAPS = Path(__file__).resolve().parents[2] / "config" / "capabilities"


@pytest.fixture
def caps_dir(tmp_path):
    d = tmp_path / "capabilities"
    d.mkdir()
    for src in sorted(_REPO_CAPS.glob("*.yaml"))[:5]:
        shutil.copy(src, d / src.name)
    return d


def _count_parses(monkeypatch):
    calls = []
    real = cr.yaml.safe_load

    def _counting(text, *a, **kw):
        calls.append(1)
        return real(text, *a, **kw)

    monkeypatch.setattr(cr.yaml, "safe_load", _counting)
    return calls


def test_second_load_does_not_reparse(caps_dir, monkeypatch):
    calls = _count_parses(monkeypatch)
    first = cr._load_records_from_dir(caps_dir)
    parsed = len(calls)
    assert parsed == 5
    second = cr._load_records_from_dir(caps_dir)
    assert len(calls) == parsed               # served from the parse cache
    assert sorted(first) == sorted(second)
    assert [c.to_dict() for c in first.values()] == [c.to_dict() for c in second.values()]


def test_loads_never_share_record_objects(caps_dir):
    first = cr._load_records_from_dir(caps_dir)
    second = cr._load_records_from_dir(caps_dir)
    for rid in first:
        assert first[rid] is not second[rid]  # fresh objects every load


def test_cached_document_is_not_mutated_by_callers(caps_dir):
    path = sorted(caps_dir.glob("*.yaml"))[0]
    doc = cr._parsed_record_doc(path)
    doc.clear()                               # a caller trashes its copy
    assert cr._parsed_record_doc(path)        # the cache still has the real one


def test_edited_file_is_reparsed(caps_dir, monkeypatch):
    path = sorted(caps_dir.glob("*.yaml"))[0]
    before = cr._load_records_from_dir(caps_dir)
    rid = cr._parsed_record_doc(path)["id"]
    text = path.read_text(encoding="utf-8")
    path.write_text(text + "\n# operator edit\n", encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    calls = _count_parses(monkeypatch)
    after = cr._load_records_from_dir(caps_dir)
    assert len(calls) == 1                    # only the edited file re-parsed
    assert after[rid].to_dict() == before[rid].to_dict()


def test_malformed_record_fails_loud_every_time(caps_dir):
    bad = caps_dir / "zz_bad.yaml"
    bad.write_text("id: [unclosed\n", encoding="utf-8")
    for _ in range(2):                        # a failure is never cached
        with pytest.raises(cr.CapabilityLoadError, match="zz_bad.yaml"):
            cr._load_records_from_dir(caps_dir)


def test_invalid_field_still_fails_validation_on_a_cache_hit(caps_dir):
    path = sorted(caps_dir.glob("*.yaml"))[0]
    cr._load_records_from_dir(caps_dir)       # warm
    bad = caps_dir / "zz_invalid.yaml"
    bad.write_text("not_a_capability: true\n", encoding="utf-8")
    for _ in range(2):                        # validation runs on every load
        with pytest.raises(cr.CapabilityLoadError, match="zz_invalid.yaml"):
            cr._load_records_from_dir(caps_dir)
