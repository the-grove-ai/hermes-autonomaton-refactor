"""disposition-gate-v1 · P1 — THE GUARD (conformance, AST, repo-wide).

Enforces the locked rule (SPEC 3a7780a78eef81908ee1fabefc26e31c):

    A memory-proposal producer's re-emission guard must not consult ONLY
    non-terminal statuses. A guard keyed on ``pending``/``processing`` alone
    goes blind the instant the operator disposes of a subject (rejected /
    dismissed), and the next sweep re-emits it. A disposition must bind the
    producer: the guard MUST consult at least one terminal disposition.

Two structural moves:

  ENROLLMENT (auto, by fingerprint — R-17). A "memory-proposal producer" is
    any module that CONSTRUCTS a memory-proposal record: a dict literal
    carrying ``"status": "pending"`` together with a ``"proposal"`` key. This
    is the emission shape of detector.py / freshness.py / graduation.py. A NEW
    producer added later inherits the guard the moment it emits that shape —
    no one edits this file, and no module name is hardcoded (R-17: a guard
    that names the three instances re-commits the defect it exists to prevent).

  THE INVARIANT. Inside an enrolled producer, every comparison of a proposal's
    ``status`` field must reference at least one TERMINAL status. A comparison
    whose status literals are wholly within the NON-TERMINAL set — it consults
    liveness but never a disposition — is a violation.

VOCABULARY IS UNAVOIDABLE (SPEC last constraint, answered). The distinction
"consults a terminal disposition" vs "consults pending only" CANNOT be made
structurally without knowing the status strings: the meaning lives in the
values, not the syntax. So the guard names the vocabulary below. That the
distinction requires the vocabulary is exactly what shapes P2's config surface
(the config declares which statuses are terminal).

Positive control (SPEC, mandatory): the detector MUST flag a synthetic
pending-only producer guard and MUST NOT flag a terminal-aware one or a
non-producer reader. If the control fails, the guard is broken, not the
codebase. The control runs against embedded snippets so it survives P2's fix.

This file is a TEST ONLY. It writes nothing, changes no detector, proposes no
fix. At P1 the census test is expected to be RED: its failure list IS the
census this sprint works against.
"""
from __future__ import annotations

import ast
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

import pytest

# ── repo geometry ────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parents[2]

# Never swept: vendored envs, caches, VCS, and the test corpus (a test may
# embed a producer-shaped fixture by design). Everything else IS swept, so a
# producer added anywhere under the repo auto-enrolls.
EXCLUDE_DIR_PARTS = {
    ".venv", "venv", "__pycache__", ".git", "node_modules", "build", "dist",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "site-packages", "tests",
}

# ── the status vocabulary (SPEC last constraint — the guard MUST know it) ──
# R-24: ONE definition, in code, imported by the guard AND the detectors. The
# guard cannot classify "consults a terminal disposition" vs "pending only"
# without the strings — the meaning is in the values, not the syntax — so the
# vocabulary is shared, never re-typed here.
# R-15: the structural gate reads the terminal values; the T1 advisory
# (_recently_rejected) keeps reading "rejected" only. Two mechanisms, two
# semantics — the guard pins the structural wall, not the advisory.
from grove.memory.dispositions import (  # noqa: E402
    NON_TERMINAL_STATUSES,
    TERMINAL_STATUSES,
)

# ── R-25/R-26 · exemptions (exact-match on EXPRESSION, line-drift immune) ──
# A flagged site that is INTENTIONALLY liveness-only, with a written reason.
# Keyed on (path, enclosing_function, expression) — NOT line number. The site
# already moved once this sprint (:289 → :306); line pinning fires the guard on
# unrelated edits, and a guard that cries wolf gets deleted (R-26). Exact-match
# stays on the EXPRESSION (R-25): a reshaped comparison no longer matches and
# the guard fails; a shifted line does not.
EXEMPTIONS: dict = {
    ("grove/memory/detector.py", "_pending_supersession_target_ids",
     'rec.get("status") not in _BLOCKING_STATUSES'): (
        "R-23 — _pending_supersession_target_ids is a LIVENESS check "
        "('is a supersede in flight for this target', Fix 4 conflict "
        "avoidance), NOT disposition suppression. pending-only is correct "
        "here. The disposition gate for supersede is the SEPARATE sibling "
        "(disposed_target_ids on the same target_id), unioned by the caller "
        "in detect_and_stage."
    ),
}


