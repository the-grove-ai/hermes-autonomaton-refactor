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
import json
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


# ── the helper keg: what Kaizen compiles, and the operator signs ─────────


def helper_repo_path(slug: str, home: Optional[Path] = None) -> Path:
    return kegs_dir(home) / slug


def helper_document(spec: Mapping[str, Any], lineage: Any,
                    evidence_turn_ids: Any) -> bytes:
    """The committed keg document: the GRV-004 spec plus the run it was
    earned in and the turns it rests on. Its bytes are the version's hash."""
    return dump_yaml({
        **dict(spec),
        "lineage": lineage,
        "evidence": sorted(str(e) for e in (evidence_turn_ids or ())),
    })


def helper_document_hash(spec: Mapping[str, Any], lineage: Any,
                         evidence_turn_ids: Any) -> str:
    return content_hash(helper_document(spec, lineage, evidence_turn_ids))


_REPLAY_STATUS = {"unchanged": "held", "would_change": "would_change",
                  "not_covered": "reserved"}


def replay_lines(backtest: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """One line per replayed decision, in the replay's own order:
    ``{item_id, decided_id, expected, got, status, reserve_reason}`` with
    ``status`` held / would_change / reserved."""
    out = []
    for case in backtest.get("cases") or ():
        expected = case.get("confirmed")
        if expected is None:
            expected = case.get("served")
        out.append({
            "item_id": case.get("ref"),
            "decided_id": case.get("decided_id"),
            "expected": dict(expected) if isinstance(expected, Mapping) else expected,
            "got": dict(case["keg"]) if isinstance(case.get("keg"), Mapping) else case.get("keg"),
            "status": _REPLAY_STATUS[case["result"]],
            "reserve_reason": (str(case.get("note")) or None) if case.get("note") else None,
        })
    return out


def _replay_tallies(lines: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {"held": 0, "would_change": 0, "reserved": 0}
    for line in lines:
        counts[line["status"]] += 1
    return {"replayed": len(lines), **counts}


def _replay_text(tallies: Mapping[str, int]) -> str:
    return (f"replay {tallies['replayed']} replayed, {tallies['held']} held, "
            f"{tallies['would_change']} would change, {tallies['reserved']} reserved")


def compiled_under_for_goal(goal: str, *, home: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """The skill keg a goal's helpers are compiled under, projected to
    ``{skill_keg_id, skill_version, skill_content_hash, skill_commit}``; None
    when the Dock does not know the goal or it unlocks no skill. A declared
    skill that is missing or hand-edited raises (the turn could not have
    run without it)."""
    from grove.dock import load_dock

    dock = load_dock()
    if dock is None or not any(g.id == goal for g in dock.goals):
        return None
    skills = skill_kegs_for_goal(goal, home=home, dock=dock)
    if not skills:
        return None
    first = skills[0]
    return {"skill_keg_id": first["keg_id"], "skill_version": first["version"],
            "skill_content_hash": first["content_hash"], "skill_commit": first["commit"]}


def write_helper_proposal(
    *, slug: str, spec: Mapping[str, Any], lineage: Any, evidence_turn_ids: Any,
    evidence_decisions: Any, backtest: Mapping[str, Any], pattern_id: str,
    evidence_hash: str, approved_signature: str, proposal_id: str, replaces: Any,
    goal: str, now: str, tool: str, intent_class: str, andon_id: Any, flag: str,
    feedback: Any, home: Optional[Path] = None,
) -> Dict[str, Any]:
    """Commit a proposed helper version: ``keg.yaml``, its replay and the
    manifest (status ``proposed``). Returns the manifest."""
    document = helper_document(spec, lineage, evidence_turn_ids)
    digest = content_hash(document)
    if keg_id(slug, int(spec["version"]), digest) != pattern_id:
        raise ValueError(
            f"{pattern_id}: the id's hex is not the content hash of the keg document "
            f"({digest})")
    lines = replay_lines(backtest)
    tallies = _replay_tallies(lines)
    replay_file = f"replay/v{int(spec['version'])}.jsonl"
    manifest = {
        "keg_id": pattern_id,
        "slug": slug,
        "version": int(spec["version"]),
        "kind": KIND_HELPER,
        "scope": spec.get("scope"),
        "authority_level": spec.get("authority_level"),
        "content_file": "keg.yaml",
        "content_hash": digest,
        "status": "proposed",
        "goal": goal,
        "compiled_by": {"kaizen": "grove.eval.pattern_compiler",
                        "engine_commit": engine_commit()},
        "compiled_under": compiled_under_for_goal(goal, home=home),
        "evidence": [str(d) for d in (evidence_decisions or ())],
        "evidence_hash": evidence_hash,
        "approved_signature": approved_signature,
        "proposal_id": proposal_id,
        "replaces": [str(r) for r in (replaces or ())],
        "replay": {"file": replay_file, **tallies},
        "proposed_at": now,
        "signed_at": None,
        "signed_by": None,
        "imported": False,
        # What the build cache needs to be rebuilt from this manifest alone.
        "tool": tool,
        "intent_class": intent_class,
        "andon_id": andon_id,
        "flag": flag,
        "repetition_count": len(list(evidence_turn_ids or ())),
        "feedback": list(feedback or []),
    }
    repo = KegRepo.open_or_init(helper_repo_path(slug, home))
    replay_bytes = "".join(json.dumps(l, sort_keys=True, default=str) + "\n" for l in lines)
    message = (f"propose v{manifest['version']}: proposal {proposal_id}; {_replay_text(tallies)}; "
               f"supersedes {', '.join(manifest['replaces']) or 'nothing'}")
    repo.commit({"keg.yaml": document, replay_file: replay_bytes.encode("utf-8")},
                manifest, message)
    return repo.manifest()


def sign_helper(*, slug: str, pattern_id: str, proposal_id: str, signed_by: str,
                signed_at: str, replaces: Any, home: Optional[Path] = None) -> Dict[str, Any]:
    """The operator signed the version at HEAD: commit its manifest as
    ``active``. HEAD must be that version, or ValueError."""
    repo = KegRepo(helper_repo_path(slug, home))
    current = repo.manifest()
    if not current or current.get("keg_id") != pattern_id:
        raise ValueError(
            f"{pattern_id}: the keg at {repo.path} has {current.get('keg_id')!r} at HEAD, "
            f"not the version being signed")
    manifest = {**current, "status": "active", "signed_by": signed_by, "signed_at": signed_at,
                "proposal_id": proposal_id, "replaces": [str(r) for r in (replaces or ())]}
    tallies = {k: int(v) for k, v in (current.get("replay") or {}).items() if k != "file"}
    message = (f"sign v{manifest['version']}: proposal {proposal_id}; signed_by {signed_by}; "
               f"{_replay_text(tallies)}; "
               f"supersedes {', '.join(manifest['replaces']) or 'nothing'}")
    repo.commit({}, manifest, message)
    return repo.manifest()
