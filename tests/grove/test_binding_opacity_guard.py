"""binding-opacity-v1 · P1 — THE GUARD (conformance, AST, repo-wide).

Enforces the locked rule (SPEC 3a6780a78eef8146b534f56814b8aa28):

    The model slug is an OPAQUE TOKEN. It may be COMPARED, HASHED, and
    LOGGED. It may never be PARSED. Forbidden: substring (`in`), regex,
    startswith/endswith/split, membership against a literal set of model
    names, any lookup that maps a slug to behavior.

Two taint-style assertions (GATE-B F-4):
  A1 — the slug-bearing identifier may not be REFERENCED in a
       behavior-shaping operation (compare / subscript-key / parse-call /
       membership) outside an explicit PATH ALLOWLIST. Taint is tracked
       from origin through assignment within a scope, so indirection
       (dict lookup, string building, comparison through an intermediate)
       is caught.
  A2 — no configuration mapping keyed by model slug may be read by
       composition (a dict/subscript indexed by a slug-tainted key).

Positive control (SPEC, mandatory): the detector MUST flag the five known
violation SHAPES. If it does not, the guard is broken, not the codebase.
The control runs against an embedded snippet so it survives P3 deletion.

This file is a TEST ONLY. It writes nothing, deletes nothing, proposes no
fix. At P1 the repo-wide guard is expected to be RED: its failure list is
the census.
"""
from __future__ import annotations

import ast
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Set, Tuple

import pytest

# ── repo geometry ────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parents[2]

# Directories never swept: test corpus (references slugs by design), vendored
# envs, caches, VCS. Everything else under the repo IS swept (router included).
EXCLUDE_DIR_PARTS = {
    ".venv", "venv", "__pycache__", ".git", "node_modules", "build", "dist",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "site-packages", "tests",
}

# ── allowlist — APPROVED at P2 (R-H). The slug's two legitimate consumers:
# the provider adapter that constructs the API call, and telemetry.
PROPOSED_ALLOWLIST: Tuple[str, ...] = (
    # provider adapters — construct the outbound API call / normalize the slug
    # for the wire. R-2's named "provider adapter that constructs the API call".
    "agent/transports/",
    "agent/anthropic_adapter.py",
    "agent/bedrock_adapter.py",
    "agent/auxiliary_client.py",
    # (provider, model) -> live client resolution (credentials, base_url).
    "hermes_cli/runtime_provider.py",
    # router->runtime bridge; holds no provider logic (grove/providers.py:16),
    # passes the pair through (Q4 confirmed: zero parse ops).
    "grove/providers.py",
    # telemetry — records what ran.
    "grove/composer_events.py",
    "grove/intent_store.py",
    # P2 R-H additions:
    # wire normalization — strips vendor prefix / repairs the slug for the
    # native provider request (_strip_vendor_prefix, _dots_to_hyphens).
    "hermes_cli/model_normalize.py",
    # credential-pool selection by provider. R-H internal-vs-vendor rule: a
    # pool-prefix (CUSTOM_POOL_PREFIX) is our own namespace marker.
    "agent/credential_pool.py",
)

# ── scoped OUT of this sprint (P2 R-C) — a DIFFERENT provider namespace ───
# TTS / vision / STT backends, not the model binding the Cognitive Router
# dispatches to. R-2 governs the model binding. Banked separately as
# capability-binding-opacity-v1 (the anti-pattern generalizes; lands before
# open-source release). Excluded by path, NOT silently.
SCOPED_OUT: Tuple[str, ...] = (
    "tools/tts_tool.py",        # capability-binding-opacity-v1
    "tools/vision_tools.py",    # capability-binding-opacity-v1
    "tools/transcription_tools.py",  # capability-binding-opacity-v1
)

# ── what makes a value slug/provider tainted ─────────────────────────────
# Source keys read off a dict/ctx/config (X["model"], X.get("provider")).
SLUG_SOURCE_KEYS = {
    "model", "provider", "model_name", "model_id", "model_slug", "slug",
}
# Attribute reads that surface the slug (self.model, cfg.model, spec.provider).
SLUG_ATTR_NAMES = {
    "model", "provider", "model_slug", "model_name", "model_id",
}
# Parameter / local names that carry the slug directly.
SLUG_NAMES = {
    "model", "provider", "model_lower", "model_short", "model_name",
    "model_id", "model_slug", "slug", "model_used", "operator_model",
    "current_model", "requested_model",
}

