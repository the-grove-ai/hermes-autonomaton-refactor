"""Tests for instance-file health classification, malformed→loud graduation,
and the F2 repair entry point (instance-cold-start-parity-v1 P2).

Graduated set is EXACTLY three: config.yaml, grants.yaml, write_workspaces.yaml.
Absent ≠ malformed in both directions.
"""

import os
import stat

import pytest

import grove.cold_start as cs
import grove.instance_health as ih
from grove.instance_health import FileHealth, InstanceFileError


GRADUATED = ["config.yaml", "grants.yaml", "write_workspaces.yaml"]


@pytest.fixture(autouse=True)
def _clear_caches():
    cs._reset_cache()
    yield
    cs._reset_cache()


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    """A freshly materialized instance (all graduated files OK or legit-absent)."""
    home = tmp_path / "inst"
    monkeypatch.setenv("GROVE_HOME", str(home))
    cs.materialize_instance(home, bypass_cache=True)
    return home


# ── classifier: every category on every graduated file's expected shape ──────


@pytest.mark.parametrize("name", GRADUATED)
def test_classify_ok_mapping(tmp_path, name):
    p = tmp_path / name
    p.write_text("grants: []\n", encoding="utf-8")  # a valid top-level mapping
    assert ih.classify_instance_file(p, "mapping", name=name).health == FileHealth.OK


@pytest.mark.parametrize("name", GRADUATED)
def test_classify_absent(tmp_path, name):
    assert ih.classify_instance_file(tmp_path / name, "mapping", name=name).health == FileHealth.ABSENT


@pytest.mark.parametrize("name", GRADUATED)
def test_classify_0_byte_malformed(tmp_path, name):
    p = tmp_path / name
    p.write_text("", encoding="utf-8")
    c = ih.classify_instance_file(p, "mapping", name=name)
    assert c.health == FileHealth.MALFORMED
    assert "0-byte" in c.evidence


@pytest.mark.parametrize("name", GRADUATED)
def test_classify_parses_to_none_malformed(tmp_path, name):
    p = tmp_path / name
    p.write_text("# only comments here\n", encoding="utf-8")
    c = ih.classify_instance_file(p, "mapping", name=name)
    assert c.health == FileHealth.MALFORMED
    assert "None" in c.evidence


@pytest.mark.parametrize("name", GRADUATED)
def test_classify_parse_error_malformed(tmp_path, name):
    p = tmp_path / name
    p.write_text("key: [unclosed\n", encoding="utf-8")
    c = ih.classify_instance_file(p, "mapping", name=name)
    assert c.health == FileHealth.MALFORMED
    assert "parse error" in c.evidence


@pytest.mark.parametrize("name", GRADUATED)
def test_classify_wrong_top_level_shape_malformed(tmp_path, name):
    p = tmp_path / name
    p.write_text("- a\n- b\n", encoding="utf-8")  # a sequence, not a mapping
    c = ih.classify_instance_file(p, "mapping", name=name)
    assert c.health == FileHealth.MALFORMED
    assert "expected mapping" in c.evidence


@pytest.mark.parametrize("name", GRADUATED)
def test_classify_unreadable(tmp_path, name):
    if os.geteuid() == 0:
        pytest.skip("euid==0 bypasses file permissions; chmod is moot")
    p = tmp_path / name
    p.write_text("grants: []\n", encoding="utf-8")
    os.chmod(p, 0)
    try:
        c = ih.classify_instance_file(p, "mapping", name=name)
        assert c.health == FileHealth.UNREADABLE
        assert "permission" in c.evidence.lower()
    finally:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)


def test_unreadable_distinct_from_malformed(tmp_path):
    # Different root, different signal: the two fault categories are not merged.
    assert FileHealth.UNREADABLE != FileHealth.MALFORMED


# ── reader graduation (D2) — present-but-malformed raises; absent unchanged ──


def _load_config_fresh(monkeypatch, home):
    from hermes_cli.config import _LOAD_CONFIG_CACHE, load_config
    _LOAD_CONFIG_CACHE.clear()
    cs._reset_cache()
    return load_config()


def test_config_reader_raises_on_malformed(fresh_home, monkeypatch):
    (fresh_home / "config.yaml").write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    from hermes_cli.config import _LOAD_CONFIG_CACHE, load_config
    _LOAD_CONFIG_CACHE.clear()
    with pytest.raises(InstanceFileError, match="config.yaml.*repair-instance"):
        load_config()


def test_config_reader_absent_still_default_config(tmp_path, monkeypatch):
    # ABSENT config → DEFAULT_CONFIG, byte-for-byte unchanged (no materialize).
    home = tmp_path / "bare"
    home.mkdir()
    (home / ".grove_instance").write_text("cold_start_version: 1\n")  # avoid refuse
    monkeypatch.setenv("GROVE_HOME", str(home))
    from hermes_cli.config import _LOAD_CONFIG_CACHE, DEFAULT_CONFIG, load_config
    _LOAD_CONFIG_CACHE.clear()
    cs._reset_cache()
    # Remove any seed the auto-mark materialize may have created, to test ABSENT.
    (home / "config.yaml").unlink(missing_ok=True)
    _LOAD_CONFIG_CACHE.clear()
    cfg = load_config()
    assert cfg["model"] == DEFAULT_CONFIG["model"]


