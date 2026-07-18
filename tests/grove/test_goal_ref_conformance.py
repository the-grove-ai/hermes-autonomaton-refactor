"""dock-goal-ref-integrity-v1 M6a — goal-ref conformance guard.

Statically pin the property behind defect D1: **no writer emits a
``GOAL_ALIGNMENT_VALUES`` member into a refs field.** ``goal_alignment``
carries CATEGORY strings (direct/indirect/orthogonal/...); ``dock_goal_refs``
/ ``dock_goal_ref`` hold dock goal IDS. The shipped defect —
``dominant_dock_goal`` collecting category strings that landed in session-page
``dock_goal_refs`` — is structurally excluded three ways:

1. No refs-field keyword value expression contains a category literal or a
   ``goal_alignment`` reference.
2. No function CALLED inside a refs-field keyword value (resolved one level,
   the ``dominant_dock_goal`` shape) references ``goal_alignment`` or a
   category literal in its body.
3. No function that CONTAINS a refs-field keyword write also references
   ``goal_alignment`` anywhere in its body. Deliberately strict: reading
   alignment and writing refs in one function is the defect's precondition,
   so a future legitimate need must consciously amend this guard.

Follows the ledger-eventtype conformance precedent
(test_ledger_eventtype_conformance.py): literal/static extraction over the
grove/ scan surface, plus a scan-sight guard on the known writer sites so an
extractor regression cannot silently un-scan them.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from grove.classify import GOAL_ALIGNMENT_VALUES

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCAN_DIRS = ("grove",)
_REF_KEYWORDS = frozenset({"dock_goal_refs", "dock_goal_ref"})
_CATEGORY_LITERALS = frozenset(GOAL_ALIGNMENT_VALUES)


def _iter_modules() -> Iterator[Tuple[str, ast.Module]]:
    for d in _SCAN_DIRS:
        for py in sorted((_REPO_ROOT / d).rglob("*.py")):
            if "test" in py.name:
                continue
            try:
                tree = ast.parse(py.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            yield str(py.relative_to(_REPO_ROOT)), tree


def _mentions_goal_alignment(node: ast.AST) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and n.attr == "goal_alignment":
            return True
        if isinstance(n, ast.Name) and n.id == "goal_alignment":
            return True
        if isinstance(n, ast.Constant) and n.value == "goal_alignment":
            return True
        if isinstance(n, ast.keyword) and n.arg == "goal_alignment":
            return True
    return False


def _category_literals_in(node: ast.AST) -> List[str]:
    return [
        n.value
        for n in ast.walk(node)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and n.value in _CATEGORY_LITERALS
    ]


def _called_names(node: ast.AST) -> List[str]:
    """Simple-Name callables invoked anywhere within ``node``."""
    return [
        n.func.id
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    ]


def _functions_by_name() -> Dict[str, List[Tuple[str, ast.FunctionDef]]]:
    out: Dict[str, List[Tuple[str, ast.FunctionDef]]] = {}
    for relpath, tree in _iter_modules():
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.setdefault(n.name, []).append((relpath, n))
    return out


def _ref_keyword_sites() -> List[Tuple[str, int, ast.expr, Optional[ast.AST]]]:
    """(relpath, lineno, keyword-value expr, enclosing function|None) for every
    ``dock_goal_refs=`` / ``dock_goal_ref=`` keyword argument in the scan
    surface."""
    sites: List[Tuple[str, int, ast.expr, Optional[ast.AST]]] = []
    for relpath, tree in _iter_modules():
        funcs = [
            n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]

        def enclosing(call: ast.Call) -> Optional[ast.AST]:
            best: Optional[ast.AST] = None
            for f in funcs:
                if (
                    f.lineno <= call.lineno
                    and call.lineno <= (f.end_lineno or f.lineno)
                ):
                    # innermost wins — the latest-starting enclosing def
                    if best is None or f.lineno >= best.lineno:  # type: ignore[union-attr]
                        best = f
            return best

        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            for kw in n.keywords:
                if kw.arg in _REF_KEYWORDS:
                    sites.append((relpath, n.lineno, kw.value, enclosing(n)))
    return sites


def test_no_category_value_reaches_a_refs_field():
    sites = _ref_keyword_sites()
    assert sites, "extraction found no refs-field keyword sites — scan broke"
    funcs = _functions_by_name()
    violations: List[str] = []

    for relpath, lineno, value, _fn in sites:
        # 1. Direct: category literal or goal_alignment reference in the
        #    keyword value expression itself.
        for lit in _category_literals_in(value):
            violations.append(
                f"{relpath}:{lineno} refs keyword value contains category "
                f"literal {lit!r}"
            )
        if _mentions_goal_alignment(value):
            violations.append(
                f"{relpath}:{lineno} refs keyword value references "
                f"goal_alignment"
            )
        # 2. One-level feeder resolution (the dominant_dock_goal shape): any
        #    in-surface function called inside the value expression must not
        #    touch goal_alignment or category literals.
        for name in _called_names(value):
            for def_path, fn in funcs.get(name, []):
                if _mentions_goal_alignment(fn):
                    violations.append(
                        f"{relpath}:{lineno} refs value calls {name}() "
                        f"({def_path}:{fn.lineno}) which references "
                        f"goal_alignment"
                    )
                for lit in _category_literals_in(fn):
                    violations.append(
                        f"{relpath}:{lineno} refs value calls {name}() "
                        f"({def_path}:{fn.lineno}) which contains category "
                        f"literal {lit!r}"
                    )

    assert not violations, (
        "goal_alignment categories must never feed a refs field "
        "(dock-goal-ref-integrity-v1):\n" + "\n".join(f"  {v}" for v in violations)
    )


def test_no_refs_writer_function_reads_goal_alignment():
    # Rule 3 — strict conjunction: a function that writes a refs field must
    # not read goal_alignment at all.
    violations = [
        f"{relpath}:{lineno} inside {getattr(fn, 'name', '?')}()"
        for relpath, lineno, _value, fn in _ref_keyword_sites()
        if fn is not None and _mentions_goal_alignment(fn)
    ]
    assert not violations, (
        "functions writing a refs field must not read goal_alignment:\n"
        + "\n".join(f"  {v}" for v in violations)
    )


def test_scan_surface_covers_the_known_writer_doors():
    # Guard the extractor itself (precedent's second test): the three writer
    # doors this sprint gated — compactor, digest, adapters — plus the
    # pipeline seam must stay visible to the scan.
    paths = {relpath for relpath, _, _, _ in _ref_keyword_sites()}
    for expected in (
        "grove/wiki/session_compactor.py",
        "grove/wiki/pipeline.py",
        "grove/memory/digest.py",
        "grove/wiki/adapters.py",
    ):
        assert expected in paths, f"scan lost sight of writer door {expected}"

    # The historically-defective feeder must stay within one-level resolution
    # reach: build_session_doc still derives refs via dominant_dock_goal.
    feeder_names = {
        name
        for _, _, value, _ in _ref_keyword_sites()
        for name in _called_names(value)
    }
    assert "dominant_dock_goal" in feeder_names, (
        "build_session_doc no longer routes refs through dominant_dock_goal —"
        " re-point this guard at the new derivation seam"
    )