@dataclass(frozen=True)
class Finding:
    path: str            # repo-relative
    line: int            # informational only — NOT part of identity (R-26)
    function: str        # enclosing function — exemptions key on this, not line
    expr: str            # source text of the offending comparison
    statuses: Tuple[str, ...]  # the status literals it consults

    def key(self) -> Tuple[str, str, str]:
        return (self.path, self.function, self.expr)


def _is_str_const(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _literal_str_collection(node: ast.AST) -> Optional[Set[str]]:
    """The set of string constants in a collection literal, or None.

    Handles ``{"a", "b"}`` / ``["a"]`` / ``("a",)`` and the ``frozenset(...)``
    / ``set(...)`` wrappers around any of those. Returns None for a collection
    that is not a pure set of string constants (so a computed collection never
    masquerades as a known status set)."""
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        out: Set[str] = set()
        for e in node.elts:
            s = _is_str_const(e)
            if s is None:
                return None
            out.add(s)
        return out
    if isinstance(node, ast.Call):
        func = node.func
        name = (func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute) else None)
        if name in {"frozenset", "set"} and len(node.args) == 1:
            return _literal_str_collection(node.args[0])
        if name in {"frozenset", "set"} and not node.args:
            return set()
    return None


def _collect_module_status_collections(tree: ast.Module) -> Dict[str, Set[str]]:
    """Module-level names bound to a literal set of strings — so ``status in
    _BLOCKING_STATUSES`` resolves to the frozenset's members. Only module-level
    bindings are followed (the three guards define theirs at module scope)."""
    out: Dict[str, Set[str]] = {}
    for stmt in tree.body:
        targets: List[ast.AST] = []
        value: Optional[ast.AST] = None
        if isinstance(stmt, ast.Assign):
            targets, value = stmt.targets, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets, value = [stmt.target], stmt.value
        if value is None:
            continue
        members = _literal_str_collection(value)
        if members is None:
            continue
        for t in targets:
            if isinstance(t, ast.Name):
                out[t.id] = members
    return out


def _is_status_access(node: ast.AST) -> bool:
    """A read of a record's ``status`` field: X.get("status") / X["status"]
    / X.status. Field-name based, so it survives whatever the record variable
    is called (the parametrized-read failure mode from AMEND-1)."""
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get" and node.args
            and _is_str_const(node.args[0]) == "status"):
        return True
    if isinstance(node, ast.Subscript) and _is_str_const(node.slice) == "status":
        return True
    if isinstance(node, ast.Attribute) and node.attr == "status":
        return True
    return False


def _referenced_statuses(
    node: ast.AST, collections: Dict[str, Set[str]]
) -> Set[str]:
    """Status string literals an operand compares against: a string constant,
    a module-level status collection name, or an inline collection literal."""
    s = _is_str_const(node)
    if s is not None:
        return {s}
    if isinstance(node, ast.Name) and node.id in collections:
        return set(collections[node.id])
    lit = _literal_str_collection(node)
    if lit is not None:
        return lit
    return set()