# Vendor tokens — used only to recognise a literal COLLECTION of model names
# (e.g. TOOL_USE_ENFORCEMENT_MODELS) and vendor string literals in compares.
VENDOR_TOKENS = {
    "gpt", "codex", "gemini", "gemma", "grok", "glm", "claude", "anthropic",
    "openai", "qwen", "alibaba", "deepseek", "llama", "mistral", "ollama",
    "moonshot", "kimi", "sonnet", "opus", "haiku", "cohere", "bedrock",
    "mixtral", "phi", "xai", "x-ai", "z-ai", "yi-", "command-r",
}

PARSE_METHODS = {
    "startswith", "endswith", "split", "rsplit", "partition", "rpartition",
}
RE_FUNCS = {"match", "search", "findall", "fullmatch", "sub", "compile", "split"}


@dataclass(frozen=True)
class Finding:
    path: str          # repo-relative
    line: int
    col: int
    assertion: str     # "A1" | "A2"
    kind: str          # short shape label
    expr: str          # source text of the offending node
    in_allowlist: bool

    def key(self) -> Tuple[str, int, int, str]:
        return (self.path, self.line, self.col, self.kind)


def _is_str_const(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


# A vendor token counts as a MODEL-NAME token only at a slug boundary
# (binding-opacity-v1 P3, R-B): exact match, or the token followed by one of
# ``/ - .`` or a digit, AND preceded by start-of-string or a non-alphanumeric
# char. This clears the api_mode-enum class (``codex_responses`` — ``_``-joined),
# dylib paths (``libopus`` — mid-word embed), and env-var lists
# (``ANTHROPIC_API_KEY`` — ``_``-joined) without a name denylist (which R-B
# rejected: a slug could be laundered through a denylisted variable name).
_SEP_AFTER = set("/-.")


def _looks_vendor(s: str) -> bool:
    low = s.lower()
    if low in VENDOR_TOKENS:  # exact
        return True
    for tok in VENDOR_TOKENS:
        start = 0
        while True:
            i = low.find(tok, start)
            if i < 0:
                break
            start = i + 1
            before_ok = (i == 0) or (not low[i - 1].isalnum())
            j = i + len(tok)
            after = low[j] if j < len(low) else ""
            after_ok = (after in _SEP_AFTER) or after.isdigit()
            if before_ok and after_ok:
                return True
    return False


# ── taint precision: a MAPPING is not the slug string (binding-opacity P3, R-B) ─
# The false positives (`"base_url" in model_cfg`, `"localhost" in base_url`,
# `"model" in file_config["model"]`) are membership against a config OBJECT, not
# the slug string. The discriminator is the RHS: an expression used as a mapping
# — string-key subscripted, `.get()`/mapping-method receiver — is a dict. We fix
# the TAINT ORIGIN (per operator ruling), not the flag rule: such expressions do
# not carry slug taint, so `"flash" in model_lower` still flags (model_lower is
# never used as a mapping) while `"base_url" in model_cfg` does not.
_MAPPING_METHODS = {"get", "items", "keys", "values", "pop", "setdefault", "update"}


def _canon(node: ast.AST) -> Optional[str]:
    try:
        return ast.unparse(node)
    except Exception:
        return None


def _collect_dict_exprs(nodes: List[ast.AST]) -> Set[str]:
    """Canonical form of every expression USED AS A MAPPING in this scope:
    subscripted with a STRING key, or the receiver of a mapping method. Such an
    expression is a dict — not the slug string."""
    out: Set[str] = set()
    for n in nodes:
        if isinstance(n, ast.Subscript) and _is_str_const(n.slice) is not None:
            c = _canon(n.value)
            if c:
                out.add(c)
        elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr in _MAPPING_METHODS):
            c = _canon(n.func.value)
            if c:
                out.add(c)
    return out


def _is_dict(node: ast.AST, dict_exprs: Set[str]) -> bool:
    c = _canon(node)
    return c is not None and c in dict_exprs


