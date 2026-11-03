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
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import yaml

CELLAR_DIRNAME = "cellar"
MANIFEST_FILE = "manifest.yaml"

logger = logging.getLogger(__name__)

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
        """The HEAD commit, or None for a repository with no commit yet. A
        git error (a corrupt repository, a permissions fault, dubious
        ownership) is raised, never read as "empty"."""
        if not self.exists():
            return None
        out = self._git("rev-parse", "--verify", "-q", "HEAD", check=False)
        if out.returncode == 0:
            return out.stdout.strip() or None
        if out.stderr.strip() or out.returncode != 1:
            raise RuntimeError(
                f"{self.path}: git could not read HEAD (exit {out.returncode}): "
                f"{out.stderr.strip() or 'no message'}")
        return None

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


def helper_document(spec: Mapping[str, Any], lineage: Any) -> bytes:
    """The committed keg document: the GRV-004 spec plus the run it was
    earned in. Its bytes are the version's hash. The turns and decisions it
    rests on are provenance and live in the manifest, so two versions of one
    keg differ in the document only where the rules differ."""
    return dump_yaml({**dict(spec), "lineage": lineage})


def helper_document_hash(spec: Mapping[str, Any], lineage: Any) -> str:
    return content_hash(helper_document(spec, lineage))


TRANSITIONS_FILE = "transitions.jsonl"


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


def _replay_text(tallies: Optional[Mapping[str, Any]]) -> str:
    if not tallies or tallies.get("replayed") is None:
        return "replay none on record"      # an imported, pre-cellar version
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
    feedback: Any, tool_args: Optional[Mapping[str, Any]] = None,
    home: Optional[Path] = None,
) -> Dict[str, Any]:
    """Commit a proposed helper version: ``keg.yaml``, its replay and the
    manifest (status ``proposed``). Returns the manifest."""
    document = helper_document(spec, lineage)
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
        "name": spec.get("name"),
        "compiled_by": {"kaizen": "grove.eval.pattern_compiler",
                        "engine_commit": engine_commit()},
        "compiled_under": compiled_under_for_goal(goal, home=home),
        "evidence": [str(d) for d in (evidence_decisions or ())],
        "evidence_turns": sorted(str(t) for t in (evidence_turn_ids or ())),
        "evidence_hash": evidence_hash,
        "approved_signature": approved_signature,
        "proposal_id": proposal_id,
        "replaces": [str(r) for r in (replaces or ())],
        # The version this draft revises, as proposed (the cache row's
        # ``supersedes``); ``replaces`` is settled at signature.
        "supersedes": (str(list(replaces)[0]) if replaces else None),
        "replay": {"file": replay_file, **tallies},
        "proposed_at": now,
        "signed_at": None,
        "signed_by": None,
        "imported": False,
        # What the build cache needs to be rebuilt from this manifest alone.
        "tool": tool,
        "tool_args": {k: v for k, v in (tool_args or {}).items() if k != "keg"},
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


def check_signable(slug: str, pattern_id: str, *, home: Optional[Path] = None) -> Dict[str, Any]:
    """The version at the keg's HEAD must be the one being signed, or
    ValueError. Called BEFORE anything is written, so a refusal leaves no
    half-signed state behind."""
    repo = KegRepo(helper_repo_path(slug, home))
    current = repo.manifest() if repo.exists() else {}
    if not current or current.get("keg_id") != pattern_id:
        raise ValueError(
            f"{pattern_id}: the keg at {repo.path} has {current.get('keg_id')!r} at HEAD, "
            f"not the version being signed")
    return current