def _emits_memory_proposal(tree: ast.Module) -> bool:
    """ENROLLMENT fingerprint: the module constructs a memory-proposal record —
    a dict literal with ``"status": "pending"`` AND a ``"proposal"`` key. This
    is the emission shape shared by every memory detector; routing producers
    use the RoutingProposal dataclass, not this dict, so they do not enroll."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        has_pending_status = False
        has_proposal = False
        for k, v in zip(node.keys, node.values):
            key = _is_str_const(k) if k is not None else None
            if key == "status" and _is_str_const(v) == "pending":
                has_pending_status = True
            if key == "proposal":
                has_proposal = True
        if has_pending_status and has_proposal:
            return True
    return False


def _src(node: ast.AST, source: str) -> str:
    try:
        seg = ast.get_source_segment(source, node)
        if seg:
            return " ".join(seg.split())[:160]
    except Exception:
        pass
    return f"<{type(node).__name__}>"


def _iter_compares(tree: ast.AST):
    """Yield (compare_node, enclosing_function_name) for every ast.Compare,
    tracking the nearest enclosing def (R-26 — findings are identified by the
    function they live in, not by line)."""
    def walk(node, func):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield from walk(child, child.name)
            else:
                if isinstance(child, ast.Compare):
                    yield child, func
                yield from walk(child, func)
    yield from walk(tree, "<module>")


def scan_module(path: str, source: str) -> Tuple[bool, List[Finding]]:
    """(is_producer, findings). Findings are only computed for enrolled
    producers: a status comparison whose consulted statuses lie wholly within
    the non-terminal set (it never consults a terminal disposition)."""
    tree = ast.parse(source)
    if not _emits_memory_proposal(tree):
        return False, []

    collections = _collect_module_status_collections(tree)
    findings: List[Finding] = []
    seen: Set[Tuple[str, str, str]] = set()

    for node, func in _iter_compares(tree):
        operands = [node.left] + list(node.comparators)
        if not any(_is_status_access(o) for o in operands):
            continue
        # statuses consulted = literals from the NON status-access operands.
        consulted: Set[str] = set()
        for o in operands:
            if _is_status_access(o):
                continue
            consulted |= _referenced_statuses(o, collections)
        if not consulted:
            continue  # compares against a variable — cannot classify, skip.
        non_terminal = consulted & NON_TERMINAL_STATUSES
        terminal = consulted & TERMINAL_STATUSES
        # Violation: consults liveness, never a terminal disposition.
        if non_terminal and not terminal:
            expr = _src(node, source)
            k = (path, func, expr)
            if k in seen:
                continue
            seen.add(k)
            findings.append(Finding(
                path, getattr(node, "lineno", 0), func, expr,
                tuple(sorted(consulted)),
            ))
    return True, findings


def _iter_py_files() -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIR_PARTS]
        for fn in filenames:
            if fn.endswith(".py"):
                yield Path(dirpath) / fn


def scan_repo() -> Tuple[List[str], List[Finding]]:
    """(enrolled_producer_paths, all_findings) across the repo."""
    producers: List[str] = []
    findings: List[Finding] = []
    for p in _iter_py_files():
        rel = str(p.relative_to(REPO_ROOT))
        try:
            is_producer, fs = scan_module(rel, p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        if is_producer:
            producers.append(rel)
            findings += fs
    return producers, findings


# ── positive control (SPEC, mandatory) ────────────────────────────────────
# A producer that guards on pending only — MUST flag.
_CONTROL_PENDING_ONLY = '''
_BLOCKING = frozenset({"processing", "pending"})

class NewDetector:
    def _already(self, sid):
        for rec in self._read():
            if rec.get("status") in _BLOCKING:
                return True
        return False
    def _pending_targets(self):
        return {r["proposal"]["target_id"] for r in self._read()
                if r.get("status") == "pending"}
    def stage(self, proposals, sid):
        self._append({"session_id": sid, "status": "pending",
                      "timestamp": "t", "proposal": proposals[0]})
'''

# A producer that consults a terminal disposition — MUST NOT flag.
_CONTROL_TERMINAL_AWARE = '''
_SUPPRESSING = frozenset({"pending", "processing", "rejected", "dismissed"})

class GoodDetector:
    def _blocked(self, tid):
        for rec in self._read():
            if rec.get("status") in _SUPPRESSING:
                return True
        return False
    def stage(self, proposals, sid):
        self._append({"session_id": sid, "status": "pending",
                      "timestamp": "t", "proposal": proposals[0]})
'''

# A reader that guards on pending only but does NOT emit — MUST NOT enroll.
_CONTROL_NON_PRODUCER = '''
def pending_items(records):
    return [r for r in records if r.get("status") == "pending"]
'''


@pytest.mark.guard
def test_positive_control_flags_pending_only_producer():
    """A pending-only producer guard MUST be caught. If not, the guard is
    broken — not the codebase (SPEC positive control)."""
    is_producer, findings = scan_module("<<pending-only>>", _CONTROL_PENDING_ONLY)
    assert is_producer, "control producer was not enrolled by the fingerprint"
    # Two pending-only guards in the snippet (the _BLOCKING membership and the
    # == "pending" filter); both must flag.
    assert len(findings) == 2, (
        f"expected 2 pending-only violations in the control, got {len(findings)}: "
        f"{[(f.line, f.expr) for f in findings]}"
    )


@pytest.mark.guard
def test_positive_control_spares_terminal_aware_producer():
    """A producer whose guard consults a terminal disposition MUST NOT flag —
    it already binds. Proves the guard is not merely flagging every status
    read (R-15: terminal-aware is the target state, not a violation)."""
    is_producer, findings = scan_module("<<terminal-aware>>", _CONTROL_TERMINAL_AWARE)
    assert is_producer, "control producer was not enrolled"
    assert not findings, (
        f"a terminal-aware guard must not flag; got {[f.expr for f in findings]}"
    )


@pytest.mark.guard
def test_positive_control_does_not_enroll_non_producer():
    """A pending-only READER that does not emit a proposal MUST NOT enroll —
    the class is producers, not every status reader (spares portal/cli/digest
    filters, R-15)."""
    is_producer, findings = scan_module("<<reader>>", _CONTROL_NON_PRODUCER)
    assert not is_producer, "a non-emitting reader was wrongly enrolled"
    assert not findings


@pytest.mark.guard
def test_enrollment_is_by_fingerprint_not_module_name():
    """Auto-enrollment (R-17): the three known producers enroll via the
    emission fingerprint, and a synthetic new producer enrolls too — without
    this file naming any of them. Membership is 'emits a memory-proposal
    record', nothing else."""
    producers, _ = scan_repo()
    for expected in ("grove/memory/detector.py",
                     "grove/memory/freshness.py",
                     "grove/memory/graduation.py"):
        assert expected in producers, (
            f"{expected} was not auto-enrolled by the emission fingerprint; "
            f"enrolled: {sorted(producers)}"
        )
    # a brand-new producer, never named anywhere, enrolls purely by shape.
    is_producer, _ = scan_module("<<future-detector>>", _CONTROL_PENDING_ONLY)
    assert is_producer