def _is_source(node: ast.AST, dict_exprs: Set[str]) -> bool:
    """A node that directly surfaces the slug STRING from ctx/config/attribute.
    An expression used as a mapping in this scope is a dict, never the slug."""
    if _is_dict(node, dict_exprs):
        return False
    # X["model"] / X['provider']
    if isinstance(node, ast.Subscript):
        k = _is_str_const(node.slice)
        if k in SLUG_SOURCE_KEYS:
            return True
    # X.get("model") / X.get("provider", ...)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "get" and node.args:
            k = _is_str_const(node.args[0])
            if k in SLUG_SOURCE_KEYS:
                return True
    # self.model / cfg.provider / spec.model_slug
    if isinstance(node, ast.Attribute) and node.attr in SLUG_ATTR_NAMES:
        return True
    return False


def _contains_taint(node: ast.AST, tainted: Set[str], dict_exprs: Set[str]) -> bool:
    """True if the expression references the slug STRING anywhere (for SINKS).
    A dict-typed name/expression is excluded — it is not the slug."""
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and n.id in tainted and n.id not in dict_exprs:
            return True
        if _is_source(n, dict_exprs):
            return True
    return False


def _derives_taint(node: ast.AST, tainted: Set[str], dict_exprs: Set[str]) -> bool:
    """Stricter test used for PROPAGATION (RHS of an assignment)."""
    if _is_source(node, dict_exprs):
        return True
    if isinstance(node, ast.Name):
        return node.id in tainted and node.id not in dict_exprs
    # transform chain on a tainted value: (...).lower() / .strip() / .format()
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute) and _derives_taint(node.func.value, tainted, dict_exprs):
            return True
    if isinstance(node, (ast.BoolOp,)):
        return any(_derives_taint(v, tainted, dict_exprs) for v in node.values)
    if isinstance(node, ast.IfExp):
        return (_derives_taint(node.body, tainted, dict_exprs)
                or _derives_taint(node.orelse, tainted, dict_exprs))
    if isinstance(node, ast.BinOp):  # string building: slug + "..."
        return (_derives_taint(node.left, tainted, dict_exprs)
                or _derives_taint(node.right, tainted, dict_exprs))
    if isinstance(node, (ast.Subscript, ast.Attribute)):
        return _is_source(node, dict_exprs)
    return False


def _target_names(target: ast.AST) -> List[str]:
    out: List[str] = []
    if isinstance(target, ast.Name):
        out.append(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for e in target.elts:
            out.extend(_target_names(e))
    return out


def _seed_tainted_for_scope(scope: ast.AST) -> Set[str]:
    seed: Set[str] = set()
    args = getattr(scope, "args", None)
    if args is not None:
        for a in list(args.args) + list(args.posonlyargs) + list(args.kwonlyargs):
            if a.arg in SLUG_NAMES:
                seed.add(a.arg)
    return seed


def _scope_nodes(body: Iterable[ast.AST]) -> List[ast.AST]:
    """All nodes reachable from *body* WITHOUT crossing a nested function
    boundary (FunctionDef/AsyncFunctionDef/Lambda). Comprehensions are NOT
    boundaries — the enclosing scope's names are visible inside them, so a
    genexp like ``p in model_lower for p in MODELS`` is still inspected.
    Prevents file-wide taint pollution across unrelated functions."""
    _BOUND = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
    out: List[ast.AST] = []

    def _signature_only(fn: ast.AST) -> None:
        # a nested def: its name/decorators/defaults are in THIS scope; its body
        # is a separate scope and must not be descended into (taint isolation).
        out.append(fn)
        for d in getattr(fn, "decorator_list", []):
            _descend(d)
        args = getattr(fn, "args", None)
        if args is not None:
            for df in list(args.defaults) + list(args.kw_defaults):
                if df is not None:
                    _descend(df)

    def _descend(node: ast.AST) -> None:
        out.append(node)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _BOUND):
                _signature_only(child)
            else:
                _descend(child)

    for stmt in body:
        if isinstance(stmt, _BOUND):
            _signature_only(stmt)
        else:
            _descend(stmt)
    return out


