"""Sprint 69 — load-time secrets-hygiene Andon.

config.yaml must not contain secret literals; secrets live in ~/.grove/.env
and are referenced as ${VAR}. ``_andon_secret_literals`` halts loudly when a
raw secret appears in the parsed config, and ``load_config`` invokes it
OUTSIDE the graceful parse-failure handler so it cannot be swallowed.
"""

from __future__ import annotations

import textwrap

import pytest

from hermes_cli.config import (
    ConfigSecretAndon,
    _andon_secret_literals,
    load_config,
)


# ── the walker, in isolation ──────────────────────────────────────────────


class TestAndonWalker:
    def test_value_shape_notion_token_in_mcp_env_andons(self):
        # The exact Sprint 69 bug: an ntn_ literal in mcp_servers.*.env.
        cfg = {"mcp_servers": {"notion": {"env": {
            "NOTION_TOKEN": "ntn_FAKEtesttokenNOTAREALSECRET000000"
        }}}}
        with pytest.raises(ConfigSecretAndon, match="NOTION_TOKEN"):
            _andon_secret_literals(cfg, "config.yaml")

    def test_value_shape_openai_key_andons(self):
        cfg = {"providers": {"x": {"api_key": "sk-ant-abcdef0123456789xyz"}}}
        with pytest.raises(ConfigSecretAndon):
            _andon_secret_literals(cfg, "config.yaml")

    def test_envvar_named_secret_literal_andons(self):
        cfg = {"OPENROUTER_API_KEY": "or-aaaaaaaaaaaaaaaaaaaa"}
        with pytest.raises(ConfigSecretAndon):
            _andon_secret_literals(cfg, "config.yaml")

    def test_lowercase_api_key_with_shaped_value_andons(self):
        # A real-looking credential under a lowercase api_key is caught by
        # the value-shape signal (sk- prefix), even though the key name is
        # not flagged on its own.
        cfg = {"model": {"api_key": "sk-ant-0123456789abcdefghij"}}
        with pytest.raises(ConfigSecretAndon):
            _andon_secret_literals(cfg, "config.yaml")

    def test_lowercase_api_key_with_generic_literal_passes(self):
        # Deliberate scoping (Sprint 69): a generic literal under lowercase
        # api_key/token is NOT flagged — model.api_key and custom-provider
        # api_key are real config slots. Only the value-shape and env-var-key
        # signals fire, so non-credential-shaped values pass.
        _andon_secret_literals({"model": {"api_key": "test-key"}}, "config.yaml")
        _andon_secret_literals({"provider": {"token": "plain-token"}}, "config.yaml")

    @pytest.mark.parametrize("cfg", [
        {"provider": {"api_key": "${OPENAI_API_KEY}"}},   # env-ref
        {"provider": {"api_key": ""}},                     # empty
        {"ui": {"record_key": "ctrl+b"}},                  # keybinding, not a secret
        {"security": {"redact_secrets": True}},            # bool
        {"voice": {"show_token_count": False}},            # bool
        {"mcp_servers": {"notion": {                       # the hosted OAuth block
            "url": "https://mcp.notion.com/mcp",
            "auth": "oauth",
            "oauth": {"client_name": "Grove Autonomaton", "redirect_port": 8765},
        }}},
    ])
    def test_clean_configs_pass(self, cfg):
        _andon_secret_literals(cfg, "config.yaml")  # must not raise


# ── load_config integration: the Andon is not swallowed ───────────────────


class TestLoadConfigAndon:
    def test_secret_literal_halts_load(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GROVE_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text(
            textwrap.dedent(
                """\
                mcp_servers:
                  notion:
                    command: npx
                    env:
                      NOTION_TOKEN: ntn_FAKEtesttokenNOTAREALSECRET000000
                """
            ),
            encoding="utf-8",
        )
        with pytest.raises(ConfigSecretAndon, match="NOTION_TOKEN"):
            load_config()

    def test_env_ref_config_loads_clean(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GROVE_HOME", str(tmp_path))
        monkeypatch.setenv("NOTION_TOKEN", "ntn_realvalue_kept_out_of_config")
        (tmp_path / "config.yaml").write_text(
            textwrap.dedent(
                """\
                custom_providers:
                  - name: notion-ish
                    base_url: https://example.com
                    api_key: ${NOTION_TOKEN}
                    model: x
                """
            ),
            encoding="utf-8",
        )
        cfg = load_config()  # must not raise; ${VAR} expands for runtime
        assert cfg["custom_providers"][0]["api_key"] == "ntn_realvalue_kept_out_of_config"
