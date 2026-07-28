"""Tests for the cold-start instance materializer (instance-cold-start-parity-v1).

Covers the contract, the four-case adoption logic, the pinned filesystem
semantics (atomic/O_EXCL/symlink-refusal/never-overwrite), idempotency, the
GROVE_HOME redirect regression for D3's two retired hardcodes, and identity
genericization.
"""

import os
import stat

import pytest

import grove.cold_start as cs
from grove.cold_start import ColdStartError, materialize_instance


@pytest.fixture(autouse=True)
def _clear_cache():
    cs._reset_cache()
    yield
    cs._reset_cache()


# ── contract ─────────────────────────────────────────────────────────────────


def test_contract_loads_and_validates():
    contract = cs._load_contract(None)
    assert contract["version"] == cs._COLD_START_VERSION
    assert contract["grove_shape_signature"]
    assert contract["entries"]


def test_every_seed_source_exists_in_repo():
    contract = cs._load_contract(None)
    for entry in contract["entries"]:
        src = entry["seed_source"]
        if src and src != "none":
            assert (cs._repo_root() / src).exists(), f"missing seed source: {src}"


def test_contract_rejects_unknown_absence_class(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "version: 1\n"
        "grove_shape_signature: [sessions]\n"
        "entries:\n"
        "  - path: x\n    kind: file\n    seed_source: none\n"
        "    absence_class: made_up\n    owner: operator-state\n",
        encoding="utf-8",
    )
    with pytest.raises(ColdStartError):
        cs._load_contract(bad)


# ── fresh materialization ────────────────────────────────────────────────────


def test_fresh_home_full_materialization(tmp_path):
    home = tmp_path / "inst"
    report = materialize_instance(home, force=True)

    assert report.state == "fresh"
    assert report.marker_written is True
    assert (home / ".grove_instance").exists()

    # contract-derived dirs (NOT the retired hermes 10-dir list)
    for d in ("sessions", "logs", "logs/curator", "capabilities/state"):
        assert (home / d).is_dir(), d
    assert not (home / "cron").exists()
    assert not (home / "memories").exists()

    # seeded files (definition-seeded, seed_source != none)
    for f in (
        "routing.operational.yaml",
        "routing.authority.yaml",
        "manifest.yaml",
        "write_workspaces.yaml",
        "workspaces.yaml",
        "constitution.md",
        "soul.md",
        "affordances.md",
        "operator-core.md",
        "operator-extended.md",
        "config.yaml",          # SPEC P1 seed (comment-only)
        "dock/dock.yaml",       # SPEC P1 seed / OOBE Moment 2
    ):
        assert (home / f).exists(), f

    # absence-legit files (seed_source: none) are NOT seeded
    for f in ("grants.yaml", "zones.autonomaton.yaml", "routing.autonomaton.yaml",
              "model-catalog.yaml", "agents.md"):
        assert not (home / f).exists(), f


def test_fresh_home_secures_dirs_0700(tmp_path):
    home = tmp_path / "inst"
    materialize_instance(home, force=True)
    assert stat.S_IMODE(os.stat(home).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(home / "sessions").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(home / "capabilities" / "state").st_mode) == 0o700


def test_seeded_files_0600(tmp_path):
    home = tmp_path / "inst"
    materialize_instance(home, force=True)
    assert stat.S_IMODE(os.stat(home / "soul.md").st_mode) == 0o600


# ── idempotency ──────────────────────────────────────────────────────────────


def test_idempotent_second_run_writes_nothing(tmp_path):
    home = tmp_path / "inst"
    materialize_instance(home, force=True)
    report2 = materialize_instance(home, force=True)  # force → bypass process cache
    assert report2.state == "marked"
    assert report2.created_dirs == []
    assert report2.seeded_files == []
    assert report2.marker_written is False
    assert report2.wrote_anything is False


# ── four-case adoption ───────────────────────────────────────────────────────


def test_case_fresh_empty_dir(tmp_path):
    home = tmp_path / "inst"
    home.mkdir()  # exists but empty
    assert materialize_instance(home, force=True).state == "fresh"


def test_case_marked(tmp_path):
    home = tmp_path / "inst"
    materialize_instance(home, force=True)
    assert materialize_instance(home, force=True).state == "marked"


def test_case_adopt_grove_shaped_unmarked(tmp_path):
    home = tmp_path / "inst"
    home.mkdir()
    (home / "sessions").mkdir()  # grove-shape signature present, no marker
    report = materialize_instance(home, force=True)
    assert report.state == "adopt"
    assert report.marker_written is True
    assert (home / ".grove_instance").exists()
    # adoption still seeds missing files
    assert (home / "soul.md").exists()


