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
    _validate_catalog,
    assert_safe_catalog_mutation,
    load_catalog,
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
