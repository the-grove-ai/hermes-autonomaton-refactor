"""The cellar's kegs — each one its own git repository (compiler-shapes-v1).

``$GROVE_HOME/cellar/`` is a plain directory: storage, never a repository.
Every keg under ``cellar/kegs/<slug>/`` is a git repository of its own, with
versions as commits and nothing else: no branches beyond ``main``, no tags,
no submodules, no remote. A keg is complete on its own — copy its directory
anywhere and it still verifies.

Two kinds share one manifest shape (``manifest.yaml``, keys sorted):

* ``skill`` — a vanilla Hermes ``SKILL.md``, seeded from the repo by the
  operator. The node never edits it.
* ``helper`` — the GRV-004 keg document (``keg.yaml``) Kaizen compiles from
  the operator's decisions, with its replay (``replay/v<N>.jsonl``) beside it.

A version's id is ``keg:<slug>:v<N>:<12 hex>``, the hex being the content
hash of the committed document. The manifest records the commit the version
landed in.

Pipeline stage: Execution (Kaizen writes a proposal; the engine commits a
signature) and Compilation (a goal turn reads its skill from here).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import yaml

CELLAR_DIRNAME = "cellar"
MANIFEST_FILE = "manifest.yaml"

KIND_SKILL = "skill"
KIND_HELPER = "helper"

_GIT_ENV = {
    # No user or system config is read: a keg's history is the node's, not
    # the operator's shell identity.
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "mylo",
    "GIT_AUTHOR_EMAIL": "mylo@localhost",
    "GIT_COMMITTER_NAME": "mylo",
    "GIT_COMMITTER_EMAIL": "mylo@localhost",
}


# ── paths and hashing ─────────────────────────────────────────────────


def cellar_dir(home: Optional[Path] = None) -> Path:
    if home is None:
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home())
    return Path(home) / CELLAR_DIRNAME


def kegs_dir(home: Optional[Path] = None) -> Path:
    return cellar_dir(home) / "kegs"


def content_hash(data: bytes) -> str:
    """``sha256:<hex>`` of the exact bytes of a committed document."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def keg_id(slug: str, version: int, digest: str) -> str:
    """``keg:<slug>:v<N>:<12 hex>``; ``digest`` with or without ``sha256:``."""
    hexpart = digest.split(":", 1)[1] if ":" in digest else digest
    return f"keg:{slug}:v{int(version)}:{hexpart[:12]}"


def dump_yaml(obj: Mapping[str, Any]) -> bytes:
    """The ONE serializer for every committed document: sorted keys, UTF-8,
    block style. Byte-identical for equal mappings."""
    return yaml.safe_dump(
        dict(obj), sort_keys=True, allow_unicode=True, default_flow_style=False,
    ).encode("utf-8")


def engine_commit(repo_root: Optional[Path] = None) -> Optional[str]:
    """The commit of the running checkout, read from ``.git`` without git.
    None when the code does not run from a checkout."""
    root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parent.parent
    git = root / ".git"
    try:
        if git.is_file():       # a worktree: "gitdir: <path>"
            git = Path(git.read_text(encoding="utf-8").split(":", 1)[1].strip())
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head or None
        ref = head.split(":", 1)[1].strip()
        loose = git / ref
        if loose.exists():
            return loose.read_text(encoding="utf-8").strip() or None
        common = git / "commondir"
        base = (git / common.read_text(encoding="utf-8").strip()).resolve() if common.exists() else git
        loose = base / ref
        if loose.exists():
            return loose.read_text(encoding="utf-8").strip() or None
        packed = base / "packed-refs"
        if packed.exists():
            for line in packed.read_text(encoding="utf-8").splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref:
                    return parts[0]
    except OSError:
        return None
    return None


# ── one keg, one repository ───────────────────────────────────────────


