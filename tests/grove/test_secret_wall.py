"""secrets-only-wall-v1 (Hotfix 3) — the single file_safety wall: a DENY-LIST,
not a sandbox.

There is NO in-bounds confinement — the agent does legitimate file work anywhere
(project files, /tmp, IDE/ACP surfaces). ``is_secret_path`` refuses only:
  (a) sensitive SYSTEM roots (/etc, /var/log, ~/.ssh, ~/.aws, ~/.config/gcloud),
  (b) secret files/dirs (credentials/tokens/keys) WHEREVER they live — including
      inside ~/.grove, which is otherwise the most readable place.

``realpath`` canonicalizes the target FIRST, so a ``..`` traversal or a symlink
that resolves onto a secret / sensitive root is matched on the real destination.
"""
from __future__ import annotations

import os

import pytest

from grove.utils.fs_utils import is_secret_path


# ── secrets blocked WHEREVER they live (basename globs + dir anchors) ─────────

@pytest.mark.parametrize("name", [
    ".env", ".env.bak-20260101", "auth.json", "credentials.json",
    "google_client_secret.json", "google_token.json",
    "google_token.json.bak-revoked", "application_default_credentials.json",
    "my-service_account.json", "channel_directory.json", "gateway_state.json",
    ".npmrc", "pip.conf", "server.pem", "tls.key", "id_rsa", "id_rsa.pub",
])
def test_secret_basename_blocked_anywhere(tmp_path, name):
    # A secret basename is walled in an ordinary project dir, not just ~/.grove.
    p = tmp_path / "project" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x")
    assert is_secret_path(str(p)) is True


@pytest.mark.parametrize("d", ["mcp-tokens", "pairing", "secrets", ".credentials"])
def test_secret_dir_anchor_blocked_anywhere(tmp_path, d):
    p = tmp_path / "anywhere" / d / "inner.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x")
    assert is_secret_path(str(p)) is True
    assert is_secret_path(str(p.parent)) is True  # the dir itself


def test_git_config_suffix_blocked(tmp_path):
    p = tmp_path / "repo" / ".git" / "config"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("[core]\n")
    assert is_secret_path(str(p)) is True


# ── sensitive SYSTEM roots blocked (absolute-prefix) ─────────────────────────

@pytest.mark.parametrize("p", [
    "/etc/passwd", "/etc/shadow", "/var/log/auth.log",
    "~/.ssh/id_ed25519", "~/.aws/credentials", "~/.config/gcloud/foo.json",
])
def test_sensitive_root_blocked(p):
    assert is_secret_path(p) is True


# ── NON-secrets allowed anywhere (the deny-list defaults open) ───────────────

def test_grove_knowledge_and_config_allowed():
    H = os.path.expanduser("~/.grove")
    for rel in ("research/substack-draft-chinese-open-weight.md",  # THE smoke test
                "cellar/page.md", "wiki/pages/p.md", "index/cellar.db",
                "memories/m.md", "routing.config.yaml", "zones.schema.yaml",
                "dock/dock.yaml", "constitution.md", "kanban.db"):
        assert is_secret_path(f"{H}/{rel}") is False, rel


def test_out_of_grove_project_files_allowed(tmp_path):
    # Core IDE/ACP workflow — the agent reads/writes project files outside ~/.grove.
    for rel in ("project/main.py", "project/README.md", "project/src/app.ts"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        assert is_secret_path(str(p)) is False, rel
    assert is_secret_path("/tmp/scratch.txt") is False


def test_grove_root_itself_allowed():
    assert is_secret_path(os.path.expanduser("~/.grove")) is False


# ── false-positive guards (why dir anchors, not a `*secret*` substring glob) ──

def test_doc_cache_secret_bin_not_walled():
    H = os.path.expanduser("~/.grove")
    assert is_secret_path(f"{H}/cache/documents/doc_abc123_secret.bin") is False


def test_credential_skill_not_walled():
    H = os.path.expanduser("~/.grove")
    p = f"{H}/capabilities/skill__ingested__debugging-mcp-credentials__x.yaml"
    assert is_secret_path(p) is False


# ── realpath is the sole traversal guard (in-bounds is gone) ─────────────────

def test_dotdot_traversal_to_sensitive_root_blocked():
    H = os.path.expanduser("~/.grove")
    assert is_secret_path(f"{H}/../../../../../../etc/passwd") is True


def test_symlink_to_secret_blocked(tmp_path):
    # A symlink in a project dir pointing at ~/.grove/.env must realpath-resolve
    # to the secret and BLOCK (Correction 3).
    secret = os.path.expanduser("~/.grove/.env")
    link = tmp_path / "innocent.txt"
    try:
        os.symlink(secret, link)
    except OSError:
        pytest.skip("symlinks unsupported here")
    assert is_secret_path(str(link)) is True