def test_case_refuse_non_grove_unmarked(tmp_path):
    home = tmp_path / "inst"
    home.mkdir()
    (home / "some-unrelated-file.txt").write_text("not grove", encoding="utf-8")
    with pytest.raises(ColdStartError, match="misconfiguration|not Grove-shaped"):
        materialize_instance(home, force=True)


# ── pinned filesystem semantics (F1) ─────────────────────────────────────────


def test_never_overwrites_operator_file(tmp_path):
    home = tmp_path / "inst"
    home.mkdir()
    (home / "sessions").mkdir()  # grove-shaped → adopt (so materialize proceeds)
    sentinel = "OPERATOR OWNED — DO NOT TOUCH\n"
    (home / "soul.md").write_text(sentinel, encoding="utf-8")
    report = materialize_instance(home, force=True)
    assert (home / "soul.md").read_text(encoding="utf-8") == sentinel
    assert str(home / "soul.md") in report.skipped_present


def test_symlink_refusal(tmp_path):
    home = tmp_path / "inst"
    home.mkdir()
    target = tmp_path / "elsewhere.txt"
    target.write_text("attacker target", encoding="utf-8")
    link = home / "soul.md"
    link.symlink_to(target)
    report = cs.MaterializeReport(home=str(home), state="fresh")
    with pytest.raises(ColdStartError, match="symlink"):
        cs._seed_file(cs._repo_root() / "config" / "identity" / "soul.md", link, report, False)
    # the symlink target was not written through
    assert target.read_text(encoding="utf-8") == "attacker target"


def test_o_excl_collision_refusal(tmp_path):
    dst = tmp_path / "target.yaml"
    # pre-plant the exact pid-scoped tmp the seeder will try to create
    tmp = dst.parent / f".{dst.name}.cold-start.{os.getpid()}.tmp"
    tmp.write_text("stale", encoding="utf-8")
    with pytest.raises(FileExistsError):
        cs._atomic_seed(dst, b"new data")
    # the pre-existing tmp was NOT clobbered, the target was NOT created
    assert tmp.read_text(encoding="utf-8") == "stale"
    assert not dst.exists()


# ── GROVE_HOME redirect (D3 regression: router.py:977 + zones.py:626) ─────────


def test_grove_home_redirect_materializes_under_redirect(tmp_path, monkeypatch):
    home = tmp_path / "redirected"
    monkeypatch.setenv("GROVE_HOME", str(home))
    report = materialize_instance(force=True)  # home=None → get_hermes_home()
    assert report.home == str(home)
    assert (home / "soul.md").exists()
    assert (home / "sessions").is_dir()
    # nothing leaked into the real ~/.grove default
    assert str(home) in report.home


def test_router_seed_honors_grove_home(tmp_path, monkeypatch):
    home = tmp_path / "gh"
    home.mkdir()
    monkeypatch.setenv("GROVE_HOME", str(home))
    from grove.router import _resolve_config_path
    resolved = _resolve_config_path(None)
    # D3: seeds under GROVE_HOME, not a hardcoded ~/.grove
    assert resolved == home / "routing.operational.yaml"
    assert resolved.exists()
    assert (home / "routing.authority.yaml").exists()  # sibling seeded too