class KegRepo:
    """One keg's git repository. Versions are commits on ``main``."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    # -- git plumbing ----------------------------------------------------

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self.path), *args],
            capture_output=True, text=True, check=check,
            env={**os.environ, **_GIT_ENV},
        )

    @classmethod
    def open_or_init(cls, path: Path) -> "KegRepo":
        repo = cls(path)
        if not repo.exists():
            repo.path.mkdir(parents=True, exist_ok=True)
            repo._git("init", "-q", "--initial-branch=main")
            repo._git("config", "user.name", "mylo")
            repo._git("config", "user.email", "mylo@localhost")
        return repo

    def exists(self) -> bool:
        return (self.path / ".git").is_dir()

    def head(self) -> Optional[str]:
        if not self.exists():
            return None
        out = self._git("rev-parse", "--verify", "-q", "HEAD", check=False)
        return out.stdout.strip() or None

    # -- reading ---------------------------------------------------------

    def read(self, path: str, ref: str = "HEAD") -> bytes:
        """The committed bytes of ``path`` at ``ref``."""
        out = subprocess.run(
            ["git", "-C", str(self.path), "show", f"{ref}:{path}"],
            capture_output=True, check=True, env={**os.environ, **_GIT_ENV},
        )
        return out.stdout

    def manifest(self, ref: str = "HEAD") -> Dict[str, Any]:
        """The manifest at ``ref`` with ``commit`` filled in: the last commit
        that touched it at or before ``ref``. ``{}`` when there is none."""
        if self.head() is None:
            return {}
        try:
            raw = self.read(MANIFEST_FILE, ref)
        except subprocess.CalledProcessError:
            return {}
        data = yaml.safe_load(raw.decode("utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{self.path}: {MANIFEST_FILE} at {ref} is not a mapping")
        landed = self._git("log", "-1", "--format=%H", ref, "--", MANIFEST_FILE).stdout.strip()
        data["commit"] = landed or None
        return data

    def log(self) -> List[Dict[str, Any]]:
        """Every commit, oldest first: ``{sha, message, manifest}``."""
        if self.head() is None:
            return []
        out = self._git("log", "--reverse", "--format=%H%x1f%B%x1e").stdout
        entries = []
        for chunk in out.split("\x1e"):
            if not chunk.strip():
                continue
            sha, message = chunk.lstrip("\n").split("\x1f", 1)
            entries.append({"sha": sha.strip(), "message": message.strip(),
                            "manifest": self.manifest(sha.strip())})
        return entries

    def diff(self, a: str, b: str) -> str:
        return self._git("diff", a, b, "--").stdout

    # -- writing ---------------------------------------------------------

    def commit(self, files: Mapping[str, bytes], manifest: Mapping[str, Any],
               message: str) -> str:
        """Write ``files`` and the manifest, commit, return the sha. The
        manifest's ``commit`` is never stored (it is the commit being made)."""
        if not self.exists():
            raise ValueError(f"{self.path} is not a keg repository")
        for rel, data in files.items():
            target = self.path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        stored = {k: v for k, v in dict(manifest).items() if k != "commit"}
        (self.path / MANIFEST_FILE).write_bytes(dump_yaml(stored))
        self._git("add", "-A")
        if not self._git("status", "--porcelain").stdout.strip():
            raise ValueError(f"{self.path}: nothing to commit")
        self._git("commit", "-q", "-m", message)
        return self.head() or ""

    # -- integrity -------------------------------------------------------

    def verify(self, ref: str = "HEAD") -> None:
        """The manifest's ``content_hash`` is the hash of its ``content_file``
        at ``ref``, and (for HEAD) the working tree matches the commit.
        ValueError naming the keg otherwise."""
        manifest = self.manifest(ref)
        if not manifest:
            raise ValueError(f"{self.path}: no manifest at {ref}")
        kid = manifest.get("keg_id", "?")
        content_file = str(manifest.get("content_file") or "")
        if not content_file:
            raise ValueError(f"{kid}: manifest names no content_file")
        committed = self.read(content_file, ref)
        if content_hash(committed) != manifest.get("content_hash"):
            raise ValueError(
                f"{kid}: content_hash {manifest.get('content_hash')!r} does not match "
                f"{content_file} at {ref} ({content_hash(committed)})")
        if ref == "HEAD":
            on_disk = self.path / content_file
            if not on_disk.exists() or on_disk.read_bytes() != committed:
                raise ValueError(
                    f"{kid}: {content_file} on disk differs from the committed version "
                    f"(hand-edited?)")
            if self._git("status", "--porcelain").stdout.strip():
                raise ValueError(f"{kid}: the keg's working tree has uncommitted changes")