def test_grants_reader_raises_on_malformed(tmp_path, monkeypatch):
    home = tmp_path / "inst"
    home.mkdir()
    (home / "grants.yaml").write_text("just a string, not a mapping", encoding="utf-8")
    from grove.grants import GrantStore
    store = GrantStore(home / "grants.yaml")
    with pytest.raises(InstanceFileError, match="grants.yaml"):
        store.load()


def test_grants_reader_absent_still_silent_empty(tmp_path):
    from grove.grants import GrantStore
    store = GrantStore(tmp_path / "nope-grants.yaml")
    assert store.load() == []  # ABSENT → silent [], unchanged


def test_write_workspaces_reader_raises_on_malformed(tmp_path):
    import grove.utils.fs_utils as fsu
    fsu._write_workspaces_cache.clear()
    (tmp_path / "write_workspaces.yaml").write_text("42\n", encoding="utf-8")  # int, not mapping
    with pytest.raises(InstanceFileError, match="write_workspaces.yaml"):
        fsu._load_write_workspaces(str(tmp_path))


def test_write_workspaces_reader_absent_still_loud_empty(tmp_path):
    import grove.utils.fs_utils as fsu
    fsu._write_workspaces_cache.clear()
    # ABSENT → loud-empty frozenset(), unchanged (no raise).
    assert fsu._load_write_workspaces(str(tmp_path)) == frozenset()


# ── gateway preflight (D3) — loud halt on each fault category, names file ────


@pytest.mark.parametrize("content,label", [
    ("", "0-byte"),
    ("# comments only\n", "parses-to-None"),
    ("key: [unclosed\n", "parse-error"),
    ("- a\n- b\n", "wrong-shape"),
])
def test_preflight_halts_loud_on_each_category(fresh_home, content, label):
    (fresh_home / "config.yaml").write_text(content, encoding="utf-8")
    with pytest.raises(InstanceFileError, match="config.yaml"):
        ih.preflight_graduated_files(fresh_home)


def test_preflight_clean_on_fresh(fresh_home):
    results = ih.preflight_graduated_files(fresh_home)
    assert all(not c.is_fault for c in results)


# ── repair entry point (D4) ──────────────────────────────────────────────────


def test_repair_detects_without_parsing_success(fresh_home):
    # Every graduated file corrupt: detection still runs and completes.
    (fresh_home / "config.yaml").write_text("key: [unclosed\n", encoding="utf-8")
    (fresh_home / "grants.yaml").write_text("- not a mapping\n", encoding="utf-8")
    (fresh_home / "write_workspaces.yaml").write_text("", encoding="utf-8")
    plan = ih.detect(fresh_home)
    assert {c.name for c in plan.faults} == set(GRADUATED)


def test_repair_confirm_quarantines_and_reseeds_green(fresh_home):
    (fresh_home / "config.yaml").write_text("# broken to None\n", encoding="utf-8")
    out = []
    summary = ih.run_repair(home=fresh_home, confirm=lambda p: True, out=out.append)
    assert summary["confirmed"] is True
    assert summary["clean_after"] is True
    # quarantine landed in the DECLARED location: ~/.grove/quarantine/<name>.<ts>
    q = summary["quarantined"][0]
    assert q["name"] == "config.yaml"
    assert str(fresh_home / "quarantine") in q["quarantined_to"]
    assert os.path.exists(q["quarantined_to"])  # residue preserved
    # reseeded green
    assert ih.classify_instance_file(fresh_home / "config.yaml", "mapping", name="config.yaml").health == FileHealth.OK


def test_repair_decline_zero_writes(fresh_home):
    (fresh_home / "config.yaml").write_text("# broken\n", encoding="utf-8")
    before = (fresh_home / "config.yaml").read_bytes()
    quarantine_before = list((fresh_home / "quarantine").iterdir())
    summary = ih.run_repair(home=fresh_home, confirm=lambda p: False, out=lambda _m: None)
    assert summary["confirmed"] is False
    assert summary["quarantined"] == []
    # zero writes: the (still-broken) file is byte-identical, quarantine untouched
    assert (fresh_home / "config.yaml").read_bytes() == before
    assert list((fresh_home / "quarantine").iterdir()) == quarantine_before


def test_repair_grants_quarantine_restores_legit_absent(fresh_home):
    # grants.yaml has seed_source: none → after quarantine it is legitimately
    # absent (silent []), and the materializer does NOT reseed it. Still "clean".
    (fresh_home / "grants.yaml").write_text("not a mapping\n", encoding="utf-8")
    summary = ih.run_repair(home=fresh_home, confirm=lambda p: True, out=lambda _m: None)
    assert summary["clean_after"] is True
    assert not (fresh_home / "grants.yaml").exists()  # legit-absent restored


# ── contract validation covers the new quarantine + graduation entries ───────


def test_contract_declares_quarantine_and_graduated(fresh_home):
    contract = cs._load_contract(None)
    paths = {e["path"] for e in contract["entries"]}
    assert "quarantine/" in paths
    grad = {e["path"] for e in contract["entries"] if e.get("graduated")}
    assert grad == set(GRADUATED)
    for e in contract["entries"]:
        if e.get("graduated"):
            assert e["expected_shape"] in ("mapping", "sequence")


def test_quarantine_dir_materialized(fresh_home):
    assert (fresh_home / "quarantine").is_dir()