def sign_helper(*, slug: str, pattern_id: str, proposal_id: str, signed_by: str,
                signed_at: str, replaces: Any, home: Optional[Path] = None) -> Dict[str, Any]:
    """The operator signed the version at HEAD: commit its manifest as
    ``active``. HEAD must be that version, or ValueError."""
    repo = KegRepo(helper_repo_path(slug, home))
    current = check_signable(slug, pattern_id, home=home)
    manifest = {**current, "status": "active", "signed_by": signed_by, "signed_at": signed_at,
                "proposal_id": proposal_id, "replaces": [str(r) for r in (replaces or ())]}
    tallies = {k: v for k, v in (current.get("replay") or {}).items() if k != "file"}
    message = (f"sign v{manifest['version']}: proposal {proposal_id}; signed_by {signed_by}; "
               f"{_replay_text(tallies)}; "
               f"supersedes {', '.join(manifest['replaces']) or 'nothing'}")
    repo.commit({}, manifest, message)
    return repo.manifest()


# ── the build cache: derived from the cellar ─────────────────────────────

import contextvars
import contextlib
import subprocess as _subprocess
from datetime import datetime, timezone

_signing: contextvars.ContextVar[bool] = contextvars.ContextVar("keg_signing", default=False)


@contextlib.contextmanager
def signing():
    """Inside: cache status changes are the signature's own (active, and the
    superseded versions), recorded by the sign commit and derived on
    rebuild — not as transitions."""
    token = _signing.set(True)
    try:
        yield
    finally:
        _signing.reset(token)


def _slug_of_row(row: Any) -> str:
    from grove.keg import keg_record
    slug = ((keg_record(row).get("keg") or {}).get("slug"))
    if not slug:
        parts = str(row.pattern_id).split(":")
        slug = parts[1] if len(parts) > 2 else ""
    return str(slug)


def note_cache_transition(store: Any, pattern_id: str, *, status: Optional[str] = None,
                          promoted_at: Optional[str] = None,
                          promotion_evidence: Optional[str] = None,
                          home: Optional[Path] = None) -> Optional[str]:
    """A cache row's status or bookkeeping changed outside a signature (a
    halt, a demotion, a rejection, feedback): append it to the keg's
    ``transitions.jsonl`` and commit, so a rebuild replays it. Returns the
    commit, or None when the row is not a keg the cellar knows (a pre-cellar
    row before import; a signature in progress)."""
    if _signing.get() or not str(pattern_id).startswith("keg:"):
        return None
    row = store.get(pattern_id)
    if row is None:
        return None
    repo = KegRepo(helper_repo_path(_slug_of_row(row), home))
    if not repo.exists() or repo.head() is None or pattern_id not in repo.keg_ids():
        return None
    line = {"keg_id": pattern_id, "at": datetime.now(timezone.utc).isoformat()}
    if status is not None:
        line["status"] = status
    if promoted_at is not None:
        line["promoted_at"] = promoted_at
    if promotion_evidence is not None:
        line["promotion_evidence"] = promotion_evidence
    path = repo.path / TRANSITIONS_FILE
    existing = path.read_bytes() if path.exists() else b""
    data = existing + (json.dumps(line, sort_keys=True, default=str) + "\n").encode("utf-8")
    what = f"status {pattern_id} → {status}" if status else f"evidence {pattern_id}"
    return repo.commit({TRANSITIONS_FILE: data}, repo.manifest(), what)


def _keg_ids(self: "KegRepo") -> List[str]:
    """Every version id this keg's history names, oldest first."""
    if self.head() is None:
        return []
    shas = self._git("log", "--reverse", "--format=%H", "--", MANIFEST_FILE).stdout.split()
    out: List[str] = []
    for sha in shas:
        try:
            data = yaml.safe_load(self.read(MANIFEST_FILE, sha).decode("utf-8")) or {}
        except _subprocess.CalledProcessError:
            continue
        kid = data.get("keg_id")
        if kid and kid not in out:
            out.append(str(kid))
    return out


KegRepo.keg_ids = _keg_ids


