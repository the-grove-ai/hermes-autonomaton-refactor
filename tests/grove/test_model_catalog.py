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