# ── the skill keg: seeded from the repo, never edited on the node ────────


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def skill_repo_path(slug: str, home: Optional[Path] = None) -> Path:
    return kegs_dir(home) / f"{slug}-skill"


def seed_skill(slug: str, *, source: Optional[Path] = None,
               home: Optional[Path] = None) -> Dict[str, Any]:
    """Seed (or re-seed) the skill ``slug`` from the repo's ``SKILL.md`` into
    its own keg. Unchanged content is a no-op that returns the HEAD manifest;
    changed content lands as the next version. Returns the manifest."""
    src = Path(source) if source is not None else _repo_root() / "skills" / slug / "SKILL.md"
    data = src.read_bytes()
    digest = content_hash(data)
    repo = KegRepo.open_or_init(skill_repo_path(slug, home))
    current = repo.manifest()
    if current and current.get("content_hash") == digest:
        return current
    version = int(current.get("version") or 0) + 1
    manifest = {
        "keg_id": keg_id(f"{slug}-skill", version, digest),
        "slug": f"{slug}-skill",
        "version": version,
        "kind": KIND_SKILL,
        "scope": "reserved",
        "authority_level": "green",
        "content_file": "SKILL.md",
        "content_hash": digest,
        "status": "active",
        "skill": slug,
        "source": f"skills/{slug}/SKILL.md",
    }
    repo.commit({"SKILL.md": data}, manifest, f"seed {slug} v{version}: {digest}")
    return repo.manifest()


def skill_kegs_for_goal(goal_id: str, *, home: Optional[Path] = None,
                        dock: Any = None) -> List[Dict[str, Any]]:
    """The skill kegs a Dock goal unlocks, verified, in declaration order:
    ``{slug, keg_id, version, content_hash, commit, body}``. A goal the Dock
    does not know, or a declared skill with no keg, is a LookupError; a
    keg whose content does not match its manifest is a ValueError (verify)."""
    from grove.dock import _parse_frontmatter, load_dock

    if dock is None:
        dock = load_dock()
    goal = next((g for g in (getattr(dock, "goals", None) or ()) if g.id == goal_id), None)
    if goal is None:
        raise LookupError(f"the Dock declares no goal {goal_id!r}")
    out: List[Dict[str, Any]] = []
    for slug in goal.unlocked_skills:
        repo = KegRepo(skill_repo_path(slug, home))
        if not repo.exists() or repo.head() is None:
            raise LookupError(
                f"goal {goal_id!r} unlocks skill {slug!r} but there is no keg at "
                f"{repo.path} — seed it: python -m grove.keg_store seed-skill {slug}")
        repo.verify()
        manifest = repo.manifest()
        _, body = _parse_frontmatter(repo.read(str(manifest["content_file"])).decode("utf-8"))
        out.append({
            "slug": slug,
            "keg_id": manifest["keg_id"],
            "version": int(manifest["version"]),
            "content_hash": manifest["content_hash"],
            "commit": manifest["commit"],
            "body": body,
        })
    return out


def _main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m grove.keg_store")
    sub = parser.add_subparsers(dest="command", required=True)
    seed = sub.add_parser("seed-skill", help="seed a repo skill into the cellar as a keg")
    seed.add_argument("slug")
    seed.add_argument("--source", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.command == "seed-skill":
        manifest = seed_skill(args.slug, source=args.source)
        print(dump_yaml(manifest).decode("utf-8"), end="")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