def _is_exempt(f: Finding) -> bool:
    return f.key() in EXEMPTIONS  # (path, function, expr) — line-drift immune (R-26)


# ── THE GUARD (GREEN at P2 — census minus exemptions) ─────────────────────
@pytest.mark.guard
def test_memory_detector_guards_consult_terminal_dispositions():
    """Every memory-proposal producer's status guard must consult a terminal
    disposition, EXCEPT the declared liveness exemptions (R-23/R-25). RED at
    P1 (4 sites); green at P2 after the three widenings, leaving only the
    exempted supersede liveness check. Do not adjust the assertion to a count;
    a green suite that lies is worse than a red one."""
    producers, findings = scan_repo()
    assert producers, "no memory-proposal producers enrolled — fingerprint broke"
    unexempt = [f for f in findings if not _is_exempt(f)]
    lines = [
        f"  {f.path}:{f.line}  consults {list(f.statuses)} (no terminal "
        f"disposition)\n      {f.expr}"
        for f in sorted(unexempt, key=lambda f: (f.path, f.line))
    ]
    assert not unexempt, (
        f"\n{len(unexempt)} memory producer guard(s) consult NON-TERMINAL "
        f"status only — they go blind after the operator disposes of a "
        f"subject, and the next sweep re-emits it. Widen each through the "
        f"shared gate (grove/memory/dispositions.py), keyed on target_id "
        f"(SPEC R-13); or, if intentionally liveness-only, add a reasoned "
        f"exemption (R-25):\n" + "\n".join(lines)
    )


@pytest.mark.guard
def test_exemptions_match_their_recorded_site():
    """R-25/R-26: every exemption is exact-match on (path, enclosing_function,
    expression) and self-disclosing. It must still trip the guard at a site
    with that function AND expression — line-drift immune (a shifted line still
    matches), but a reshaped comparison or a renamed/removed function does NOT,
    forcing the exemption to justify itself in the diff rather than rot."""
    _, findings = scan_repo()
    live = {f.key() for f in findings}
    stale = []
    for key, reason in EXEMPTIONS.items():
        assert isinstance(reason, str) and reason.strip(), \
            f"exemption {key} has no written reason"
        if key not in live:
            stale.append(
                f"{key[0]} :: {key[1]} :: {key[2]!r} no longer trips the guard "
                f"(function renamed, expression reshaped, or fixed)"
            )
    assert not stale, "stale exemption(s) — remove or update:\n  " + "\n  ".join(stale)


if __name__ == "__main__":
    prods, fs = scan_repo()
    print("=" * 78)
    print(f"ENROLLED memory-proposal producers ({len(prods)}):")
    for p in sorted(prods):
        print(f"  {p}")
    print("-" * 78)
    print(f"CENSUS — {len(fs)} pending-only guard(s):")
    for f in sorted(fs, key=lambda f: (f.path, f.line)):
        print(f"  {f.path}:{f.line}  consults {list(f.statuses)}")
        print(f"      {f.expr}")
    print("=" * 78)