def _collect_tainted(scope_body: Iterable[ast.stmt], seed: Set[str],
                     dict_exprs: Set[str]) -> Set[str]:
    tainted = set(n for n in seed if n not in dict_exprs)
    nodes = _scope_nodes(scope_body)
    for _ in range(6):  # fixpoint on chained assignments
        grew = False
        for node in nodes:
            if isinstance(node, ast.Assign):
                if _derives_taint(node.value, tainted, dict_exprs):
                    for t in node.targets:
                        for nm in _target_names(t):
                            if nm not in tainted and nm not in dict_exprs:
                                tainted.add(nm); grew = True
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                if node.value is not None and _derives_taint(node.value, tainted, dict_exprs):
                    for nm in _target_names(node.target):
                        if nm not in tainted and nm not in dict_exprs:
                            tainted.add(nm); grew = True
        if not grew:
            break
    return tainted


def _src(node: ast.AST, source: str) -> str:
    try:
        seg = ast.get_source_segment(source, node)
        if seg:
            return " ".join(seg.split())[:160]
    except Exception:
        pass
    return f"<{type(node).__name__}>"


def _literal_model_collection(node: ast.AST) -> bool:
    """A tuple/list/set literal whose elements are mostly vendor names."""
    if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return False
    strs = [_is_str_const(e) for e in node.elts]
    strs = [s for s in strs if s is not None]
    if len(strs) < 2:
        return False
    hits = sum(1 for s in strs if _looks_vendor(s))
    return hits >= max(2, (len(strs) + 1) // 2)


def _detect_in_scope(
    scope_body: Iterable[ast.stmt], tainted: Set[str], path: str, source: str,
    allow: bool, is_composition: bool, dict_exprs: Set[str],
) -> List[Finding]:
    """Two-rule detector.

    Rule 1 (repo-wide, outside the allowlist): the slug may not be PARSED —
    substring `in`, membership against a literal model-name collection,
    startswith/endswith/split, or regex. R-2 forbids these everywhere but the
    adapter.

    Rule 2 (COMPOSITION modules only): the slug may not be REFERENCED AT ALL —
    equality (== / !=), dict-key subscript, or the Rule-1 parses. Equality and
    dict-keys are permitted elsewhere by R-2/F-3, but in composition they map
    the slug to prompt content and are the 951-class defect.
    """
    findings: List[Finding] = []

    def add(node, assertion, kind):
        findings.append(Finding(
            path, getattr(node, "lineno", 0), getattr(node, "col_offset", 0),
            assertion, kind, _src(node, source), allow,
        ))

    for node in _scope_nodes(scope_body):
        if isinstance(node, ast.Compare):
            operands = [node.left] + list(node.comparators)
            for op, right in zip(node.ops, node.comparators):
                if isinstance(op, (ast.In, ast.NotIn)):
                    # substring: the container (RHS) is a slug-tainted string
                    substring = _contains_taint(right, tainted, dict_exprs)
                    # membership against a literal model-name collection
                    coll_is_models = any(_literal_model_collection(o) for o in operands)
                    if substring or coll_is_models:
                        add(node, "A1", "parse: substring/model-set membership on slug")
                elif isinstance(op, (ast.Eq, ast.NotEq, ast.Is, ast.IsNot)):
                    # equality is permitted by R-2 EXCEPT inside composition,
                    # where it maps slug -> prompt content (951-class).
                    if is_composition and any(_contains_taint(o, tainted, dict_exprs) for o in operands):
                        add(node, "A2", "composition branches on slug (equality)")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            if attr in PARSE_METHODS and _contains_taint(node.func.value, tainted, dict_exprs):
                add(node, "A1", f"parse: .{attr}() on slug")
            if (isinstance(node.func.value, ast.Name) and node.func.value.id == "re"
                    and attr in RE_FUNCS
                    and any(_contains_taint(a, tainted, dict_exprs) for a in node.args)):
                add(node, "A1", f"parse: regex re.{attr}() on slug")
        elif isinstance(node, ast.Subscript):
            # dict-key by slug is permitted by R-2 EXCEPT in composition (A2).
            if is_composition and _contains_taint(node.slice, tainted, dict_exprs) and not _is_source(node, dict_exprs):
                add(node, "A2", "composition reads mapping keyed by slug")
    return findings


# Composition modules — the slug may not be referenced here AT ALL (Rule 2).
COMPOSITION_PREFIXES = ("grove/prompt/", "agent/prompt_builder.py")


def _is_composition(rel: str) -> bool:
    return any(rel == p or rel.startswith(p) for p in COMPOSITION_PREFIXES)


def scan_module(path: str, source: str, force_composition: bool = False) -> List[Finding]:
    tree = ast.parse(source)
    allow = _path_allowlisted(path)
    comp = force_composition or _is_composition(path)
    findings: List[Finding] = []

    # module scope
    module_nodes = _scope_nodes(tree.body)
    module_dicts = _collect_dict_exprs(module_nodes)
    module_tainted = _collect_tainted(tree.body, set(), module_dicts)
    findings += _detect_in_scope(tree.body, module_tainted, path, source, allow, comp, module_dicts)

    # literal model-name collections defined anywhere (A1 root artifact)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _literal_model_collection(node.value):
            findings.append(Finding(
                path, node.lineno, node.col_offset, "A1",
                "literal model-name collection", _src(node.value, source), allow,
            ))

    # each function scope — dict-typing is scope-local (a name that is a dict in
    # one function may be a slug string in another).
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            body = node.body if not isinstance(node, ast.Lambda) else [ast.Expr(node.body)]
            local_dicts = _collect_dict_exprs(_scope_nodes(body)) | module_dicts
            seed = _seed_tainted_for_scope(node) | module_tainted
            local = _collect_tainted(body, seed, local_dicts)
            findings += _detect_in_scope(body, local, path, source, allow, comp, local_dicts)

    # dedupe by (path, line, col, kind)
    seen: Set[Tuple] = set()
    uniq: List[Finding] = []
    for f in findings:
        if f.key() not in seen:
            seen.add(f.key()); uniq.append(f)
    return uniq


# ── line-scoped exemptions (binding-opacity-v1 P3, R-B) ──────────────────
# Each MUST carry a written reason; the guard asserts the reason exists AND
# that the exemption is still LIVE (matches a real detection), so a fixed or
# moved line surfaces as a stale exemption in the diff rather than rotting.
# HARD CAP: 10. Past ten, the heuristic is wrong, not the code (R-B).
# Both entries are the R-H internal-vs-vendor rule: a string OUR system mints
# is not a vendor slug. Parsing our own namespace marker is not slug inference.
EXEMPTIONS: dict = {
    ("hermes_cli/kanban_db.py", 379):
        "kanban card slug — our own namespace (R-H internal-vs-vendor), not a "
        "vendor model binding; .split('-') parses a card id, not a model slug.",
    ("scripts/sample_and_compress.py", 30):
        "HuggingFace dataset identifiers (NousResearch/...), not model bindings "
        "the Cognitive Router dispatches to; the '-glm-kimi-' tokens are dataset "
        "names, not a bound model.",
}


def _path_allowlisted(rel: str) -> bool:
    return any(rel == a or rel.startswith(a) for a in PROPOSED_ALLOWLIST)


def _is_exempt(f: "Finding") -> bool:
    return (f.path, f.line) in EXEMPTIONS


def _is_scoped_out(rel: str) -> bool:
    return any(rel == s or rel.startswith(s) for s in SCOPED_OUT)


def _iter_py_files() -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIR_PARTS]
        for fn in filenames:
            if fn.endswith(".py"):
                yield Path(dirpath) / fn