def rows_from_repo(repo: KegRepo) -> List[Any]:
    """Replay a helper keg's history into cache rows: a proposal commit makes
    a suspended row; a sign commit makes it active (and supersedes the active
    or halted versions of the same lineage, as the signature did); an
    imported commit is the row as it was; transitions apply in commit order.
    ``hit_count`` and ``last_hit_at`` are runtime counters, not in the cellar."""
    from grove.intent_store import normalize_message_stem
    from grove.pattern_cache import CompiledPattern, STATUS_ACTIVE, STATUS_HALTED, STATUS_SUPERSEDED, STATUS_SUSPENDED, t0_key

    rows: Dict[str, Dict[str, Any]] = {}
    seen_transitions = 0
    for entry in repo.log():
        sha, m = entry["sha"], entry["manifest"]
        if m.get("kind") != KIND_HELPER:
            continue
        kid = str(m["keg_id"])
        if m.get("imported"):
            if kid not in rows:
                rows[kid] = {**dict(m["cache_row"]), "hit_count": 0, "last_hit_at": None}
        elif kid not in rows:
            doc = yaml.safe_load(repo.read("keg.yaml", sha).decode("utf-8")) or {}
            spec = {k: v for k, v in doc.items() if k != "lineage"}
            args = {**dict(m.get("tool_args") or {}), "keg": spec}
            invocation = {"tool": m["tool"], "args": args,
                          "approved_signature": m["approved_signature"]}
            record = {
                "keg": {"name": spec.get("name"), "slug": m["slug"], "version": int(m["version"]),
                        "lineage": doc.get("lineage"), "dock_goal": spec.get("dock_goal")},
                "andon_id": m.get("andon_id"), "repetition_count": m.get("repetition_count"),
                "flag": m.get("flag"), "supersedes": m.get("supersedes"),
                "feedback": list(m.get("feedback") or []),
            }
            rows[kid] = dict(
                pattern_id=kid,
                t0_key=t0_key(str(m["intent_class"]),
                              normalize_message_stem(str((spec.get("trigger") or {}).get("request") or ""))),
                intent_class=str(m["intent_class"]), cacheable_type="executable",
                cached_response=None, compiled_invocation=json.dumps(invocation, sort_keys=True),
                evidence_hash=m["evidence_hash"], status=STATUS_SUSPENDED,
                created_at=m["proposed_at"], promoted_at=None, last_hit_at=None, hit_count=0,
                promotion_evidence=json.dumps(record, sort_keys=True),
            )
        row = rows[kid]
        if m.get("signed_at") and m.get("status") == "active" and row["promoted_at"] is None:
            row["status"] = STATUS_ACTIVE
            row["promoted_at"] = m["signed_at"]
            record = json.loads(row["promotion_evidence"])
            record["signed"] = {"by": m.get("signed_by"), "at": m["signed_at"],
                                "proposal_id": m.get("proposal_id")}
            row["promotion_evidence"] = json.dumps(record, sort_keys=True)
            lineage = (record.get("keg") or {}).get("lineage")
            for other_id, other in rows.items():
                if other_id == kid:
                    continue
                okeg = (json.loads(other["promotion_evidence"] or "{}").get("keg") or {})
                if (okeg.get("slug") == m["slug"] and okeg.get("lineage") == lineage
                        and other["status"] in (STATUS_ACTIVE, STATUS_HALTED)):
                    other["status"] = STATUS_SUPERSEDED
        try:
            lines = repo.read(TRANSITIONS_FILE, sha).decode("utf-8").splitlines()
        except _subprocess.CalledProcessError:
            lines = []
        for raw in lines[seen_transitions:]:
            t = json.loads(raw)
            target = rows.get(str(t.get("keg_id")))
            if target is None:
                continue
            for key in ("status", "promoted_at", "promotion_evidence"):
                if t.get(key) is not None:
                    target[key] = t[key]
        seen_transitions = len(lines)
    return [CompiledPattern(**r) for r in rows.values()]


_COUNTERS = ("hit_count", "last_hit_at")


