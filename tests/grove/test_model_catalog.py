"""model-catalog-v1 P1 guards for the GROVE portal catalog (grove/config/model_catalog.py).

Distinct from tests/hermes_cli/test_model_catalog.py (the separate CLI remote-JSON
catalog — see cli-catalog-unification debt). Covers:
  * G-1a metadata-only schema contract — unknown and endpoint/credential fields
    are rejected on load, so the catalog can never become routing-load-bearing.
  * G-2 referential integrity guard — a catalog write that removes/renames a slug
    still referenced by a live tier binding or ModelBinding record is refused,
    naming every referrer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from grove.config.model_catalog import (
    CatalogWriteError,
    _load_catalog_file,
    _validate_catalog,
    assert_safe_catalog_mutation,
    describe_catalog_write,
    load_catalog,
    merge_catalogs,
    merged_catalog_provenance,
    mint_catalog_entry,
    upsert_sovereign_entry,
    write_sovereign_catalog,
)

_ADD_KIMI = (
    "models:\n"
    '  - slug: "moonshotai/kimi-k3"\n'
    '    display_name: "Kimi K3"\n'
    "    provider: openrouter\n"
    "    input_cost_per_mtok: 1\n"
    "    output_cost_per_mtok: 2\n"
)

_SRC = Path("t.yaml")


def _entry(slug="a/b", **over):
    e = {
        "slug": slug,
        "display_name": "X",
        "provider": "openrouter",
        "input_cost_per_mtok": 1,
        "output_cost_per_mtok": 2,
    }
    e.update(over)
    return e


# ── G-1a: metadata-only schema contract ──────────────────────────────────────


class TestMetadataOnlyContract:
    def test_repo_catalog_passes_hardened_schema(self):
        # The shipped catalog must remain valid under the tightened schema.
        assert len(load_catalog()) >= 9

    def test_notes_is_allowed(self):
        _validate_catalog([_entry(notes="cheap and fast")], _SRC)  # no raise

    def test_unknown_field_rejected(self):
        with pytest.raises(ValueError, match="unknown field 'flavor'"):
            _validate_catalog([_entry(flavor="spicy")], _SRC)

    @pytest.mark.parametrize(
        "field",
        ["url", "endpoint", "base_url", "api_base", "api_key", "apikey",
         "token", "secret", "credential", "auth", "bearer", "password",
         "auth_header"],
    )
    def test_credential_class_field_rejected(self, field):
        with pytest.raises(ValueError, match="endpoint/credential-class"):
            _validate_catalog([_entry(**{field: "x"})], _SRC)

    def test_credential_message_is_louder_than_unknown(self):
        # A credential-class unknown field gets the metadata-only message, not the
        # generic unknown-field one — the contract is stated at the point of refusal.
        with pytest.raises(ValueError, match="metadata-only"):
            _validate_catalog([_entry(base_url="http://x")], _SRC)


# ── G-2: referential integrity guard ─────────────────────────────────────────


class TestReferentialGuard:
    def _cur(self):
        return [_entry("keep/me"), _entry("drop/me")]

    def test_unreferenced_delete_allowed(self):
        new = [_entry("keep/me")]
        out = assert_safe_catalog_mutation(new, current_models=self._cur(), referrers={})
        assert [m["slug"] for m in out] == ["keep/me"]

    def test_referenced_delete_refused_names_referrer(self):
        new = [_entry("keep/me")]
        with pytest.raises(CatalogWriteError) as ei:
            assert_safe_catalog_mutation(
                new, current_models=self._cur(),
                referrers={"drop/me": ["tier_preferences.T2", "ModelBinding[skill.fleet.scout]"]},
            )
        msg = str(ei.value)
        assert "drop/me" in msg
        assert "tier_preferences.T2" in msg
        assert "ModelBinding[skill.fleet.scout]" in msg

    def test_rename_of_referenced_is_delete_plus_add_refused(self):
        # Renaming drop/me -> drop/me-v2 removes the old slug; since it is
        # referenced, the write is refused (rename == delete + add).
        new = [_entry("keep/me"), _entry("drop/me-v2")]
        with pytest.raises(CatalogWriteError, match="drop/me"):
            assert_safe_catalog_mutation(
                new, current_models=self._cur(),
                referrers={"drop/me": ["tier_preferences.T3"]},
            )

    def test_add_only_is_always_allowed(self):
        new = self._cur() + [_entry("brand/new")]
        out = assert_safe_catalog_mutation(new, current_models=self._cur(), referrers={"drop/me": ["tier_preferences.T2"]})
        assert "brand/new" in {m["slug"] for m in out}

    def test_mutation_validates_schema_first(self):
        # A proposed catalog that violates the metadata contract is rejected
        # before referential analysis — a bad write never reaches the referrers.
        with pytest.raises(ValueError, match="endpoint/credential-class"):
            assert_safe_catalog_mutation(
                [_entry("keep/me", api_key="leak")],
                current_models=self._cur(), referrers={},
            )


# ── M-9: per-slug merge (operator-wins per slug, repo entries survive) ────────


class TestPerSlugMerge:
    def test_override_wins_per_slug(self):
        repo = [_entry("a/x", display_name="RepoX"), _entry("b/y", display_name="RepoY")]
        sov = [_entry("a/x", display_name="OverrideX", input_cost_per_mtok=99)]
        merged = {m["slug"]: m for m in merge_catalogs(repo, sov)}
        assert merged["a/x"]["display_name"] == "OverrideX"
        assert merged["a/x"]["input_cost_per_mtok"] == 99

    def test_unnamed_repo_entries_survive(self):
        repo = [_entry("a/x"), _entry("b/y"), _entry("c/z")]
        sov = [_entry("a/x", display_name="only-a-overridden")]
        slugs = [m["slug"] for m in merge_catalogs(repo, sov)]
        assert slugs == ["a/x", "b/y", "c/z"]  # order preserved, none dropped

    def test_new_sovereign_slug_appended(self):
        repo = [_entry("a/x")]
        sov = [_entry("z/new")]
        slugs = [m["slug"] for m in merge_catalogs(repo, sov)]
        assert slugs == ["a/x", "z/new"]

    def test_load_catalog_merges_repo_and_sovereign(self, tmp_path, monkeypatch):
        sov = tmp_path / "model-catalog.yaml"
        sov.write_text(
            "models:\n"
            '  - slug: "moonshotai/kimi-k3"\n'
            '    display_name: "Kimi K3"\n'
            "    provider: openrouter\n"
            "    input_cost_per_mtok: 1\n"
            "    output_cost_per_mtok: 2\n",
            encoding="utf-8",
        )
        monkeypatch.setattr("grove.config.model_catalog._sovereign_catalog_path", lambda: sov)
        slugs = {m["slug"] for m in load_catalog()}
        assert "moonshotai/kimi-k3" in slugs           # added
        assert "anthropic/claude-opus-4.6" in slugs    # repo entry survives

    def test_in_file_duplicate_slug_rejected(self, tmp_path):
        f = tmp_path / "model-catalog.yaml"
        f.write_text(
            "models:\n"
            '  - {slug: "a/x", display_name: "1", provider: p, input_cost_per_mtok: 1, output_cost_per_mtok: 2}\n'
            '  - {slug: "a/x", display_name: "2", provider: p, input_cost_per_mtok: 1, output_cost_per_mtok: 2}\n',
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="duplicate slug"):
            _load_catalog_file(f)

    def test_referential_guard_sees_merged_current(self, tmp_path, monkeypatch):
        # G-2 runs against the MERGED view: removing a sovereign-added slug that
        # a referrer points at is refused just like a repo slug.
        sov = tmp_path / "model-catalog.yaml"
        sov.write_text(
            "models:\n"
            '  - {slug: "z/candidate", display_name: "C", provider: p, input_cost_per_mtok: 1, output_cost_per_mtok: 2}\n',
            encoding="utf-8",
        )
        monkeypatch.setattr("grove.config.model_catalog._sovereign_catalog_path", lambda: sov)
        current = load_catalog()  # includes z/candidate
        new = [m for m in current if m["slug"] != "z/candidate"]  # drop it
        with pytest.raises(CatalogWriteError, match="z/candidate"):
            assert_safe_catalog_mutation(new, referrers={"z/candidate": ["ModelBinding[skill.fleet.scout]"]})


# ── G-4: merge provenance for the approval card ──────────────────────────────


class TestMergeProvenance:
    def _sov(self, tmp_path, monkeypatch, body):
        f = tmp_path / "model-catalog.yaml"
        f.write_text(body, encoding="utf-8")
        monkeypatch.setattr("grove.config.model_catalog._sovereign_catalog_path", lambda: f)

    def test_repo_only_entry_marked_repo(self, tmp_path, monkeypatch):
        # No sovereign file → every entry is origin=repo, no shadows.
        prov = {p["slug"]: p for p in merged_catalog_provenance()}
        anchor = prov["anthropic/claude-opus-4.6"]
        assert anchor["_origin"] == "repo" and anchor["_shadowed_fields"] == {}

    def test_new_sovereign_slug_marked_override(self, tmp_path, monkeypatch):
        self._sov(tmp_path, monkeypatch,
            "models:\n  - {slug: \"z/new\", display_name: N, provider: p, input_cost_per_mtok: 1, output_cost_per_mtok: 2}\n")
        prov = {p["slug"]: p for p in merged_catalog_provenance()}
        assert prov["z/new"]["_origin"] == "override"
        assert prov["z/new"]["_shadowed_fields"] == {}

    def test_shadowing_override_lists_masked_fields(self, tmp_path, monkeypatch):
        self._sov(tmp_path, monkeypatch,
            "models:\n  - {slug: \"anthropic/claude-opus-4.6\", display_name: \"MINE\", "
            "provider: openrouter, input_cost_per_mtok: 99, output_cost_per_mtok: 100}\n")
        prov = {p["slug"]: p for p in merged_catalog_provenance()}
        rec = prov["anthropic/claude-opus-4.6"]
        assert rec["_origin"] == "override_shadows_repo"
        assert rec["display_name"] == "MINE"                 # resolved value wins
        assert "input_cost_per_mtok" in rec["_shadowed_fields"]
        assert rec["_shadowed_fields"]["input_cost_per_mtok"]["override"] == 99


# ── M-5/G-4: approval-card rendering for a catalog write ──────────────────────


class TestCatalogWriteCard:
    def test_new_model_renders_merged_view_not_delta(self):
        desc = describe_catalog_write("/home/hermes/.grove/model-catalog.yaml", _ADD_KIMI)
        assert desc is not None
        assert "resolved merged view" in desc
        assert "moonshotai/kimi-k3 | Kimi K3 | openrouter | $1/$2 per Mtok [NEW]" in desc
        # M-9 legibility: unlisted repo entries survive, stated on the card.
        assert "unlisted repo entries survive" in desc

    def test_shadowing_write_marks_masked_fields(self):
        shadow = (
            "models:\n"
            '  - slug: "anthropic/claude-opus-4.6"\n'
            '    display_name: "MINE"\n'
            "    provider: openrouter\n"
            "    input_cost_per_mtok: 99\n"
            "    output_cost_per_mtok: 100\n"
        )
        desc = describe_catalog_write("/x/model-catalog.yaml", shadow)
        assert "[SHADOWS repo:" in desc
        assert "input_cost_per_mtok" in desc
        assert "MINE" in desc and "$99/$100" in desc  # fully-resolved, not delta

    def test_matching_entry_marked_matches_repo(self):
        # Re-writing an existing repo entry verbatim → no shadow, marked as match.
        import yaml

        cur = load_catalog()
        opus = next(m for m in cur if m["slug"] == "anthropic/claude-opus-4.6")
        body = yaml.safe_dump({"models": [dict(opus)]}, sort_keys=False)
        desc = describe_catalog_write("/x/model-catalog.yaml", body)
        assert desc is not None
        assert "[matches repo]" in desc

    def test_non_catalog_path_returns_none(self):
        assert describe_catalog_write("/tmp/notes.txt", "hello") is None

    def test_unparseable_content_returns_none(self):
        assert describe_catalog_write("/x/model-catalog.yaml", "not: [valid") is None
        assert describe_catalog_write("/x/model-catalog.yaml", "no models key: true") is None

    def test_card_integration_via_describe_red_action(self):
        from grove.red_pending_store import describe_red_action

        desc, opaque = describe_red_action(
            "write_file",
            {"path": "/home/hermes/.grove/model-catalog.yaml", "content": _ADD_KIMI},
        )
        assert opaque is False
        assert "resolved merged view" in desc and "[NEW]" in desc

    def test_non_catalog_write_file_still_generic(self):
        from grove.red_pending_store import describe_red_action

        desc, _ = describe_red_action("write_file", {"path": "/tmp/x.txt", "content": "hi"})
        assert desc.startswith("Write file /tmp/x.txt")


# ── P4: deterministic mint + sovereign write (the DoD write path) ────────────


class TestMintAndWrite:
    @pytest.fixture
    def sov(self, tmp_path, monkeypatch):
        path = tmp_path / "model-catalog.yaml"
        monkeypatch.setattr("grove.config.model_catalog._sovereign_catalog_path", lambda: path)
        return path

    def test_mint_builds_schema_valid_entry(self):
        e = mint_catalog_entry("moonshotai/kimi-k3", "Kimi K3", 3.0, 15.0, notes="fast")
        assert e == {
            "slug": "moonshotai/kimi-k3", "display_name": "Kimi K3",
            "provider": "openrouter", "input_cost_per_mtok": 3.0,
            "output_cost_per_mtok": 15.0, "notes": "fast",
        }

    def test_mint_defaults_provider_to_openrouter(self):
        # The live-run bug: the agent put provider=moonshotai. The mint fixes it.
        assert mint_catalog_entry("moonshotai/kimi-k3", "K", 1, 2)["provider"] == "openrouter"

    def test_mint_rejects_bad_cost(self):
        with pytest.raises(ValueError, match="input_cost_per_mtok"):
            mint_catalog_entry("a/b", "B", "free", 2)

    def test_mint_rejects_credential_smuggling(self):
        # notes is the only free field; a credential can't ride in — mint uses the
        # metadata-only schema, so there is no field to smuggle one through.
        e = mint_catalog_entry("a/b", "B", 1, 2, notes="see docs")
        assert set(e) <= {"slug", "display_name", "provider",
                          "input_cost_per_mtok", "output_cost_per_mtok", "notes"}

    def test_write_roundtrips_through_load(self, sov):
        e = mint_catalog_entry("moonshotai/kimi-k3", "Kimi K3", 3.0, 15.0)
        write_sovereign_catalog(upsert_sovereign_entry(e))
        cat = load_catalog()
        slugs = {m["slug"] for m in cat}
        assert "moonshotai/kimi-k3" in slugs            # added
        assert "anthropic/claude-opus-4.6" in slugs     # repo survives
        assert len(cat) == 26

    def test_written_file_is_schema_shaped(self, sov):
        # Regression pin for the live-run defect: the writer emits a `models:` LIST
        # (not the slug-keyed map the agent free-handed), so load never fails.
        import yaml
        write_sovereign_catalog(upsert_sovereign_entry(mint_catalog_entry("a/b", "B", 1, 2)))
        data = yaml.safe_load(sov.read_text())
        assert isinstance(data["models"], list)
        assert data["models"][0]["slug"] == "a/b"

    def test_upsert_replaces_same_slug_in_place(self, sov):
        first = mint_catalog_entry("a/b", "First", 1, 2)
        write_sovereign_catalog(upsert_sovereign_entry(first))
        second = mint_catalog_entry("a/b", "Second", 9, 9)
        models = upsert_sovereign_entry(second)
        assert [m["slug"] for m in models] == ["a/b"]          # no dup
        assert models[0]["display_name"] == "Second"

    def test_add_catalog_entry_tool_happy_path(self, sov):
        from tools.catalog_tool import add_catalog_entry
        msg = add_catalog_entry("moonshotai/kimi-k3", "Kimi K3", 3.0, 15.0, notes="fast")
        assert "Added moonshotai/kimi-k3" in msg
        assert sov.exists()
        assert "moonshotai/kimi-k3" in {m["slug"] for m in load_catalog()}

    def test_add_catalog_entry_tool_rejects_bad_input(self, sov):
        from tools.catalog_tool import add_catalog_entry
        msg = add_catalog_entry("a/b", "B", "not-a-number", 2)
        assert "invalid entry" in msg.lower() or "error" in msg.lower()
        assert not sov.exists()  # nothing written on a bad mint

    def test_tool_card_shows_merged_view(self):
        from grove.red_pending_store import describe_red_action
        desc, opaque = describe_red_action(
            "add_catalog_entry",
            {"slug": "moonshotai/kimi-k3", "display_name": "Kimi K3",
             "input_cost_per_mtok": 3.0, "output_cost_per_mtok": 15.0},
        )
        assert opaque is False
        assert "[NEW]" in desc and "moonshotai/kimi-k3" in desc and "repo entries survive" in desc


# ── gap-2: file-write doors refuse raw catalog writes (steer to the tool) ─────


class TestRawCatalogWriteDoor:
    def test_write_file_to_sovereign_catalog_refused(self):
        from tools.file_tools import write_file_tool
        r = write_file_tool("/home/hermes/.grove/model-catalog.yaml", "models: junk")
        assert "add_catalog_entry" in r and "system-managed" in r

    def test_write_file_to_repo_catalog_refused(self):
        from tools.file_tools import write_file_tool
        r = write_file_tool("config/model-catalog.yaml", "models: junk")
        assert "add_catalog_entry" in r

    def test_patch_catalog_refused(self):
        from tools.file_tools import patch_tool
        r = patch_tool(mode="replace", path="/x/model-catalog.yaml",
                       old_string="a", new_string="b")
        assert "add_catalog_entry" in r

    def test_normal_write_not_refused_by_catalog_door(self, tmp_path):
        from tools.file_tools import write_file_tool
        r = write_file_tool(str(tmp_path / "notes.txt"), "hello")
        assert "add_catalog_entry" not in r

    def test_similarly_named_file_not_caught(self, tmp_path):
        from tools.file_tools import write_file_tool
        r = write_file_tool(str(tmp_path / "my-model-catalog.yaml"), "x")
        assert "add_catalog_entry" not in r  # basename match, not substring

    def test_tool_path_unaffected_by_door(self, tmp_path, monkeypatch):
        # add_catalog_entry writes via write_sovereign_catalog, not the file door.
        monkeypatch.setattr(
            "grove.config.model_catalog._sovereign_catalog_path",
            lambda: tmp_path / "model-catalog.yaml",
        )
        from tools.catalog_tool import add_catalog_entry
        msg = add_catalog_entry("moonshotai/kimi-k3", "Kimi K3", 3.0, 15.0)
        assert "Added moonshotai/kimi-k3" in msg
        assert (tmp_path / "model-catalog.yaml").exists()


# ── GATE-B fold: mint hardening (G-2 TOCTOU, G-3 bounds, G-4 sanitize, G-5, G-6) ─


class TestMintHardening:
    @pytest.fixture
    def sov(self, tmp_path, monkeypatch):
        path = tmp_path / "model-catalog.yaml"
        monkeypatch.setattr("grove.config.model_catalog._sovereign_catalog_path", lambda: path)
        return path

    # G-6 provider enum
    def test_provider_typo_fails_loud(self):
        with pytest.raises(ValueError, match="not a known routing provider"):
            mint_catalog_entry("x/y", "Y", 1, 2, provider="openrouterr")

    def test_default_provider_openrouter_ok(self):
        assert mint_catalog_entry("x/y", "Y", 1, 2)["provider"] == "openrouter"

    # G-3 cost bounds
    def test_cost_over_cap_rejected(self):
        with pytest.raises(ValueError, match="out of bounds"):
            mint_catalog_entry("x/y", "Y", 3_000_000, 2)

    def test_negative_cost_rejected(self):
        with pytest.raises(ValueError, match="out of bounds"):
            mint_catalog_entry("x/y", "Y", -1, 2)

    def test_zero_cost_allowed_and_flagged_on_card(self):
        e = mint_catalog_entry("x/free", "Free", 0, 0)
        assert e["input_cost_per_mtok"] == 0
        from grove.config.model_catalog import _fmt_costs
        assert "$0" in _fmt_costs(e) and "verify" in _fmt_costs(e)

    # G-4 sanitize
    def test_hostile_display_name_rendered_inert(self):
        hostile = "Kimi\x1b[31mK3\nDROP\x00TABLE" + "A" * 200
        e = mint_catalog_entry("x/y", hostile, 1, 2, notes="l1\nl2\x1b[0m\x07")
        assert "\x1b" not in e["display_name"] and "\n" not in e["display_name"]
        assert "\x00" not in e["display_name"]
        assert len(e["display_name"]) <= 81  # 80-char cap + ellipsis
        assert "\x1b" not in e["notes"] and "\n" not in e["notes"]

    # G-2 TOCTOU
    def test_toctou_new_becomes_shadow_is_rejected(self, sov):
        # A repo slug stands in for "a repo entry appeared under a slug staged NEW".
        repo_slug = _load_catalog_file(
            __import__("grove.config.model_catalog", fromlist=["_repo_catalog_path"])._repo_catalog_path()
        )[0]["slug"]
        entry = mint_catalog_entry(repo_slug, "Override", 1, 2)
        with pytest.raises(CatalogWriteError, match="state drift"):
            upsert_sovereign_entry(entry, expected_origin="new")

    def test_toctou_intentional_shadow_allowed(self, sov):
        from grove.config.model_catalog import _repo_catalog_path
        repo_slug = _load_catalog_file(_repo_catalog_path())[0]["slug"]
        entry = mint_catalog_entry(repo_slug, "Override", 1, 2)
        out = upsert_sovereign_entry(entry, expected_origin="shadows_repo")
        assert any(m["slug"] == repo_slug for m in out)

    def test_toctou_new_stays_new_ok(self, sov):
        entry = mint_catalog_entry("brand/new-xyz", "New", 1, 2)
        out = upsert_sovereign_entry(entry, expected_origin="new")
        assert out[-1]["slug"] == "brand/new-xyz"

    # G-5 atomicity
    def test_write_is_tmp_fsync_replace(self, sov, monkeypatch):
        import os
        calls = {"fsync": 0, "replace": []}
        real_fsync, real_replace = os.fsync, os.replace
        monkeypatch.setattr(os, "fsync", lambda fd: (calls.__setitem__("fsync", calls["fsync"] + 1), real_fsync(fd))[1])
        monkeypatch.setattr(os, "replace", lambda src, dst: (calls["replace"].append((src, dst)), real_replace(src, dst))[1])
        write_sovereign_catalog([mint_catalog_entry("a/b", "B", 1, 2)])
        assert calls["fsync"] >= 1                      # durable flush
        assert len(calls["replace"]) == 1              # atomic swap
        src, dst = calls["replace"][0]
        assert src.endswith(".tmp") and str(dst) == str(sov)
        assert not list(sov.parent.glob("*.tmp"))      # no torn tmp left
        assert "a/b" in sov.read_text()                # content complete