def scan_repo() -> List[Finding]:
    findings: List[Finding] = []
    for p in _iter_py_files():
        rel = str(p.relative_to(REPO_ROOT))
        try:
            findings += scan_module(rel, p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue  # unparseable/binary — skip, do not abort the sweep
    return findings


# ── the five KNOWN shapes (positive control) ─────────────────────────────
_POSITIVE_CONTROL_SNIPPET = '''
TOOL_USE_ENFORCEMENT_MODELS = ("gpt", "codex", "gemini", "gemma", "grok", "glm")

def _tool_use_enforcement_provider(ctx):
    model_lower = (ctx.get("model") or "").lower()
    inject = any(p in model_lower for p in TOOL_USE_ENFORCEMENT_MODELS)   # :739
    return inject

def _model_operational_guidance_provider(ctx):
    model_lower = (ctx.get("model") or "").lower()
    inject = any(p in model_lower for p in TOOL_USE_ENFORCEMENT_MODELS)   # :778
    if "gemini" in model_lower or "gemma" in model_lower:                 # :781
        return "google"
    if "gpt" in model_lower or "codex" in model_lower:                    # :786
        return "openai"

def _alibaba_model_override_provider(ctx):
    if ctx.get("provider") != "alibaba":                                  # :951
        return None
'''


def _control_findings() -> List[Finding]:
    # the control mimics composition, where equality on the slug is forbidden.
    return scan_module("<<positive-control>>", _POSITIVE_CONTROL_SNIPPET,
                       force_composition=True)


# ── tests ────────────────────────────────────────────────────────────────
@pytest.mark.guard
def test_positive_control_flags_all_five_known_shapes():
    """The detector must catch every known violation shape. If this fails,
    the guard is broken — not the codebase (SPEC positive control)."""
    findings = _control_findings()

    # 1. substring over TOOL_USE_ENFORCEMENT_MODELS + the two inline sites:
    #    the two `any(p in model_lower ...)` and gemini/gemma + gpt/codex.
    membership = [f for f in findings
                  if f.kind == "parse: substring/model-set membership on slug"]
    assert len(membership) >= 4, (
        "expected >=4 substring/membership flags (two any(... in model_lower) "
        f"sites + inline gemini/gpt sites); got {len(membership)}: "
        f"{[(f.line, f.expr) for f in membership]}"
    )
    # 2. the literal model-name collection itself (TOOL_USE_ENFORCEMENT_MODELS).
    assert any(f.kind == "literal model-name collection" for f in findings), \
        "TOOL_USE_ENFORCEMENT_MODELS literal collection not flagged"
    # 3. provider != 'alibaba' — equality on the slug inside composition (A2).
    assert any(f.kind == "composition branches on slug (equality)" for f in findings), \
        "provider != 'alibaba' composition branch not flagged"


@pytest.mark.guard
def test_positive_control_maps_to_live_composer_lines():
    """P1 validation: the five live violations at composer.py:739/778/781/786
    and :951 must be present in the repo scan (proves detection against real
    source, not just the synthetic control). Naturally lapses after P3 removes
    them — guarded so it does not become a false failure post-deletion."""
    composer = REPO_ROOT / "grove/prompt/composer.py"
    if not composer.exists():
        pytest.skip("composer.py absent")
    src = composer.read_text(encoding="utf-8")
    findings = scan_module("grove/prompt/composer.py", src)
    flagged_lines = {f.line for f in findings}
    known = {739, 778, 781, 786, 951}
    still_present = {ln for ln in known if _line_has_slug_predicate(src, ln)}
    missing = {ln for ln in still_present if ln not in flagged_lines}
    assert not missing, (
        f"guard MISSED known live violations at composer.py lines {sorted(missing)} "
        f"(still present in source). Flagged lines nearby: "
        f"{sorted(l for l in flagged_lines if 730 <= l <= 960)}"
    )


def _line_has_slug_predicate(src: str, ln: int) -> bool:
    lines = src.splitlines()
    if ln < 1 or ln > len(lines):
        return False
    text = lines[ln - 1]
    return ("model_lower" in text) or ("alibaba" in text) or (
        "TOOL_USE_ENFORCEMENT_MODELS" in text)


# ── assertion 3 — binding capability fields are unreadable by composition ─
# GATE-B F-2 (accepted): a declared binding field describes the model's physics
# and is consumed by the adapter/runtime — NEVER by prompt composition. If a
# field could only ever be spent on prompt text, it is psychology, not physics,
# and does not belong on the binding at all. This makes that STRUCTURAL: the
# guard fails if any composition module reads a binding capability field.
BINDING_CAPABILITY_FIELDS = frozenset({
    "context_window", "reasoning_support", "native_tool_schema", "api_mode",
    "system_message_role", "prompt_cache_style",
})


def _binding_field_reads(source: str) -> List[Tuple[int, str]]:
    """Reads of a binding capability field: X.get("field"), X["field"], .field."""
    hits: List[Tuple[int, str]] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return hits
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "get" and n.args):
            k = _is_str_const(n.args[0])
            if k in BINDING_CAPABILITY_FIELDS:
                hits.append((n.lineno, k))
        elif isinstance(n, ast.Subscript):
            k = _is_str_const(n.slice)
            if k in BINDING_CAPABILITY_FIELDS:
                hits.append((n.lineno, k))
        elif isinstance(n, ast.Attribute) and n.attr in BINDING_CAPABILITY_FIELDS:
            hits.append((n.lineno, n.attr))
    return hits