def import_cache_rows(store: Any, *, home: Optional[Path] = None) -> List[str]:
    """Pre-cellar keg rows (no version of theirs in any keg repository) are
    imported into their slug's repository, one commit each in ``created_at``
    order, carrying the row so a rebuild reproduces it. Idempotent."""
    from dataclasses import asdict
    from grove.keg import keg_of, keg_record

    known: Dict[str, List[str]] = {}
    imported: List[str] = []
    rows = [r for r in store.all() if str(r.pattern_id).startswith("keg:")]
    rows.sort(key=lambda r: (r.created_at or "", r.pattern_id))
    for row in rows:
        slug = _slug_of_row(row)
        if slug not in known:
            repo = KegRepo(helper_repo_path(slug, home))
            known[slug] = repo.keg_ids() if repo.exists() else []
        if row.pattern_id in known[slug]:
            continue
        spec = keg_of(row)
        if not spec:
            logger.warning(
                "[keg_store] %s: no keg document in its compiled_invocation — not imported, "
                "and not rebuilt from the cellar; the row stays cache-native", row.pattern_id)
            continue
        record = keg_record(row)
        keg_meta = record.get("keg") or {}
        document = helper_document(spec, keg_meta.get("lineage"))
        invocation = json.loads(row.compiled_invocation or "{}")
        cache_row = {k: v for k, v in asdict(row).items() if k not in _COUNTERS}
        manifest = {
            "keg_id": row.pattern_id,
            "slug": slug,
            "version": int(spec.get("version") or keg_meta.get("version") or 0),
            "kind": KIND_HELPER,
            "scope": spec.get("scope"),
            "authority_level": spec.get("authority_level"),
            "content_file": "keg.yaml",
            "content_hash": content_hash(document),
            "status": row.status,
            "goal": spec.get("dock_goal"),
            "name": spec.get("name"),
            "compiled_by": None,
            "compiled_under": None,
            "evidence": [],
            "evidence_turns": [],
            "evidence_hash": row.evidence_hash,
            "approved_signature": invocation.get("approved_signature"),
            "proposal_id": (record.get("signed") or {}).get("proposal_id"),
            "replaces": [record["supersedes"]] if record.get("supersedes") else [],
            "supersedes": record.get("supersedes"),
            "replay": None,
            "proposed_at": row.created_at,
            "signed_at": row.promoted_at,
            "signed_by": (record.get("signed") or {}).get("by"),
            "imported": True,
            "imported_from": "pattern_cache.db",
            "cache_row": cache_row,
            "tool": invocation.get("tool"),
            "tool_args": {k: v for k, v in (invocation.get("args") or {}).items() if k != "keg"},
            "intent_class": row.intent_class,
            "andon_id": record.get("andon_id"),
            "flag": record.get("flag"),
            "repetition_count": record.get("repetition_count"),
            "feedback": list(record.get("feedback") or []),
        }
        repo = KegRepo.open_or_init(helper_repo_path(slug, home))
        repo.commit({"keg.yaml": document}, manifest,
                    f"import {row.pattern_id} from pattern_cache.db (pre-cellar, status {row.status})")
        known[slug].append(row.pattern_id)
        imported.append(row.pattern_id)
    return imported


def sync_cache(*, home: Optional[Path] = None, store: Any = None) -> Dict[str, int]:
    """At start-up: import pre-cellar keg rows, then rebuild every keg row the
    cellar holds that the cache does not. Returns the counts."""
    from grove.pattern_cache import PatternCacheStore

    store = store if store is not None else PatternCacheStore()
    imported = import_cache_rows(store, home=home)
    rebuilt = 0
    root = kegs_dir(home)
    if root.is_dir():
        for path in sorted(p for p in root.iterdir() if p.is_dir()):
            repo = KegRepo(path)
            if not repo.exists() or repo.head() is None:
                continue
            if repo.manifest().get("kind") != KIND_HELPER:
                continue
            for row in rows_from_repo(repo):
                if store.get(row.pattern_id) is None:
                    store.upsert(row)
                    rebuilt += 1
    return {"imported": len(imported), "rebuilt": rebuilt}
