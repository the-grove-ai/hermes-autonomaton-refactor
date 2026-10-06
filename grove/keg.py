"""Keg semantics for the T0 pattern cache (GRV-004 v1.1, Section IV).

A keg here is a compiled T0 cache entry that also satisfies GRV-004's keg
definition: identity, per-keg scope, a governance classification
(``authority_level``) and, for an executable keg, a declared interface —
``inputs``, ``outputs`` and ``conditions``. The cache entry
(``pattern_cache.db``) stays the single store; this module holds the shared
rules that compile time (Kaizen's proposal builder), approval (the flywheel
handlers) and execution time (the T0 path) must agree on.

Two things are deliberately kept apart:

* ``authority_level`` — what the keg MAY DO once granted (``green``: it runs
  with no human checkpoint before execution).
* the grant itself — the operator signing the proposal. That is operator-only
  and lives on the proposal's disposition, never on the keg's own fields.

Pipeline stage: Compilation (a keg is how a recognized request becomes a
declared action with no model call). The condition grammar is the limited one
GRV-004's worked example uses — ``==``, ``IN``, ``NOT IN``, ``AND``, ``OR`` —
plus one addition, ``CONTAINS`` on a declared text input (a case-insensitive
substring test), because equality alone cannot separate two cases that share
every categorical field. All of it is evaluated deterministically. GRV-004 leaves evaluation semantics to the
implementation; ours are: first matching condition wins, ``AND`` binds tighter
than ``OR``, no parentheses, and an input with no value never matches (the
bias is toward "not covered", which sends the work back to the interpreter).
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

KEG_PROTOCOL = "GRV-004"
KEG_PROTOCOL_VERSION = "1.1"

SCOPES = ("public", "member", "reserved")
AUTHORITY_LEVELS = ("green", "yellow", "red")
DATA_TYPES = ("string", "numeric", "boolean", "filepath")

# ── the improvement loop, by role ─────────────────────────────────────
#
# Standard work → Jidoka → Andon → Kaizen → expert signs (or sends feedback)
# → new standard work. Every event a keg produces is named for the role that
# produced it, so a trace reads as the loop's separate steps.
LOOP_JIDOKA_FLAG = "jidoka_flag"             # the watcher flags; it never fixes
LOOP_ANDON_EVENT = "andon_event"             # the cord: issue details + provenance
LOOP_KAIZEN_PROPOSAL = "kaizen_proposal"     # the butler proposes; it never commits
LOOP_SIGNED = "signed"                       # the expert signs
LOOP_FEEDBACK = "feedback"                   # ... or sends feedback back to Kaizen
LOOP_NEW_STANDARD_WORK = "new_standard_work"

# What Jidoka can flag. A miss stops the line; a tier-down pattern stops nothing.
FLAG_TIER_DOWN_PATTERN = "tier_down_pattern"
FLAG_ANOMALY = "anomaly"
FLAGS = (FLAG_TIER_DOWN_PATTERN, FLAG_ANOMALY)


def keg_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    if not slug:
        raise ValueError(f"keg name {name!r} has no usable characters")
    return slug


# ── condition grammar ─────────────────────────────────────────────────

_TOKEN_RE = re.compile(
    r"""\s*(?:
        (?P<str>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")
      | (?P<num>-?\d+(?:\.\d+)?)
      | (?P<op>==)
      | (?P<punct>[\[\],])
      | (?P<word>[A-Za-z_][A-Za-z0-9_]*)
    )""",
    re.VERBOSE,
)
_KEYWORDS = {"AND", "OR", "IN", "NOT", "CONTAINS"}
_WS_RE = re.compile(r"\s+")


def _tokenize(expr: str) -> List[Tuple[str, Any]]:
    tokens: List[Tuple[str, Any]] = []
    pos = 0
    text = expr.rstrip()
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if m is None or m.end() == pos:
            raise ValueError(f"cannot read condition at position {pos}: {expr!r}")
        pos = m.end()
        if m.group("str") is not None:
            raw = m.group("str")[1:-1]
            tokens.append(("lit", re.sub(r"\\(.)", r"\1", raw)))
        elif m.group("num") is not None:
            tokens.append(("lit", float(m.group("num"))))
        elif m.group("op") is not None:
            tokens.append(("op", "=="))
        elif m.group("punct") is not None:
            tokens.append(("punct", m.group("punct")))
        else:
            word = m.group("word")
            if word in _KEYWORDS:
                tokens.append(("kw", word))
            elif word in ("true", "false"):
                tokens.append(("lit", word == "true"))
            else:
                tokens.append(("ident", word))
    return tokens


def parse_condition(expr: str, inputs: Mapping[str, Any]) -> List[List[Tuple[str, str, Any]]]:
    """Parse ``expr`` into OR-groups of AND-clauses.

    Each clause is ``(input_name, operator, operand)`` with operator one of
    ``==`` / ``IN`` / ``NOT IN`` / ``CONTAINS``. Raises ValueError on anything outside the
    grammar or naming an input the keg does not declare — a keg whose rule
    cannot be read is a compile-time defect, never a runtime surprise.
    """
    if not isinstance(expr, str) or not expr.strip():
        raise ValueError("condition 'if' must be a non-empty string")
    tokens = _tokenize(expr)
    i = 0

    def _peek() -> Tuple[str, Any]:
        return tokens[i] if i < len(tokens) else ("end", None)

    def _take(kind: str, value: Any = None) -> Any:
        nonlocal i
        got_kind, got_value = _peek()
        if got_kind != kind or (value is not None and got_value != value):
            want = value if value is not None else kind
            raise ValueError(f"expected {want!r} in condition {expr!r}")
        i += 1
        return got_value

    def _literal_list() -> List[Any]:
        _take("punct", "[")
        items = [_take("lit")]
        while _peek() == ("punct", ","):
            _take("punct", ",")
            items.append(_take("lit"))
        _take("punct", "]")
        return items

    def _clause() -> Tuple[str, str, Any]:
        name = _take("ident")
        if name not in inputs:
            raise ValueError(
                f"condition {expr!r} reads {name!r}, which is not a declared input"
            )
        kind, value = _peek()
        if kind == "op":
            _take("op")
            return (name, "==", _take("lit"))
        if (kind, value) == ("kw", "IN"):
            _take("kw", "IN")
            return (name, "IN", _literal_list())
        if (kind, value) == ("kw", "NOT"):
            _take("kw", "NOT")
            _take("kw", "IN")
            return (name, "NOT IN", _literal_list())
        if (kind, value) == ("kw", "CONTAINS"):
            _take("kw", "CONTAINS")
            needle = _take("lit")
            if not isinstance(needle, str) or not needle.strip():
                raise ValueError(f"CONTAINS needs a non-empty text value in {expr!r}")
            if (inputs[name] or {}).get("data_type") != "string":
                raise ValueError(
                    f"CONTAINS reads {name!r}, which is not a declared text input"
                )
            return (name, "CONTAINS", needle)
        raise ValueError(
            f"expected ==, IN, NOT IN or CONTAINS after {name!r} in {expr!r}"
        )

    groups: List[List[Tuple[str, str, Any]]] = []
    while True:
        group = [_clause()]
        while _peek() == ("kw", "AND"):
            _take("kw", "AND")
            group.append(_clause())
        groups.append(group)
        if _peek() == ("kw", "OR"):
            _take("kw", "OR")
            continue
        break
    if _peek()[0] != "end":
        raise ValueError(f"unexpected text after condition in {expr!r}")
    return groups


def _norm(value: Any) -> Any:
    """Comparison form. Strings compare case-insensitively with whitespace
    collapsed, so "Acme  Co" and "acme co" are one value; numbers compare as numbers; booleans as booleans."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return _WS_RE.sub(" ", value).strip().casefold()
    return value


def _clause_holds(clause: Tuple[str, str, Any], values: Mapping[str, Any]) -> bool:
    name, op, operand = clause
    if name not in values or values[name] is None:
        return False  # no value → never a match (falls to "not covered")
    actual = _norm(values[name])
    if op == "==":
        return actual == _norm(operand)
    if op == "CONTAINS":
        return isinstance(actual, str) and _norm(operand) in actual
    members = [_norm(x) for x in operand]
    return (actual in members) if op == "IN" else (actual not in members)


def match(spec: Mapping[str, Any], values: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """The first condition that matches ``values``, or None."""
    inputs = spec.get("inputs") or {}
    for cond in spec.get("conditions") or []:
        groups = parse_condition(cond.get("if"), inputs)
        if any(all(_clause_holds(c, values) for c in group) for group in groups):
            return cond
    return None


def evaluate(spec: Mapping[str, Any], values: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The outputs of the first condition that matches ``values``, or None
    when the keg does not answer this input. Deterministic; no model call.

    A keg does not answer an input in two ways, and both send the work back to
    the interpreter: no condition matches, or the first matching condition is
    a DEFER — a rule that says "this case is not mine". A defer is how a keg
    is narrowed after a miss without teaching it a new answer."""
    cond = match(spec, values)
    if cond is None or cond.get("defer"):
        return None
    return dict(cond.get("then") or {})


def defers(spec: Mapping[str, Any], values: Mapping[str, Any]) -> bool:
    """True when the first matching condition explicitly defers."""
    cond = match(spec, values)
    return bool(cond is not None and cond.get("defer"))


# ── spec validation ───────────────────────────────────────────────────


def validate_spec(spec: Any) -> None:
    """Raise ValueError unless ``spec`` is a complete executable keg.

    GRV-004 minimum (identity, scope, governance classification) plus the full
    executable interface. ``reserve`` is required: what the keg does NOT cover
    is declared, not inferred (Invariant III, applied at keg level)."""
    if not isinstance(spec, Mapping):
        raise ValueError(f"keg spec must be a mapping, got {type(spec).__name__}")
    keg_slug(str(spec.get("name") or ""))
    version = spec.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError(f"keg version must be an integer >= 1, got {version!r}")
    if spec.get("protocol") != KEG_PROTOCOL:
        raise ValueError(f"keg protocol must be {KEG_PROTOCOL!r}")
    if spec.get("scope") not in SCOPES:
        raise ValueError(f"keg scope must be one of {SCOPES}, got {spec.get('scope')!r}")
    if spec.get("authority_level") not in AUTHORITY_LEVELS:
        raise ValueError(
            f"keg authority_level must be one of {AUTHORITY_LEVELS}, "
            f"got {spec.get('authority_level')!r}"
        )
    for field in ("reserve", "dock_goal"):
        if not isinstance(spec.get(field), str) or not spec[field].strip():
            raise ValueError(f"keg {field!r} must be a non-empty string")
    # The T0 trigger is declared on the keg itself: the operator request it
    # answers. The Dispatcher's T0 lookup is the generic cache lookup on that
    # request — it knows nothing about any particular keg.
    trigger = spec.get("trigger")
    if (
        not isinstance(trigger, Mapping)
        or not isinstance(trigger.get("request"), str)
        or not trigger["request"].strip()
    ):
        raise ValueError("keg trigger must declare the 'request' it answers")
    inputs, outputs = spec.get("inputs"), spec.get("outputs")
    for label, block in (("inputs", inputs), ("outputs", outputs)):
        if not isinstance(block, Mapping) or not block:
            raise ValueError(f"keg {label} must be a non-empty mapping")
        for key, decl in block.items():
            if not isinstance(decl, Mapping) or decl.get("data_type") not in DATA_TYPES:
                raise ValueError(
                    f"keg {label}.{key} needs a data_type in {DATA_TYPES}"
                )
    conditions = spec.get("conditions")
    if not isinstance(conditions, list) or not conditions:
        raise ValueError("keg conditions must be a non-empty list")
    for cond in conditions:
        if not isinstance(cond, Mapping):
            raise ValueError("each keg condition must be a mapping")
        parse_condition(cond.get("if"), inputs)
        then = cond.get("then")
        if cond.get("defer"):
            if then:
                raise ValueError(
                    f"condition {cond.get('if')!r} both defers and assigns outputs"
                )
            continue
        if not isinstance(then, Mapping) or not then:
            raise ValueError(
                f"condition {cond.get('if')!r} needs 'then' outputs or 'defer: true'"
            )
        unknown = [k for k in then if k not in outputs]
        if unknown:
            raise ValueError(
                f"condition {cond.get('if')!r} assigns undeclared output(s): "
                f"{', '.join(unknown)}"
            )


def rules_digest(spec: Mapping[str, Any], evidence: Any, lineage: Any = None) -> str:
    """Identity digest for one drafted keg: its interface plus the evidence it
    rests on. Identical rules on identical evidence are the identical keg (never
    re-proposed); a revised rule or new evidence is a different draft, and
    so is the same keg earned again in a later run (``lineage``)."""
    body = {
        "inputs": spec.get("inputs"),
        "outputs": spec.get("outputs"),
        "conditions": spec.get("conditions"),
        "trigger": spec.get("trigger"),
        "lineage": lineage,
        "evidence": sorted(str(e) for e in (evidence or ())),
    }
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


# ── reading a keg back off a cache entry ──────────────────────────────


def keg_of(pattern: Any) -> Optional[Dict[str, Any]]:
    """The keg spec carried in a cache entry's compiled invocation, or None
    when the entry is an ordinary cached pattern."""
    raw = getattr(pattern, "compiled_invocation", None)
    if not raw:
        return None
    try:
        inv = json.loads(raw)
    except (TypeError, ValueError):
        return None
    spec = (inv.get("args") or {}).get("keg") if isinstance(inv, dict) else None
    return spec if isinstance(spec, dict) else None


def keg_record(pattern: Any) -> Dict[str, Any]:
    """The keg's bookkeeping block (version lineage, who signed, feedback),
    stored in the entry's existing ``promotion_evidence`` JSON."""
    raw = getattr(pattern, "promotion_evidence", None)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def lifecycle(status: str) -> Dict[str, Any]:
    """Map a cache status onto GRV-004's keg lifecycle (OKF ``status``).

    * proposed → ``draft``; never serves.
    * signed → ``stable``; serves.
    * halted after a miss → still ``stable``, with ``halted`` set: it does not
      serve, and its grant stands until the operator rules on Kaizen's fix.
    * replaced by a later signed version → still ``stable`` history, not
      current.
    * only the operator's revocation makes a keg ``deprecated``.
    """
    from grove.pattern_cache import (
        STATUS_ACTIVE, STATUS_DEMOTED, STATUS_HALTED, STATUS_REJECTED,
        STATUS_SUPERSEDED, STATUS_SUSPENDED,
    )
    table = {
        STATUS_SUSPENDED: ("draft", "proposed"),
        STATUS_REJECTED: ("draft", "feedback sent"),
        STATUS_ACTIVE: ("stable", "serving"),
        STATUS_HALTED: ("stable", "halted"),
        STATUS_SUPERSEDED: ("stable", "replaced"),
        STATUS_DEMOTED: ("deprecated", "revoked"),
    }
    if status not in table:
        raise ValueError(f"unknown cache status {status!r}")
    okf_status, state = table[status]
    return {
        "status": okf_status,
        "state": state,
        "serves": status == STATUS_ACTIVE,
        "halted": status == STATUS_HALTED,
    }