@pytest.mark.guard
def test_assertion3_positive_control_catches_composition_binding_read():
    """A synthetic composition provider reading context_window MUST be caught,
    or assertion 3 is broken (SPEC P4a deliverable 4)."""
    snippet = (
        "def _bad_provider(ctx):\n"
        "    if ctx.get('context_window', 0) > 100000:\n"
        "        return SectionResult(label='x', text='y')\n"
        "    schema = ctx['native_tool_schema']\n"
    )
    hits = {f for _, f in _binding_field_reads(snippet)}
    assert "context_window" in hits and "native_tool_schema" in hits, (
        f"assertion 3 failed to catch synthetic binding-field reads; got {hits}"
    )


@pytest.mark.guard
def test_composition_cannot_read_binding_capability_fields():
    """ASSERTION 3. No composition module may read a binding capability field.
    Green today (the fields do not exist yet); a canary that fails the instant
    a physics fact is spent on prompt text."""
    violations: List[str] = []
    for p in _iter_py_files():
        rel = str(p.relative_to(REPO_ROOT))
        if not _is_composition(rel):
            continue
        try:
            for ln, f in _binding_field_reads(p.read_text(encoding="utf-8")):
                violations.append(f"{rel}:{ln} reads binding capability field {f!r}")
        except (UnicodeDecodeError, OSError):
            continue
    assert not violations, (
        "composition reads a binding capability field (physics belongs on the "
        "binding, read by the adapter — never composition):\n  " + "\n  ".join(violations)
    )