def test_zones_overlay_honors_grove_home(tmp_path, monkeypatch):
    home = tmp_path / "gh"
    home.mkdir()
    monkeypatch.setenv("GROVE_HOME", str(home))
    from grove.zones import _resolve_overlay_path
    assert _resolve_overlay_path() is None  # absent → None
    (home / "zones.autonomaton.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    assert _resolve_overlay_path() == home / "zones.autonomaton.yaml"


# ── F5c OpenRouter key signal (gated on require_api_key) ─────────────────────


def test_key_signal_when_absent_and_required(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    report = materialize_instance(tmp_path / "inst", force=True, require_api_key=True)
    assert report.key_signal is not None
    assert "OPENROUTER_API_KEY" in report.key_signal


def test_no_key_signal_when_present(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-abcdefghijklmnop")
    report = materialize_instance(tmp_path / "inst", force=True, require_api_key=True)
    assert report.key_signal is None


def test_key_check_skipped_when_not_required(tmp_path, monkeypatch):
    # The rename makes intent explicit: generic CLI (require_api_key=False, the
    # default) never runs the F5c check, even with no key present. Only gateway
    # init opts in. require_api_key is NOT a refuse bypass.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    report = materialize_instance(tmp_path / "inst", force=True)  # default False
    assert report.key_signal is None


# ── F1 case 4 refuses UNCONDITIONALLY — including the CLI/load_config path ────


def test_cli_path_load_config_refuses_case4(tmp_path, monkeypatch):
    # The load_config hot path (via ensure_hermes_home → materialize_instance,
    # require_api_key defaulting False) must STILL refuse a case-4 GROVE_HOME —
    # there is no lenient bypass. Point GROVE_HOME at a non-empty, unmarked,
    # non-Grove-shaped dir and confirm load_config raises with zero writes.
    bad = tmp_path / "not-grove"
    bad.mkdir()
    (bad / "some-unrelated.txt").write_text("junk", encoding="utf-8")
    monkeypatch.setenv("GROVE_HOME", str(bad))
    cs._reset_cache()
    from hermes_cli.config import load_config
    with pytest.raises(ColdStartError, match="not Grove-shaped|wrong directory"):
        load_config()
    # zero provisioning writes into the case-4 dir
    assert not (bad / ".grove_instance").exists()
    assert not (bad / "sessions").exists()
    assert not (bad / "soul.md").exists()
    assert list(bad.iterdir()) == [bad / "some-unrelated.txt"]


# ── dock + config.yaml seeding (restored SPEC P1 seeds) ──────────────────────


def test_dock_and_config_seeded_from_repo(tmp_path):
    home = tmp_path / "inst"
    materialize_instance(home, force=True)
    assert (home / "dock" / "dock.yaml").exists()
    assert (home / "config.yaml").exists()
    # dock came from the repo template byte-for-byte
    assert (home / "dock" / "dock.yaml").read_bytes() == (
        cs._repo_root() / "config" / "dock" / "dock.yaml"
    ).read_bytes()


def test_dock_and_config_never_overwritten(tmp_path):
    home = tmp_path / "inst"
    home.mkdir()
    (home / "sessions").mkdir()  # grove-shaped → adopt, so materialize proceeds
    (home / "dock").mkdir()
    dock_sentinel = "version: 1\ngoals: []\n# OPERATOR DOCK — keep\n"
    cfg_sentinel = "OPENROUTER_API_KEY: sk-operator-owned\n"
    (home / "dock" / "dock.yaml").write_text(dock_sentinel, encoding="utf-8")
    (home / "config.yaml").write_text(cfg_sentinel, encoding="utf-8")
    materialize_instance(home, force=True)
    assert (home / "dock" / "dock.yaml").read_text(encoding="utf-8") == dock_sentinel
    assert (home / "config.yaml").read_text(encoding="utf-8") == cfg_sentinel


def test_config_seed_parses_to_empty_dict_not_none():
    import yaml
    seed = (cs._repo_root() / "config" / "config.yaml").read_text(encoding="utf-8")
    # GATE-B F4: config.yaml must parse to a dict, NOT None (a comments-only file
    # parses to None, which F4 classifies as MALFORMED — P2 graduates that to a
    # loud halt, so a fresh install would trip its own detector). The explicit {}
    # yields an empty dict → zero DEFAULT_CONFIG semantic change.
    parsed = yaml.safe_load(seed)
    assert parsed == {}
    assert isinstance(parsed, dict)
    assert "OPENROUTER_API_KEY" in seed  # the commented key line is present


def test_seed_decision_keys_on_seed_source_not_absence_class(tmp_path):
    # Correction 3: an entry is seeded iff seed_source != none, regardless of
    # absence_class. grants.yaml (seed_source none, absence_class silent_empty)
    # and manifest.yaml (seed_source set, absence_class fail_loud) prove both
    # axes are independent.
    home = tmp_path / "inst"
    materialize_instance(home, force=True)
    assert (home / "manifest.yaml").exists()       # seed_source set → seeded
    assert not (home / "grants.yaml").exists()      # seed_source none → not seeded


# ── identity genericization (P0 §P0.3) ───────────────────────────────────────


def test_fresh_seed_produces_generic_soul(tmp_path):
    home = tmp_path / "inst"
    materialize_instance(home, force=True)
    soul = (home / "soul.md").read_text(encoding="utf-8")
    assert "Mylo" not in soul
    assert "Name your Autonomaton" in soul


def test_repo_identity_files_have_no_mylo_graft():
    idir = cs._repo_root() / "config" / "identity"
    for f in ("soul.md", "constitution.md", "affordances.md", "operator-core.md"):
        assert "Mylo" not in (idir / f).read_text(encoding="utf-8"), f


def test_operator_core_is_a_clean_stub():
    text = (cs._repo_root() / "config" / "identity" / "operator-core.md").read_text(encoding="utf-8")
    assert "[Replace this" in text  # placeholder, not filled operator content


def test_jidoka_tiers_unchanged():
    from grove.identity import _IDENTITY_FILES
    tiers = {row[0]: row[3] for row in _IDENTITY_FILES}
    assert tiers["constitution.md"] == "jidoka"
    assert tiers["soul.md"] == "jidoka"
