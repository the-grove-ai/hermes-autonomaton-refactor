"""The cellar: each keg is its own git repository, versions are commits, and a
keg is complete on its own (compiler-shapes-v1)."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from grove.keg_store import KegRepo, content_hash, dump_yaml, keg_id


def _manifest(data: bytes, **over):
    base = {
        "keg_id": "keg:demo:v1:aaaaaaaaaaaa", "slug": "demo", "version": 1,
        "kind": "skill", "scope": "reserved", "authority_level": "green",
        "content_file": "SKILL.md", "content_hash": content_hash(data),
        "status": "active",
    }
    base.update(over)
    return base


def _git(repo: KegRepo, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo.path), *args], capture_output=True, text=True, check=True,
    ).stdout


def test_a_keg_repo_is_a_plain_git_repo_with_versions_as_commits(tmp_path):
    repo = KegRepo.open_or_init(tmp_path / "kegs" / "demo")
    sha1 = repo.commit({"SKILL.md": b"# v1\n"}, _manifest(b"# v1\n"), "seed v1")
    sha2 = repo.commit(
        {"SKILL.md": b"# v2\n"},
        {**repo.manifest(), "version": 2, "content_hash": content_hash(b"# v2\n")},
        "seed v2",
    )
    assert [e["sha"] for e in repo.log()] == [sha1, sha2]
    assert [e["message"] for e in repo.log()] == ["seed v1", "seed v2"]
    assert repo.read("SKILL.md", sha1) == b"# v1\n"
    assert repo.manifest()["commit"] == sha2 and repo.manifest(sha1)["commit"] == sha1
    assert repo.head() == sha2
    assert (tmp_path / "kegs" / "demo" / ".git").is_dir()
    assert not (tmp_path / "kegs" / ".git").exists()
    assert _git(repo, "branch", "--list").split() == ["*", "main"]
    assert _git(repo, "remote") == ""
    assert _git(repo, "tag") == ""


def test_a_keg_is_movable_on_its_own(tmp_path, monkeypatch):
    repo = KegRepo.open_or_init(tmp_path / "a" / "demo")
    repo.commit({"SKILL.md": b"x"}, _manifest(b"x"), "seed")
    shutil.copytree(tmp_path / "a" / "demo", tmp_path / "elsewhere" / "demo")
    monkeypatch.setenv("GROVE_HOME", str(tmp_path / "unrelated"))
    moved = KegRepo(tmp_path / "elsewhere" / "demo")
    moved.verify()
    assert moved.manifest()["keg_id"] == "keg:demo:v1:aaaaaaaaaaaa"


def test_verify_refuses_a_hand_edited_keg(tmp_path):
    repo = KegRepo.open_or_init(tmp_path / "demo")
    repo.commit({"SKILL.md": b"# v1\n"}, _manifest(b"# v1\n"), "seed")
    repo.verify()
    (tmp_path / "demo" / "SKILL.md").write_bytes(b"edited\n")
    with pytest.raises(ValueError, match="keg:demo:v1"):
        repo.verify()


def test_verify_refuses_a_manifest_whose_hash_does_not_match_the_file(tmp_path):
    repo = KegRepo.open_or_init(tmp_path / "demo")
    repo.commit({"SKILL.md": b"# v1\n"}, _manifest(b"other"), "seed")
    with pytest.raises(ValueError, match="content_hash"):
        repo.verify()


def test_commit_refuses_when_nothing_changed(tmp_path):
    repo = KegRepo.open_or_init(tmp_path / "demo")
    repo.commit({"SKILL.md": b"# v1\n"}, _manifest(b"# v1\n"), "seed")
    with pytest.raises(ValueError, match="nothing to commit"):
        repo.commit({"SKILL.md": b"# v1\n"}, _manifest(b"# v1\n"), "again")


def test_keg_id_hex_is_the_content_hash():
    assert keg_id("demo", 3, "sha256:" + "ab" * 32) == "keg:demo:v3:abababababab"
    assert keg_id("demo", 3, "ab" * 32) == "keg:demo:v3:abababababab"


def test_dump_yaml_is_deterministic():
    assert dump_yaml({"b": 1, "a": [2, {"z": 1, "y": 2}]}) == dump_yaml(
        {"a": [2, {"y": 2, "z": 1}], "b": 1})
    assert dump_yaml({"name": "Halvorsen & Pike LLP"}).decode().startswith("name:")


def test_commit_never_reads_the_users_git_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "none"))
    monkeypatch.delenv("GIT_AUTHOR_NAME", raising=False)
    repo = KegRepo.open_or_init(tmp_path / "k")
    repo.commit({"f": b"1"}, _manifest(b"1", content_file="f"), "m")
    assert _git(repo, "log", "-1", "--format=%an <%ae>").strip() == "mylo <mylo@localhost>"


def test_open_without_init_reports_absence(tmp_path):
    repo = KegRepo(tmp_path / "nope")
    assert not repo.exists() and repo.head() is None and repo.manifest() == {}