@pytest.mark.guard
def test_exemptions_are_reasoned_live_and_under_cap():
    """Every exemption carries a written reason, is still a real detection
    (not stale), and the set stays under the hard cap of ten (R-B)."""
    assert len(EXEMPTIONS) <= 10, (
        f"{len(EXEMPTIONS)} exemptions exceeds the cap of 10 — the heuristic is "
        f"wrong, not the code (R-B). Halt and surface rather than exempt to green."
    )
    for key, reason in EXEMPTIONS.items():
        assert isinstance(reason, str) and reason.strip(), \
            f"exemption {key} has no written reason"
    raw = {(f.path, f.line) for f in scan_repo()}
    stale = [k for k in EXEMPTIONS if k not in raw]
    assert not stale, (
        f"stale exemption(s) {stale}: the line no longer trips the guard — "
        f"remove the exemption (it must justify itself in the diff)."
    )


@pytest.mark.guard
def test_slug_not_parsed_outside_allowlist():
    """THE GUARD. Slug must not be referenced in a behavior-shaping op outside
    the adapter+telemetry allowlist. RED at P1 by design: the failure list is
    the census. Goes green when P3 deletes the violations."""
    findings = scan_repo()
    census = [f for f in findings
              if not f.in_allowlist and not _is_exempt(f) and not _is_scoped_out(f.path)]
    if census:
        lines = ["\nBINDING-OPACITY CENSUS — %d violation(s) outside allowlist:" % len(census)]
        for f in sorted(census, key=lambda x: (x.path, x.line)):
            lines.append(f"  {f.path}:{f.line}  [{f.assertion} {f.kind}]  {f.expr}")
        pytest.fail("\n".join(lines))


if __name__ == "__main__":
    all_findings = scan_repo()
    ctrl = _control_findings()
    print("=" * 78)
    print("POSITIVE CONTROL (synthetic snippet):")
    for f in sorted(ctrl, key=lambda x: x.line):
        print(f"  L{f.line:<3} [{f.assertion} {f.kind}] {f.expr}")
    print("=" * 78)
    census = [f for f in all_findings if not f.in_allowlist]
    boundary = [f for f in all_findings if f.in_allowlist]
    print(f"CENSUS — {len(census)} violation(s) OUTSIDE proposed allowlist:")
    for f in sorted(census, key=lambda x: (x.path, x.line)):
        print(f"  {f.path}:{f.line}  [{f.assertion} {f.kind}]  {f.expr}")
    print("-" * 78)
    print(f"PROPOSED-ALLOWLIST consumers — {len(boundary)} reference(s) (operator rules at P2):")
    for f in sorted(boundary, key=lambda x: (x.path, x.line)):
        print(f"  {f.path}:{f.line}  [{f.assertion} {f.kind}]  {f.expr}")
