"""Decision work: a goal's queue of items, each decided once and confirmed.

The generic half of "do the work, record everything, notice what keeps
working". A Dock goal declares a ``decision_work`` block — where the items
queue up, the input and output fields, the reference table, the evidence rule
and whether its turns are isolated. This module reads that declaration and
keeps the goal's decision log. It knows no domain: a new kind of work is a new
goal, a new skill and a small adapter tool that parses that domain's items.

Pipeline stages: Telemetry (the append-only decision log), Compilation (the
declared config is what a turn is compiled against) and Approval (the checks
that refuse a decision the turn was not entitled to make).

Three things a decision record never is:
  * edited — a confirmation or correction is a NEW record that references the
    proposed one;
  * unattributed — every proposal carries the turn, tier and model (or keg)
    that produced it;
  * contaminated — a goal that declares ``isolation: sources_only`` gets its
    answer from its own declared sources only. A turn that read from the
    Cellar, from memory or from a recall tool cannot record a decision.
"""

from __future__ import annotations

import csv
import fcntl
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

ISOLATION_SOURCES_ONLY = "sources_only"
ON_UNCLEAN_OPEN_CLEAN = "open_clean_session"

# Standing-grant write class for a goal's signed session rule. The grant's
# write_class is this prefix plus a digest of the rule, so a signature commits
# to exactly the rule that was on screen when it was signed.
SESSION_RULE_PREFIX = "session_rule:"

# Prompt sections that carry recalled knowledge (composer registration names),
# and the tools that fetch it. An isolated turn composes none of the first and
# may not have called any of the second.
RECALL_SECTIONS = frozenset({
    "cellar_knowledge", "accumulated_domain_memory", "external_memory",
})
RECALL_TOOLS = frozenset({"cellar_search", "session_search", "memory"})

# Session-database key: the goal a session is isolated to. Set on a session's
# FIRST turn and never changed, so isolation is a property of the session.
ISOLATION_META_PREFIX = "goal_isolation:"

KIND_RUN_STARTED = "run_started"
KIND_PROPOSED = "proposed"
KIND_DECIDED = "decided"
KIND_SET_ASIDE = "set_aside"     # an item taken out of the queue for manual handling

DECISION_CONFIRM = "confirm"
DECISION_CORRECT = "correct"
# Decided under a signed keg's own authority, in a batch, with no operator
# checkpoint: "decided by the keg, not reviewed". It is NOT the operator's
# confirmation — it is never counted as one, never evidence for standard
# work, and never part of an accuracy figure. The operator can still confirm
# or revise the item afterward; a revision is a miss like any other.
DECISION_ACCEPTED = "accepted"

SCOPE_SINGLE_VALUE_KEYS = "single_value_keys"


class DecisionRefused(Exception):
    """A decision the turn was not entitled to make. ``reason`` is a short
    machine kind; the message is what the operator reads. ``andon_id`` is set
    when the refusal was an abnormality that pulled the andon cord."""

    def __init__(
        self, reason: str, message: str, andon_id: Optional[str] = None,
        answer: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.andon_id = andon_id
        # What Kaizen proposed in answer to the andon event (kind, summary,
        # channel) — the next step the operator is offered, never just "no".
        self.answer = dict(answer) if answer else None


# Refusals that are abnormalities — something is wrong with the turn or with
# what was proposed — as opposed to ordinary flow ("nothing is pending", "the
# queue is empty", "confirm the last one first"). An abnormality is never
# just returned to the caller: Jidoka flags it and the andon cord is pulled.
ABNORMAL_REFUSALS = frozenset({
    "no_provenance", "session_not_isolated", "contaminated_turn",
    "output_not_in_domain", "undeclared_output", "item_unreadable", "keg_fault",
    "reply_without_tool", "reply_without_record",
})


def isolation_meta_key(session_id: str) -> str:
    return f"{ISOLATION_META_PREFIX}{session_id}"


# ── the goal's declaration ────────────────────────────────────────────


@dataclass(frozen=True)
class ReferenceSpec:
    path: Path
    key_column: str
    value_column: str
    key_input: str        # the input field looked up in the table
    value_output: str     # the output field the table's value answers
    multi_value_separator: str = "/"


@dataclass(frozen=True)
class OutputDomain:
    """An output field whose value must come from a declared table column."""
    output: str
    path: Path
    column: str
    # Optional: the column holding each value's display name, so a value can
    # be shown as the operator knows it ("<value> <name>").
    name_column: Optional[str] = None


@dataclass(frozen=True)
class EvidenceRule:
    """When repeated correct work counts as a tier-down pattern.

    Evidence is a confirmed decision whose output equals the reference table's
    value for that item's key, counted ACROSS keys. ``scope`` restricts which
    keys can ever count (and so which a keg may cover): with
    ``single_value_keys`` a key holding several values never counts.
    ``threshold`` such confirmations, with no correction against the reference
    table, is the pattern."""
    threshold: int
    scope: str = SCOPE_SINGLE_VALUE_KEYS


@dataclass(frozen=True)
class KegDeclaration:
    """The standard work this goal's decisions may compile into: its name, the
    operator request it answers (the T0 trigger), its GRV-004 scope and
    authority level, and the tiers Kaizen may use to draft a revision, cheapest
    first — a draft that fails its checks is retried one tier up, never
    guessed at."""
    name: str
    request: str
    requests: Tuple[str, ...] = ()      # further example requests it answers
    # How closely a request's content words must overlap one of those examples
    # to be the keg's (grove.intent_match). The examples are illustrations,
    # not an exhaustive list.
    match_threshold: float = 0.8
    # Added to a request's score when it uses the example's verb and adds no
    # word the example lacks (grove.intent_match.overlap). 0 turns it off. It
    # governs what the KEG answers, so it is signed with the keg; it plays no
    # part in what opens a session.
    verb_bonus: float = 0.0
    scope: str = "reserved"
    authority_level: str = "green"
    revision_tiers: Tuple[str, ...] = ("T1", "T2")


@dataclass(frozen=True)
class WorkSession:
    """How this goal's work runs as a session: the operator decides, and the
    system presents the next item. Every phrase here is acted on with no model
    in between, so the block is part of the goal's SESSION RULE and is in
    force only while the operator's signature on it stands. ``enabled`` False
    (or no block) leaves the request-per-item rhythm exactly as it was."""
    enabled: bool = False
    start: Tuple[str, ...] = ()       # opens (or resumes) the work
    confirm: Tuple[str, ...] = ()     # confirms the pending item
    revise: Tuple[str, ...] = ()      # asks to revise it (answered with revise_prompt)
    pause: Tuple[str, ...] = ()       # pauses the session
    # Batch: the keg decides everything it covers at once, under its own
    # authority; the rest come to the operator one at a time as usual.
    batch: Tuple[str, ...] = ()
    batch_label: str = "Batch"            # what the scorecard calls the batch
    done_word: str = "decided"            # the goal's own word for a decided item
    before_label: str = "Before the batch"
    revise_prompt: str = "What should it be?"
    buttons: Tuple[Tuple[str, str], ...] = ()     # (action, label), in order
    # The item card. Fields: n, total, value, why, the item's declared inputs
    # and whatever else the goal's adapter supplies for the item.
    card: str = "{item} {n} of {total}: {label}\nProposed: {value}\n{why}"


@dataclass(frozen=True)
class DecisionWorkConfig:
    goal_id: str
    tool: str
    queue: Path
    inputs: Dict[str, Any]
    outputs: Dict[str, Any]
    reference: Optional[ReferenceSpec]
    output_domains: Tuple[OutputDomain, ...]
    evidence: Optional[EvidenceRule]
    isolation: Optional[str]
    sources: Tuple[Path, ...] = field(default_factory=tuple)
    keg: Optional[KegDeclaration] = None
    # What to do when this goal's work is asked for in a session that is not
    # clean: None (refuse) or "open_clean_session" (the gateway opens one and
    # re-issues the request). Part of the session rule the operator signs.
    on_unclean: Optional[str] = None
    # What one unit of this work is called, singular and plural ("message",
    # "messages"). Read by reports; plays no part in deciding anything.
    item_name: Tuple[str, str] = ("item", "items")
    work_session: WorkSession = field(default_factory=WorkSession)

    @property
    def isolated(self) -> bool:
        return self.isolation == ISOLATION_SOURCES_ONLY


def _resolve(root: Path, raw: Any) -> Path:
    p = Path(str(raw)).expanduser()
    return p if p.is_absolute() else (Path(root) / p)


def _threshold(value: Any, goal_id: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 < float(value) <= 1.0:
        raise ValueError(f"goal {goal_id!r}: match_threshold must be a number in (0, 1]")
    return float(value)


def _bonus(value: Any, goal_id: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= float(value) <= 1.0:
        raise ValueError(f"goal {goal_id!r}: verb_bonus must be a number in [0, 1]")
    return float(value)


def load_config(goal: Any) -> Optional[DecisionWorkConfig]:
    """The goal's ``decision_work`` declaration, or None when it declares
    none. Malformed declarations raise ValueError — a goal whose work cannot
    be read is a configuration defect to fix, never something to guess at."""
    raw = (getattr(goal, "extra", None) or {}).get("decision_work")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError(f"goal {goal.id!r}: decision_work must be a mapping")
    root = Path(goal.root)

    def _need(mapping: Mapping, key: str, where: str) -> Any:
        if key not in mapping or mapping[key] in (None, ""):
            raise ValueError(f"goal {goal.id!r}: {where} is missing {key!r}")
        return mapping[key]

    inputs, outputs = _need(raw, "inputs", "decision_work"), _need(raw, "outputs", "decision_work")
    if not isinstance(inputs, Mapping) or not isinstance(outputs, Mapping):
        raise ValueError(f"goal {goal.id!r}: inputs and outputs must be mappings")

    reference = None
    ref_raw = raw.get("reference_table")
    if ref_raw is not None:
        key_input = _need(ref_raw, "key_input", "reference_table")
        value_output = _need(ref_raw, "value_output", "reference_table")
        if key_input not in inputs or value_output not in outputs:
            raise ValueError(
                f"goal {goal.id!r}: reference_table key_input/value_output must "
                f"name a declared input and output"
            )
        reference = ReferenceSpec(
            path=_resolve(root, _need(ref_raw, "path", "reference_table")),
            key_column=str(_need(ref_raw, "key_column", "reference_table")),
            value_column=str(_need(ref_raw, "value_column", "reference_table")),
            key_input=str(key_input),
            value_output=str(value_output),
            multi_value_separator=str(ref_raw.get("multi_value_separator", "/")),
        )

    domains = []
    for dom in raw.get("output_domains") or []:
        out = _need(dom, "output", "output_domains")
        if out not in outputs:
            raise ValueError(f"goal {goal.id!r}: output_domains names undeclared output {out!r}")
        domains.append(OutputDomain(
            output=str(out),
            path=_resolve(root, _need(dom, "path", "output_domains")),
            column=str(_need(dom, "column", "output_domains")),
            name_column=(str(dom["name_column"]) if dom.get("name_column") else None),
        ))

    evidence = None
    ev_raw = raw.get("evidence")
    if ev_raw is not None:
        if reference is None:
            raise ValueError(f"goal {goal.id!r}: an evidence rule needs a reference_table")
        threshold = _need(ev_raw, "threshold", "evidence")
        if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 1:
            raise ValueError(f"goal {goal.id!r}: evidence threshold must be an integer >= 1")
        scope = str(ev_raw.get("scope", SCOPE_SINGLE_VALUE_KEYS))
        if scope != SCOPE_SINGLE_VALUE_KEYS:
            raise ValueError(f"goal {goal.id!r}: unknown evidence scope {scope!r}")
        evidence = EvidenceRule(threshold=threshold, scope=scope)

    isolation = raw.get("isolation")
    if isolation not in (None, ISOLATION_SOURCES_ONLY):
        raise ValueError(f"goal {goal.id!r}: unknown isolation {isolation!r}")
    on_unclean = raw.get("on_unclean")
    if on_unclean not in (None, ON_UNCLEAN_OPEN_CLEAN):
        raise ValueError(f"goal {goal.id!r}: unknown on_unclean {on_unclean!r}")

    item_name = ("item", "items")
    name_raw = raw.get("item_name")
    if name_raw is not None:
        if not isinstance(name_raw, Mapping) or not all(
            isinstance(name_raw.get(k), str) and name_raw[k].strip() for k in ("one", "many")
        ):
            raise ValueError(
                f"goal {goal.id!r}: item_name must give 'one' and 'many' as words"
            )
        item_name = (name_raw["one"].strip(), name_raw["many"].strip())

    session = WorkSession()
    ws_raw = raw.get("work_session")
    if ws_raw is not None:
        if not isinstance(ws_raw, Mapping):
            raise ValueError(f"goal {goal.id!r}: work_session must be a mapping")

        def _phrases(key: str) -> Tuple[str, ...]:
            value = ws_raw.get(key) or ()
            if not isinstance(value, (list, tuple)) or not all(
                isinstance(v, str) and v.strip() for v in value
            ):
                raise ValueError(
                    f"goal {goal.id!r}: work_session.{key} must be a list of phrases "
                    f"(quote each one: a bare yes or no is read as true or false)")
            return tuple(v.strip() for v in value)

        enabled = ws_raw.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError(f"goal {goal.id!r}: work_session.enabled must be true or false")
        labels = ws_raw.get("buttons") or {}
        if not isinstance(labels, Mapping) or any(k not in ("confirm", "revise") for k in labels):
            raise ValueError(
                f"goal {goal.id!r}: work_session.buttons may label 'confirm' and 'revise'")
        session = WorkSession(
            enabled=enabled,
            start=_phrases("start"), confirm=_phrases("confirm"),
            revise=_phrases("revise"), pause=_phrases("pause"),
            batch=_phrases("batch"),
            batch_label=str(ws_raw.get("batch_label") or WorkSession.batch_label),
            done_word=str(ws_raw.get("done_word") or WorkSession.done_word),
            before_label=str(ws_raw.get("before_label") or WorkSession.before_label),
            revise_prompt=str(ws_raw.get("revise_prompt") or WorkSession.revise_prompt),
            buttons=tuple((k, str(labels[k])) for k in ("confirm", "revise") if labels.get(k)),
            card=str(ws_raw.get("card") or WorkSession.card),
        )
        if session.enabled and (isolation != ISOLATION_SOURCES_ONLY or not session.confirm):
            raise ValueError(
                f"goal {goal.id!r}: an enabled work_session needs an isolated goal "
                f"and at least one confirm phrase")

    keg = None
    keg_raw = raw.get("keg")
    if keg_raw is not None:
        if reference is None or evidence is None:
            raise ValueError(
                f"goal {goal.id!r}: a keg needs a reference_table and an evidence rule"
            )
        tiers = keg_raw.get("revision_tiers") or ("T1", "T2")
        if not isinstance(tiers, (list, tuple)) or not all(isinstance(t, str) for t in tiers):
            raise ValueError(f"goal {goal.id!r}: keg revision_tiers must be a list of tier names")
        also = keg_raw.get("requests") or ()
        if not isinstance(also, (list, tuple)) or not all(
            isinstance(r, str) and r.strip() for r in also
        ):
            raise ValueError(f"goal {goal.id!r}: keg requests must be a list of phrases")
        keg = KegDeclaration(
            name=str(_need(keg_raw, "name", "keg")),
            request=str(_need(keg_raw, "request", "keg")),
            requests=tuple(also),
            match_threshold=_threshold(keg_raw.get("match_threshold", 0.8), goal.id),
            verb_bonus=_bonus(keg_raw.get("verb_bonus", 0.0), goal.id),
            scope=str(keg_raw.get("scope", "reserved")),
            authority_level=str(keg_raw.get("authority_level", "green")),
            revision_tiers=tuple(tiers),
        )

    resolved = getattr(goal, "resolved_sources", None)
    return DecisionWorkConfig(
        goal_id=str(goal.id),
        tool=str(_need(raw, "tool", "decision_work")),
        queue=_resolve(root, _need(raw, "queue", "decision_work")),
        inputs=dict(inputs),
        outputs=dict(outputs),
        reference=reference,
        output_domains=tuple(domains),
        evidence=evidence,
        isolation=isolation,
        sources=tuple(resolved()) if callable(resolved) else (),
        keg=keg,
        on_unclean=on_unclean,
        item_name=item_name,
        work_session=session,
    )


def config_for_goal(goal_id: str, *, dock: Any = None) -> DecisionWorkConfig:
    """Load the named goal's declaration from the Dock. Raises ValueError when
    the goal is absent or declares no decision work."""
    if dock is None:
        from grove.dock import load_dock
        dock = load_dock()
    for goal in (getattr(dock, "goals", None) or ()):
        if goal.id == goal_id:
            cfg = load_config(goal)
            if cfg is None:
                raise ValueError(f"goal {goal_id!r} declares no decision_work")
            return cfg
    raise ValueError(f"no Dock goal {goal_id!r}")


def _keyword_matches(message: str, keywords: Any) -> bool:
    text = (message or "").casefold()
    for kw in keywords or ():
        kw = str(kw).casefold().strip()
        if kw and re.search(rf"(?<!\w){re.escape(kw)}(?!\w)", text):
            return True
    return False


def asks_for_work(message: str, cfg: "DecisionWorkConfig") -> bool:
    """Whether a message, inside this goal's session, asks for the next unit
    of work: token overlap against EVERY request the goal declares (the
    primary and its short continuations), at the goal's declared threshold
    and verb bonus — the same match the keg's trigger makes."""
    if cfg.keg is None:
        return False
    from grove.intent_match import matches
    if matches(
        message, (cfg.keg.request, *cfg.keg.requests), cfg.keg.match_threshold,
        verb_bonus=cfg.keg.verb_bonus,
    ):
        return True
    return cfg.work_session.enabled and routes(message, cfg.work_session.start, cfg)


def score(message: Any, phrases: Any, cfg: "DecisionWorkConfig") -> Tuple[float, Optional[str]]:
    """The declared phrase a message is closest to, as ``(score, phrase)``:
    token overlap on normalized wording (``grove.intent_match``), with the
    goal's verb bonus. The one matcher; nothing here is a keyword list."""
    from grove.intent_match import best_match
    bonus = cfg.keg.verb_bonus if cfg.keg is not None else 0.0
    return best_match(str(message or ""), tuple(phrases or ()), verb_bonus=bonus)


def routes(message: Any, phrases: Any, cfg: "DecisionWorkConfig") -> bool:
    """Whether a message is CLOSE ENOUGH to a declared phrase to route on:
    overlap at or above the goal's declared threshold. For routing only —
    opening, resuming, batching, pausing. A miss just falls through to the
    model. Nothing that RECORDS a decision is ever matched this way (see
    :func:`says`)."""
    threshold = cfg.keg.match_threshold if cfg.keg is not None else 1.0
    return score(message, phrases, cfg)[0] >= float(threshold)


def _portal(fragment: str) -> str:
    """A portal deep link, or the bare route when no base URL is configured."""
    try:
        from grove.prompt.portal_links import resolve_portal_base_url
        base = (resolve_portal_base_url() or "").strip().rstrip("/")
    except Exception:  # noqa: BLE001 — a link is a convenience, never a blocker
        base = ""
    return f"{base}/portal#fragments/{fragment}"


# A button press as the message the gateway delivers for it: the action and
# the id of the item whose card carried the button.
BUTTON_PRESS = re.compile(r"(confirm|revise)\s+#(\S+)", re.IGNORECASE)


def button_message(action: str, item_id: str) -> str:
    return f"{action} #{item_id}"


_PHRASE_RE = re.compile(r"[^\w\s']+")


def _phrase(text: Any) -> str:
    """A phrase as it is compared: lower case, punctuation and extra space
    dropped. "OK!" and "ok" are the same thing to say."""
    return " ".join(_PHRASE_RE.sub(" ", str(text or "").casefold()).split())


def says(message: Any, phrases: Any) -> bool:
    """Whether a message IS one of the declared phrases — the whole message,
    nothing more. The match for anything that WRITES: a confirmation or a
    revision is recorded only on an exact phrase. A near miss ("sounds right",
    "ok but...") is ambiguous, and an ambiguous message goes to the model."""
    said = _phrase(message)
    return bool(said) and said in {_phrase(p) for p in (phrases or ())}


def opens_work(message: str, goal: Any, cfg: "DecisionWorkConfig") -> bool:
    """Whether a message opens this goal's work — deterministic, no model.

    A goal that declares a keg is matched by token overlap
    (``grove.intent_match``) against the keg's PRIMARY request, at the goal's
    declared threshold. Only the primary request opens a session: the keg's
    further examples are short continuations ("Next") that answer inside a
    session already open, and must never turn an unrelated new session into
    this goal's. A goal with no keg falls back to its declared keywords."""
    if cfg.keg is not None:
        from grove.intent_match import matches
        if matches(message, (cfg.keg.request,), cfg.keg.match_threshold):
            return True
        return cfg.work_session.enabled and routes(message, cfg.work_session.start, cfg)
    return _keyword_matches(message, goal.keywords)


def session_rule(cfg: "DecisionWorkConfig") -> Dict[str, Any]:
    """The goal's session rule as declared in the Dock: everything that
    decides, with no model and no keg in between, how a session behaves —
    whether it is isolated, what opens it, how closely, and what happens when
    the work is asked for in a session that is not clean.

    This is the one part of a goal's declaration that takes effect directly,
    so it is in force ONLY while the operator's signature on exactly this rule
    stands (:func:`session_rule_grant`). An edit to any of these fields is a
    draft until it is signed again."""
    rule = {
        "goal": cfg.goal_id,
        "isolation": cfg.isolation,
        "opens_on": cfg.keg.request if cfg.keg is not None else None,
        "match_threshold": cfg.keg.match_threshold if cfg.keg is not None else None,
        "on_unclean": cfg.on_unclean,
    }
    ws = cfg.work_session
    if ws.enabled:
        # Present only while the work session is switched on, so switching it
        # off leaves the rule — and the operator's signature on it — as it was.
        rule["work_session"] = {
            "start": list(ws.start), "confirm": list(ws.confirm),
            "revise": list(ws.revise), "pause": list(ws.pause),
            "after_a_decision": "present_the_next_item",
        }
        if ws.batch:
            rule["work_session"]["batch"] = list(ws.batch)
    return rule


def session_rule_digest(cfg: "DecisionWorkConfig") -> str:
    import hashlib
    return hashlib.sha256(
        json.dumps(session_rule(cfg), sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def session_rule_grant(cfg: "DecisionWorkConfig", *, store: Any = None) -> Optional[Any]:
    """The operator's standing grant on this goal's CURRENT session rule, or
    None when it is unsigned, was revoked, or the rule changed since signing."""
    if store is None:
        from grove.grants import get_grant_store
        store = get_grant_store()
    return store.get_grant(cfg.goal_id, SESSION_RULE_PREFIX + session_rule_digest(cfg))


def isolating_goal_for(
    message: str, *, dock: Any = None, grants: Any = None,
) -> Optional[str]:
    """The isolating goal a message opens work on, or None.

    A goal isolates a session only when its declared session rule is SIGNED
    (:func:`session_rule_grant`) and the message opens its work
    (:func:`opens_work`). An unsigned or changed rule isolates nothing: the
    goal's tool then refuses the work as an unclean session, and Kaizen
    answers that event by proposing the rule for signature. A Dock or
    declaration fault returns None and is logged by the caller."""
    if dock is None:
        from grove.dock import load_dock
        dock = load_dock()
    for goal in (getattr(dock, "goals", None) or ()):
        cfg = load_config(goal)
        if cfg is None or not cfg.isolated or not opens_work(message, goal, cfg):
            continue
        if session_rule_grant(cfg, store=grants) is not None:
            return goal.id
    return None


# ── reference table ───────────────────────────────────────────────────

_WS_RE = re.compile(r"\s+")


def _norm_key(value: Any) -> str:
    return _WS_RE.sub(" ", str(value or "")).strip().casefold()


class ReferenceTable:
    """The goal's declared lookup table: one key column, one value column. A
    cell may hold several values (``6300 / 6310``); such a key is multi-value
    and is never single-valued evidence."""

    def __init__(self, spec: ReferenceSpec) -> None:
        self.spec = spec
        self.rows: List[Dict[str, str]] = []
        self._by_key: Dict[str, Dict[str, str]] = {}
        with open(spec.path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            missing = [
                c for c in (spec.key_column, spec.value_column)
                if c not in (reader.fieldnames or [])
            ]
            if missing:
                raise ValueError(
                    f"reference table {spec.path.name} has no column(s): "
                    f"{', '.join(missing)}"
                )
            for row in reader:
                clean = {k: (v or "").strip() for k, v in row.items() if k}
                self.rows.append(clean)
                self._by_key[_norm_key(clean[spec.key_column])] = clean

    def row(self, key: Any) -> Optional[Dict[str, str]]:
        return self._by_key.get(_norm_key(key))

    def values(self, key: Any) -> List[str]:
        row = self.row(key)
        if row is None:
            return []
        cell = row[self.spec.value_column]
        return [v.strip() for v in cell.split(self.spec.multi_value_separator) if v.strip()]

    def single_value(self, key: Any) -> Optional[str]:
        values = self.values(key)
        return values[0] if len(values) == 1 else None

    def single_value_rows(self) -> List[Tuple[str, str]]:
        """``(key, value)`` for every key with exactly one value — the rows a
        keg scoped to single-value keys may cover."""
        out = []
        for row in self.rows:
            key = row[self.spec.key_column]
            value = self.single_value(key)
            if value is not None:
                out.append((key, value))
        return out


def _domain_values(domain: OutputDomain) -> List[str]:
    with open(domain.path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if domain.column not in (reader.fieldnames or []):
            raise ValueError(f"{domain.path.name} has no column {domain.column!r}")
        return [(row.get(domain.column) or "").strip() for row in reader]


# ── the decision log ──────────────────────────────────────────────────


def default_decisions_dir() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "decisions"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DecisionLog:
    """Append-only JSONL, one file per goal. Records are never rewritten."""

    def __init__(self, goal_id: str, *, directory: Optional[Path] = None) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", goal_id or ""):
            raise ValueError(f"unusable goal id for a decision log: {goal_id!r}")
        self.goal_id = goal_id
        self.path = Path(directory or default_decisions_dir()) / f"{goal_id}.jsonl"

    def records(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        with open(self.path, encoding="utf-8") as fh:
            for number, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError as exc:
                    # A damaged log is never read around: every later count
                    # (evidence, scorecard) would be silently wrong.
                    raise ValueError(
                        f"decision log {self.path.name} line {number} is unreadable"
                    ) from exc
        return out

    def append(self, record: Dict[str, Any]) -> Dict[str, Any]:
        record = dict(record)
        record.setdefault("id", uuid.uuid4().hex)
        record.setdefault("ts", _now())
        record["goal_id"] = self.goal_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return record

    def current_run(self) -> Optional[Dict[str, Any]]:
        runs = [r for r in self.records() if r.get("kind") == KIND_RUN_STARTED]
        return runs[-1] if runs else None

    def start_run(self, label: str = "") -> Dict[str, Any]:
        """Open a new, clearly marked run. Earlier records stay in the file;
        everything that reads "the current run" simply stops seeing them."""
        number = 1 + sum(1 for r in self.records() if r.get("kind") == KIND_RUN_STARTED)
        return self.append({
            "kind": KIND_RUN_STARTED,
            "run_id": uuid.uuid4().hex,
            "run_number": number,
            "label": label,
        })

    def run_records(self) -> List[Dict[str, Any]]:
        """Proposed and decided records of the current run, in order. The
        first write to a fresh log opens run 1 on its own."""
        run = self.current_run()
        if run is None:
            return []
        return [
            r for r in self.records()
            if r.get("run_id") == run["run_id"] and r.get("kind") != KIND_RUN_STARTED
        ]


# ── the work ──────────────────────────────────────────────────────────


class DecisionWork:
    """One goal's decision work: the queue, the log and the checks."""

    def __init__(self, config: DecisionWorkConfig, *, log: Optional[DecisionLog] = None) -> None:
        self.config = config
        self.log = log or DecisionLog(config.goal_id)
        # What Jidoka did about the most recent decision (andon events raised).
        self.last_observations: List[Dict[str, Any]] = []
        self.next_armed = False
        self.last_match: Optional[Dict[str, Any]] = None
        self.last_observation_error: Optional[str] = None

    # -- reading ----------------------------------------------------------

    def reference(self) -> Optional[ReferenceTable]:
        return ReferenceTable(self.config.reference) if self.config.reference else None

    def queue_items(self) -> List[Path]:
        if not self.config.queue.is_dir():
            raise ValueError(f"work queue {self.config.queue} is not a directory")
        return sorted(p for p in self.config.queue.iterdir() if p.is_file() and not p.name.startswith("."))

    def _state(self) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
        """``(proposed by item id, decided by proposed-record id)`` for the run."""
        proposed: Dict[str, Dict[str, Any]] = {}
        decided: Dict[str, Dict[str, Any]] = {}
        for r in self.log.run_records():
            if r.get("kind") == KIND_PROPOSED:
                proposed[r["item_id"]] = r
            elif r.get("kind") == KIND_DECIDED:
                decided[r["ref"]] = r
        return proposed, decided

    def pending(self) -> Optional[Dict[str, Any]]:
        """The proposed record still awaiting the operator's decision."""
        proposed, decided = self._state()
        for record in proposed.values():
            if record["id"] not in decided:
                return record
        return None

    def set_aside_items(self) -> Dict[str, Dict[str, Any]]:
        return {
            r["item_id"]: r for r in self.log.run_records()
            if r.get("kind") == KIND_SET_ASIDE
        }

    def check_one_step_per_turn(self, provenance: Optional[Mapping[str, Any]]) -> None:
        """Refuse to start the next item in the same turn that recorded the
        operator's decision on the last one.

        The operator asks for each item. That keeps every item's tier an
        honest answer to "who decided this": when standard work covers the
        next item it is served with no model at all, which can only happen if
        the request reaches the Dispatcher as its own turn — not if a model,
        already running to record a confirmation, carries on into the next
        item by itself. Ordinary flow, not an abnormality."""
        turn_uid = (provenance or {}).get("turn_uid")
        if not turn_uid:
            return
        for record in reversed(self.log.run_records()):
            if record.get("kind") == KIND_DECIDED:
                if record.get("turn_uid") == turn_uid:
                    raise DecisionRefused(
                        "one_step_per_turn",
                        "The operator's decision is recorded. Stop here: "
                        + ("the system presents the next item itself; do not "
                           "fetch it and do not tell the operator to ask for it."
                           if self.config.work_session.enabled else
                           "the next item starts when the operator asks for it."),
                    )
                return

    def next_item(self) -> Optional[Path]:
        """The first queued item with no proposal in the current run that has
        not been set aside for manual handling."""
        proposed, _ = self._state()
        aside = self.set_aside_items()
        for path in self.queue_items():
            if path.stem not in proposed and path.stem not in aside:
                return path
        return None

    def set_aside(self, *, item_id: str, reason: str, andon_id: Optional[str]) -> Dict[str, Any]:
        """Take the next item out of the queue for manual handling — the
        one-time action of an accepted remedy. Appends a record; the item
        stays in the queue folder and on the log, marked, never dropped."""
        expected = self.next_item()
        if expected is None or expected.stem != item_id:
            raise DecisionRefused(
                "not_next_item",
                f"{item_id} is not the next item in the queue, so it cannot be set aside.",
            )
        run = self._run()
        return self.log.append({
            "kind": KIND_SET_ASIDE, "run_id": run["run_id"], "item_id": item_id,
            "reason": (reason or "")[:300], "andon_id": andon_id,
        })

    # -- checks -----------------------------------------------------------

    def abnormal(
        self, reason: str, message: str,
        provenance: Optional[Mapping[str, Any]] = None,
    ) -> DecisionRefused:
        """Build the refusal for an abnormality, after putting it on the bus:
        a Jidoka flag (anomaly) and the andon event it raises, carrying the
        reason and this turn's provenance. Returns the exception to raise. A
        ledger fault is logged loud and the refusal still stands — the work is
        refused either way."""
        import logging

        prov = dict(provenance or {})
        andon_id = None
        answer = None
        try:
            from grove.andon import raise_andon
            from grove.keg import FLAG_ANOMALY
            andon = raise_andon(
                FLAG_ANOMALY, detector="turn_check",
                goal=self.config.goal_id, summary=f"{reason}: {message}",
                evidence=[{"turn_id": prov.get("turn_id"),
                           "turn_uid": prov.get("turn_uid")}],
                details={"reason": reason, "message": message,
                         "tier": prov.get("tier"),
                         "session_id": prov.get("session_id"),
                         "turn_uid": prov.get("turn_uid"),
                         "item_id": prov.get("item_id"),
                         "request": prov.get("request"),
                         # Earlier tries at this same request, when this turn
                         # is itself a re-issue one tier up.
                         "attempts": list(prov.get("attempts") or [])},
                observed_input={"reason": reason},
                matched_skill=prov.get("t0_pattern"),
                context={"work": self},
            )
            andon_id = andon["andon_id"]
            answer = andon.get("answer")
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).error(
                "[decision_work] could not put refusal %s for %s on the bus: %r",
                reason, self.config.goal_id, exc,
            )
        return DecisionRefused(reason, message, andon_id=andon_id, answer=answer)

    def check_attempt_not_stopped(self, provenance: Optional[Mapping[str, Any]]) -> None:
        """Refuse any further step in a turn whose attempt was already stopped
        and handed up the ladder. The request is being re-issued one tier up;
        letting this tier carry on would have two tiers answer one request."""
        prov = provenance or {}
        if not prov.get("turn_uid") or not prov.get("session_id"):
            return
        from grove import reissue

        waiting = reissue.armed(str(prov["session_id"]))
        if (waiting and waiting.get("turn_uid") == prov["turn_uid"]
                and waiting.get("authorized") == "ladder_rule"):
            raise DecisionRefused(
                "attempt_stopped",
                "This attempt was stopped and the request is being retried one "
                "tier up. Stop here.",
            )
        if reissue.stopped(str(prov["session_id"]), str(prov["turn_uid"])):
            raise DecisionRefused(
                "attempt_stopped",
                "This attempt was stopped and no tier is left to retry on. "
                "Stop here.",
            )

    def unanswered(
        self, reply: str, provenance: Optional[Mapping[str, Any]],
    ) -> Optional[DecisionRefused]:
        """A turn that was asked for this goal's work and replied without
        calling the goal's tool has claimed work it did not do. Returns the
        refusal (flagged and answered on the bus), or None when the turn is
        in order. Deterministic: the request is matched the same way the keg's
        trigger is, and the tool call is read off the turn's own record."""
        prov = provenance or {}
        if not self.config.isolated or prov.get("isolation_goal") != self.config.goal_id:
            return None
        if not asks_for_work(str(prov.get("request") or ""), self.config):
            return None
        if self.config.tool not in set(prov.get("tools_yielded") or ()):
            return self.abnormal(
                "reply_without_tool",
                f"The reply answered a request for {self.config.goal_id} work "
                f"without calling {self.config.tool}, so nothing it says was done.",
                prov,
            )
        if not self.config.work_session.enabled:
            return None
        # In a work session the turn must END in something the system can
        # stand behind: an item proposed on record, a question the model
        # declared it is asking, or an empty queue. A reply that presents an
        # answer it never recorded is a claim, not a decision.
        waiting = self.pending()
        if waiting is not None and waiting.get("turn_uid") == prov.get("turn_uid"):
            return None
        from grove import reissue
        if reissue.turn_note(prov.get("session_id"), prov.get("turn_uid")) == "asked":
            return None
        if waiting is None and self.next_item() is None:
            return None
        return self.abnormal(
            "reply_without_record",
            f"The reply answered a request for {self.config.goal_id} work but "
            f"recorded no decision and declared no question, so what it says "
            f"is not on record.",
            prov,
        )

    def ask(self, provenance: Optional[Mapping[str, Any]], question: str = "") -> None:
        """The model declares that this turn ends with a question to the
        operator about the next item, not a decision. Declared, so the
        system knows the reply is a question — and that nothing was decided.
        A transient note for this turn only; the question itself goes on the
        turn's own intent record."""
        prov = provenance or {}
        self.check_turn(prov)
        if prov.get("session_id") and prov.get("turn_uid"):
            from grove import reissue
            reissue.note_turn(str(prov["session_id"]), str(prov["turn_uid"]), "asked",
                              text=question)

    def absorbs(self, message: Any) -> bool:
        """Whether a message that arrives while the next item is ALREADY on
        its way just repeats what the system is doing ("next", "ok", a press
        on a button): absorbed with no reply. Anything else is a change of
        subject."""
        ws = self.config.work_session
        if not ws.enabled:
            return False
        return bool(
            says(message, ws.confirm) or asks_for_work(str(message or ""), self.config)
            or BUTTON_PRESS.fullmatch(str(message or "").strip()))

    def pause_notice(self) -> str:
        """The one line the operator reads when the session pauses."""
        ws = self.config.work_session
        waiting = self.pending()
        at = waiting["item_id"] if waiting is not None else (
            self.next_item().stem if self.next_item() is not None else None)
        one = self.config.item_name[0]
        where = ""
        if at:
            position, total = self.progress(at)
            where = f" at {one} {position} of {total}"
        resume = f" Say “{ws.start[0]}” to pick it back up." if ws.start else ""
        return f"Paused{where}.{resume}"

    def pause(self, provenance: Optional[Mapping[str, Any]]) -> None:
        """The model found the operator's message is about something else:
        leave the work session and have that message answered outside it.
        Re-issued as it was said; nothing about the pending item changes."""
        prov = provenance or {}
        if not self.config.work_session.enabled or not prov.get("session_id"):
            raise DecisionRefused("work_session_off", "There is no work session to pause.")
        from grove import reissue
        reissue.arm({
            "request": prov.get("request"), "turn_uid": prov.get("turn_uid"),
            "goal": self.config.goal_id, "leave_goal": True,
            "authorized": getattr(session_rule_grant(self.config), "id", None),
        }, session_id=str(prov["session_id"]))

    def decided_reply(self, turn_uid: Any) -> Optional[str]:
        """What the operator reads when a MODEL turn recorded their decision
        (they explained a revision in their own words): the same line a
        no-model confirm gives, from the record — and what the improvement
        loop did about it. None when this turn decided nothing."""
        if not turn_uid:
            return None
        proposed, decided = self._state()
        for record in proposed.values():
            verdict = decided.get(record["id"])
            if verdict is None or verdict.get("turn_uid") != turn_uid:
                continue
            before = self.value_text(record.get("output") or {})
            if verdict.get("decision") == DECISION_CORRECT:
                lines = [f"Revised: {before} → {self.value_text(verdict['output'])}."]
            elif verdict.get("decision") == DECISION_CONFIRM:
                lines = [f"Confirmed: {before}."]
            else:
                return None
            return "\n".join(lines + self.loop_notices(since=verdict.get("ts")))
        return None

    def loop_notices(self, since: Any) -> List[str]:
        """Read off the stores, not off a live watcher: a keg of this goal
        halted, or a proposal of this goal filed, at or after ``since``."""
        from grove import keg as keg_mod
        from grove.pattern_cache import PatternCacheStore, STATUS_HALTED

        lines: List[str] = []
        many = self.config.item_name[1]
        run = self.log.current_run() or {}
        try:
            for entry in PatternCacheStore().all():
                record = keg_mod.keg_record(entry).get("keg") or {}
                if (entry.status == STATUS_HALTED
                        and record.get("dock_goal") == self.config.goal_id
                        and record.get("lineage") == run.get("run_id")):
                    lines.append(
                        f"Keg v{record.get('version')} halted: covered {many} go back to "
                        f"the model until you rule on the fix.")
            from grove.eval.proposal_queue import read_all
            for proposal in read_all():
                keg = (proposal.payload or {}).get("keg") or {}
                if (keg.get("dock_goal") == self.config.goal_id
                        and str(proposal.created_at or "") >= str(since or "")):
                    lines.append(f"Kaizen proposed keg v{keg.get('version')}. "
                                 f"Review in portal: " + _portal("proposals/pending"))
        except Exception:  # noqa: BLE001 — the decision stands; the notice is extra
            import logging
            logging.getLogger(__name__).warning(
                "[decision_work] could not read loop notices for %s", self.config.goal_id)
        return lines

    def check_turn(self, provenance: Optional[Mapping[str, Any]]) -> None:
        """Refuse unless this turn may decide for this goal. Only an isolated
        goal checks; the reasons are stated plainly for the operator."""
        if not self.config.isolated:
            return
        self.check_attempt_not_stopped(provenance)
        if not provenance:
            raise self.abnormal(
                "no_provenance",
                "This decision cannot be recorded: the turn's provenance is "
                "not available, so its sources cannot be verified.",
            )
        if provenance.get("isolation_goal") != self.config.goal_id:
            raise self.abnormal(
                "session_not_isolated",
                f"This session includes turns outside the {self.config.goal_id} "
                f"goal, so its context is not clean and the work cannot run here.",
                provenance,
            )
        sections = RECALL_SECTIONS & set(provenance.get("sections") or ())
        tools = RECALL_TOOLS & set(provenance.get("tools_yielded") or ())
        hits = int(provenance.get("cellar_hits") or 0)
        if sections or tools or hits:
            found = sorted(sections) + sorted(tools) + (["cellar hits"] if hits else [])
            raise self.abnormal(
                "contaminated_turn",
                "This decision cannot be recorded: the turn drew on recalled "
                f"context ({', '.join(found)}). Work for this goal uses its "
                "declared sources only.",
                provenance,
            )

    def check_output(
        self, output: Mapping[str, Any],
        provenance: Optional[Mapping[str, Any]] = None,
    ) -> None:
        missing = [k for k in self.config.outputs if output.get(k) in (None, "")]
        if missing:
            raise DecisionRefused(
                "missing_output", f"The decision is missing: {', '.join(missing)}.")
        unknown = [k for k in output if k not in self.config.outputs]
        if unknown:
            raise self.abnormal(
                "undeclared_output", f"Not a declared output: {', '.join(unknown)}.",
                provenance)
        for domain in self.config.output_domains:
            value = str(output[domain.output]).strip()
            if value not in _domain_values(domain):
                raise self.abnormal(
                    "output_not_in_domain",
                    f"{domain.output} {value!r} is not in {domain.path.name} "
                    f"({domain.column}).",
                    provenance,
                )

    # -- writing ----------------------------------------------------------

    def _run(self) -> Dict[str, Any]:
        return self.log.current_run() or self.log.start_run("first run")

    def record(
        self,
        *,
        item_id: str,
        inputs: Mapping[str, Any],
        output: Mapping[str, Any],
        reasoning: str,
        provenance: Optional[Mapping[str, Any]],
        keg: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Record a proposed decision for the next queued item.

        ``inputs`` come from the adapter's own parse of the item, never from
        the model. ``keg`` names the keg and version when a keg, not a model,
        produced the output."""
        self.check_turn(provenance)
        waiting = self.pending()
        if waiting is not None:
            raise DecisionRefused(
                "prior_unconfirmed",
                f"{waiting['item_id']} is still waiting for the operator to "
                f"confirm or correct it.",
            )
        expected = self.next_item()
        if expected is None:
            raise DecisionRefused("queue_empty", "Every queued item is already decided.")
        if item_id != expected.stem:
            raise DecisionRefused(
                "not_next_item",
                f"The next item is {expected.stem}, not {item_id}.",
            )
        self.check_output(output, provenance)
        prov = dict(provenance or {})
        run = self._run()
        return self.log.append({
            "kind": KIND_PROPOSED,
            "run_id": run["run_id"],
            "item_id": item_id,
            "inputs": dict(inputs),
            "output": {k: str(v).strip() for k, v in output.items()},
            "reasoning": (reasoning or "").strip()[:500],
            "tier": prov.get("tier"),
            "model": prov.get("model"),
            "keg": dict(keg) if keg else None,
            "session_id": prov.get("session_id"),
            "turn_id": prov.get("turn_id"),
            "turn_uid": prov.get("turn_uid"),
            **({"batch": self.current_batch()} if self.current_batch() else {}),
        })

    def current_batch(self) -> Optional[str]:
        """The batch this run is in, if one has begun: the batch id on the
        run's most recent proposed record that carries one. Items the keg
        left for a model are part of the same batch, so they carry it too."""
        for record in reversed(self.log.run_records()):
            if record.get("kind") == KIND_PROPOSED and record.get("batch"):
                return str(record["batch"])
        return None

    def serving_keg(self) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
        """``(spec, reference)`` of the keg now serving this goal, or None."""
        from grove import keg as keg_mod
        from grove.pattern_cache import PatternCacheStore, STATUS_ACTIVE

        for entry in PatternCacheStore().all():
            if entry.status != STATUS_ACTIVE:
                continue
            spec = keg_mod.keg_of(entry)
            if spec and spec.get("dock_goal") == self.config.goal_id:
                return spec, {"name": spec.get("name"), "version": spec.get("version"),
                              "pattern_id": entry.pattern_id}
        return None

    def batch_pass(
        self, provenance: Optional[Mapping[str, Any]], inputs_for: Any,
    ) -> Dict[str, Any]:
        """Decide every queued item the serving keg covers, at once and with
        no model, each recorded as ACCEPTED under the keg's own authority.
        Items it does not cover — and any item that cannot be read — are left
        in the queue untouched, for the ordinary one-at-a-time loop.

        The operator's signature on the keg was the checkpoint, so this runs
        only for a keg whose signed authority level is green. ``inputs_for``
        is the adapter's reader: a queue path in, the item's declared inputs
        out (ValueError when the item cannot be read). Returns the counts."""
        from grove import keg as keg_mod

        self.check_turn(provenance)
        if self.pending() is not None:
            raise DecisionRefused(
                "prior_unconfirmed", "An item is waiting for your decision first.")
        prov = dict(provenance or {})
        proposed, _ = self._state()
        aside = self.set_aside_items()
        todo = [p for p in self.queue_items() if p.stem not in proposed and p.stem not in aside]
        serving = self.serving_keg()
        out = {"batch": None, "total": len(todo), "coded": 0, "left": len(todo),
               "keg": None, "reason": None}
        if serving is None:
            out["reason"] = "no_keg"
            return out
        spec, keg_ref = serving
        out["keg"] = keg_ref
        if spec.get("authority_level") != "green":
            out["reason"] = "not_green"
            return out
        run = self._run()
        batch_id = uuid.uuid4().hex
        out["batch"] = batch_id
        for path in todo:
            try:
                inputs = dict(inputs_for(path))
            except (ValueError, OSError):
                continue          # unreadable: the ordinary loop surfaces it
            output = keg_mod.evaluate(spec, inputs)
            if output is None:
                continue
            clean = {k: str(v).strip() for k, v in output.items()}
            self.check_output(clean, prov)
            record = self.log.append({
                "kind": KIND_PROPOSED, "run_id": run["run_id"], "item_id": path.stem,
                "inputs": inputs, "output": clean,
                "reasoning": f"keg {keg_ref['name']} v{keg_ref['version']}",
                "tier": "T0", "model": "pattern_cache", "keg": dict(keg_ref),
                "session_id": prov.get("session_id"), "turn_id": prov.get("turn_id"),
                "turn_uid": prov.get("turn_uid"), "batch": batch_id,
            })
            self.log.append({
                "kind": KIND_DECIDED, "run_id": run["run_id"], "ref": record["id"],
                "item_id": path.stem, "decision": DECISION_ACCEPTED, "output": clean,
                "by": "keg_authority", "session_id": prov.get("session_id"),
                "turn_id": prov.get("turn_id"), "turn_uid": prov.get("turn_uid"),
            })
            out["coded"] += 1
        out["left"] = out["total"] - out["coded"]
        if not out["coded"]:
            # Nothing covered: no batch began, so nothing later is labeled one.
            out["batch"] = None
        return out

    def rule_on(
        self, item_id: str, *, decision: str,
        corrected_output: Optional[Mapping[str, Any]] = None,
        provenance: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """The operator's ruling on an item the keg ACCEPTED without review:
        a confirmation (it becomes confirmed) or a revision (a miss — Jidoka
        sees it exactly as it sees any correction, and the keg halts). Only an
        accepted item can be ruled on this way; a decision the operator
        already made stands."""
        if decision not in (DECISION_CONFIRM, DECISION_CORRECT):
            raise DecisionRefused(
                "unknown_decision", "The decision must be 'confirm' or 'correct'.")
        proposed, decided = self._state()
        record = proposed.get(item_id)
        verdict = decided.get(record["id"]) if record else None
        if record is None or verdict is None:
            raise DecisionRefused("not_decided", f"{item_id} has not been decided.")
        if verdict.get("decision") != DECISION_ACCEPTED:
            raise DecisionRefused(
                "already_ruled", f"You have already ruled on {item_id}.")
        if decision == DECISION_CORRECT:
            if not corrected_output:
                raise DecisionRefused(
                    "missing_correction", "A revision needs the revised value.")
            self.check_output(corrected_output, provenance)
            final = {k: str(v).strip() for k, v in corrected_output.items()}
            if final == record["output"]:
                raise DecisionRefused(
                    "correction_matches",
                    "The revised value is the same as the keg's; that is a confirmation.")
        else:
            final = dict(record["output"])
        prov = dict(provenance or {})
        ruled = self.log.append({
            "kind": KIND_DECIDED, "run_id": record["run_id"], "ref": record["id"],
            "item_id": item_id, "decision": decision, "output": final,
            "after": DECISION_ACCEPTED, "session_id": prov.get("session_id"),
            "turn_id": prov.get("turn_id"), "turn_uid": prov.get("turn_uid"),
        })
        self._observe(record, ruled)
        return ruled

    def _observe(self, proposed: Mapping[str, Any], decided: Mapping[str, Any]) -> None:
        """Jidoka observes a feed write. The decision is already on record; a
        watcher fault is logged loud and kept for the caller to report, and
        never un-records what the operator decided."""
        self.last_observations = []
        self.last_observation_error = None
        try:
            from grove.detectors import decision_feed
            self.last_observations = decision_feed.observe(self, proposed, decided)
        except Exception as exc:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).error(
                "[decision_work] Jidoka could not observe decision %s for %s: %r",
                decided.get("id"), self.config.goal_id, exc,
            )
            self.last_observation_error = f"{type(exc).__name__}: {exc}"

    def decide(
        self,
        *,
        decision: str,
        corrected_output: Optional[Mapping[str, Any]] = None,
        provenance: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Record the operator's confirmation or correction of the pending
        proposal, as a new record referencing it."""
        if decision not in (DECISION_CONFIRM, DECISION_CORRECT):
            raise DecisionRefused(
                "unknown_decision", "The decision must be 'confirm' or 'correct'.")
        waiting = self.pending()
        if waiting is None:
            raise DecisionRefused(
                "nothing_pending", "No decision is waiting for confirmation.")
        if decision == DECISION_CORRECT:
            if not corrected_output:
                raise DecisionRefused(
                    "missing_correction", "A correction needs the corrected value.")
            self.check_output(corrected_output, provenance)
            final = {k: str(v).strip() for k, v in corrected_output.items()}
            if final == waiting["output"]:
                raise DecisionRefused(
                    "correction_matches",
                    "The corrected value is the same as the proposed one; "
                    "that is a confirmation.",
                )
        else:
            final = dict(waiting["output"])
        prov = dict(provenance or {})
        decided = self.log.append({
            "kind": KIND_DECIDED,
            "run_id": waiting["run_id"],
            "ref": waiting["id"],
            "item_id": waiting["item_id"],
            "decision": decision,
            "output": final,
            "session_id": prov.get("session_id"),
            "turn_id": prov.get("turn_id"),
            "turn_uid": prov.get("turn_uid"),
        })
        self._observe(waiting, decided)
        self.present_next_after(decided, prov)
        return decided

    # -- the work session -------------------------------------------------

    def present_next_after(self, decided: Mapping[str, Any],
                           provenance: Mapping[str, Any]) -> bool:
        """After a decision, have the system present the next item: re-issue
        the goal's own request, so it is routed from scratch (standard work
        first, a model only if no keg covers the item). One re-issue per
        decision, never more, and only under the operator's signed session
        rule. Returns whether it was armed. Nothing is armed when the work
        session is off: the operator then asks for each item, as before."""
        self.next_armed = False
        cfg = self.config
        if not cfg.work_session.enabled or cfg.keg is None:
            return False
        session_id = provenance.get("session_id")
        if not session_id or provenance.get("isolation_goal") != cfg.goal_id:
            return False
        grant = session_rule_grant(cfg)
        if grant is None:
            return False
        from grove import reissue

        reissue.arm({
            "request": cfg.keg.request, "authorized": getattr(grant, "id", None),
            "turn_uid": provenance.get("turn_uid"),
            "goal": cfg.goal_id, "advance": True,
        }, session_id=str(session_id))
        self.next_armed = True
        return True

    def session_action(self, message: Any) -> Optional[Dict[str, Any]]:
        """What a message in this goal's work session unambiguously IS, decided
        with no model — or None, which sends it to the model.

          confirm        — the pending item is confirmed
          revise         — the message is exactly a valid value for the goal's
                           one output: the pending item is revised to it
          revise_prompt  — the operator asked to revise; ask what it should be
          present        — the work was asked for while an item is pending:
                           show that item again
          summary        — the work was asked for and the queue is done
        """
        ws = self.config.work_session
        self.last_match = None
        if not ws.enabled:
            return None
        action = self._session_action(message)
        self.last_match = self.match_trace(message, action)
        return action

    def match_trace(self, message: Any, action: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        """How a work-session message scored against the goal's declared
        phrases, for the turn's recognition record: the closest phrase of any
        kind, its score, whether the system acted on it, and what as. A
        message that fell through to the model is recorded too — repeated
        near misses read the same way are how a phrase earns its place."""
        cfg, ws = self.config, self.config.work_session
        groups = {
            "confirm": ws.confirm, "revise": ws.revise, "start": ws.start,
            "pause": ws.pause, "batch": ws.batch,
            "request": ((cfg.keg.request, *cfg.keg.requests) if cfg.keg else ()),
        }
        best = (0.0, None, None)
        for kind, phrases in groups.items():
            value, phrase = score(message, phrases, cfg)
            if value > best[0]:
                best = (value, phrase, kind)
        return {
            "message": _phrase(message)[:120],
            "best_phrase": best[1], "phrase_kind": best[2],
            "score": round(float(best[0]), 3),
            "fired": action is not None,
            "action": (action or {}).get("action"),
            # How it fired: an exact phrase or value (the only way a decision
            # is recorded), or overlap at the threshold (routing only).
            "match": (None if action is None else
                      "button" if action.get("button") else
                      "exact" if action.get("action") in ("confirm", "revise", "revise_prompt")
                      else "overlap"),
        }

    def offer_buttons(self, record: Mapping[str, Any],
                      provenance: Optional[Mapping[str, Any]]) -> bool:
        """Offer the goal's declared buttons on the card for ``record``. Each
        button carries the item's id, so a press is always about THAT item."""
        ws = self.config.work_session
        session_id = (provenance or {}).get("session_id")
        if not ws.enabled or not ws.buttons or not session_id:
            return False
        from grove import reissue
        reissue.offer_actions(str(session_id), {
            "goal": self.config.goal_id, "item_id": record["item_id"],
            "buttons": [[action, label] for action, label in ws.buttons],
        })
        return True

    def _session_action(self, message: Any) -> Optional[Dict[str, Any]]:
        ws = self.config.work_session
        waiting = self.pending()
        pressed = BUTTON_PRESS.fullmatch(str(message or "").strip())
        if pressed:
            # A button press, delivered as a message that names its item. It
            # is acted on only for that item: a press on an old card is
            # refused when the step runs, never applied to the item waiting.
            kind, item_id = pressed.group(1).lower(), pressed.group(2)
            return {"action": "confirm" if kind == "confirm" else "revise_prompt",
                    "item_id": item_id, "button": True}
        if routes(message, ws.pause, self.config):
            return {"action": "pause"}
        if waiting is not None:
            if says(message, ws.confirm):
                return {"action": "confirm", "item_id": waiting["item_id"]}
            if says(message, ws.revise):
                return {"action": "revise_prompt", "item_id": waiting["item_id"]}
            value = self._exact_value(message)
            if value is not None:
                return {"action": "revise", "item_id": waiting["item_id"], "output": value}
            if asks_for_work(str(message or ""), self.config) or routes(
                    message, ws.batch, self.config):
                return {"action": "present", "item_id": waiting["item_id"]}
            return None
        if routes(message, ws.batch, self.config):
            return {"action": "summary"} if self.next_item() is None else {"action": "batch"}
        if asks_for_work(str(message or ""), self.config) and self.next_item() is None:
            return {"action": "summary"}
        return None

    def _exact_value(self, message: Any) -> Optional[Dict[str, str]]:
        """The message as a revised output, when the goal has ONE output, that
        output has a declared domain, and the message is exactly one of its
        values. Anything else (a reason, a description) is not a value."""
        outputs = list(self.config.outputs)
        if len(outputs) != 1:
            return None
        said = str(message or "").strip()
        for domain in self.config.output_domains:
            if domain.output == outputs[0] and said and said in _domain_values(domain):
                return {outputs[0]: said}
        return None

    def progress(self, item_id: str) -> Tuple[int, int]:
        """``(position, total)`` of an item in the queue, counted from one."""
        names = [p.stem for p in self.queue_items()]
        return (names.index(item_id) + 1 if item_id in names else 0), len(names)

    def value_text(self, output: Mapping[str, Any]) -> str:
        """An output as the operator knows it: the value, with its display
        name when the goal declares where to find one."""
        parts = []
        for name, value in output.items():
            text = str(value)
            for domain in self.config.output_domains:
                if domain.output == name and domain.name_column:
                    with open(domain.path, newline="", encoding="utf-8-sig") as fh:
                        for row in csv.DictReader(fh):
                            if (row.get(domain.column) or "").strip() == text:
                                label = (row.get(domain.name_column) or "").strip()
                                text = f"{text} {label}".strip()
                                break
            parts.append(text if len(output) == 1 else f"{name} {text}")
        return ", ".join(parts)

    def why(self, record: Mapping[str, Any]) -> str:
        """Who decided a proposed item and why, in one line: the keg version
        and the rule that fired, or the model's own reason and its tier."""
        keg_ref = record.get("keg")
        value = self.value_text({k: v for k, v in (record.get("output") or {}).items()})
        if keg_ref:
            head = f"Keg v{keg_ref.get('version')}, no model call"
            try:
                from grove import keg as keg_mod
                from grove.pattern_cache import PatternCacheStore

                entry = PatternCacheStore().get(str(keg_ref.get("pattern_id")))
                spec = keg_mod.keg_of(entry) if entry is not None else None
                rule = keg_mod.match(spec, record.get("inputs") or {}) if spec else None
                if rule is not None:
                    groups = keg_mod.parse_condition(str(rule.get("if")), spec.get("inputs") or {})
                    if len(groups) == 1 and len(groups[0]) == 1 and groups[0][0][1] == "==":
                        fired = str(groups[0][0][2])
                    else:
                        fired = keg_mod.describe_condition(
                            str(rule.get("if")), spec.get("inputs") or {})
                    return f"{head}: {fired} → {value}"
            except Exception:  # noqa: BLE001 — the card still says who decided
                import logging
                logging.getLogger(__name__).warning(
                    "[decision_work] could not read the rule keg %s fired",
                    keg_ref.get("pattern_id"))
            return head + "."
        reason = str(record.get("reasoning") or "").strip()
        tier = record.get("tier") or "model"
        return f"Model ({tier}): {reason}" if reason else f"Decided by a model ({tier})."

    def card(self, record: Mapping[str, Any], fields: Optional[Mapping[str, Any]] = None) -> str:
        """The pending item as the operator sees it, from the goal's own card
        template. ``fields`` are whatever the goal's adapter knows about the
        item beyond its declared inputs. A field the template names and
        nothing supplies is left empty, never a crash."""
        position, total = self.progress(str(record.get("item_id")))
        label_key = self.config.reference.key_input if self.config.reference else None
        inputs = dict(record.get("inputs") or {})

        class _Fields(dict):
            def __missing__(self, key: str) -> str:
                return ""

        values = _Fields({
            **inputs, **dict(fields or {}),
            "item": self.config.item_name[0].capitalize(),
            "n": position, "total": total,
            "label": str(inputs.get(label_key, "")) if label_key else "",
            "value": self.value_text(record.get("output") or {}),
            "why": self.why(record),
        })
        return self.config.work_session.card.format_map(values).strip()

    def notices(self) -> List[str]:
        """What the improvement loop did when it saw the last decision, one
        line each: a keg halted, a proposal waiting for signature. The loop
        never blocks the work; it says what happened and the work goes on."""
        lines: List[str] = []
        one, many = self.config.item_name
        for event in self.last_observations:
            if event.get("halted"):
                keg = (event.get("details") or {}).get("keg") or {}
                lines.append(
                    f"Keg v{keg.get('version')} halted: covered {many} go back to the "
                    f"model until you rule on the fix.")
            answer = event.get("answer") or {}
            if answer.get("kind") == "standard_work":
                version = (answer.get("detail") or {}).get("version")
                lines.append(
                    "Kaizen proposed " + (f"keg v{version}" if version else "a change")
                    + ". Review in portal: " + _portal("proposals/pending"))
            elif answer.get("summary") and answer.get("kind") != "watch":
                lines.append(str(answer["summary"]))
        if self.last_observation_error:
            lines.append("The watcher failed after recording this decision: "
                         + self.last_observation_error)
        return lines

    def session_step(
        self, action: Mapping[str, Any], provenance: Optional[Mapping[str, Any]],
        fields: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Carry out one work-session action (see :meth:`session_action`) and
        return what the operator reads: ``{"reply", "item_id", "decided",
        "presented"}``. Decisions are recorded here, by the system, from the
        action — never inferred from anything a model wrote. An action that
        names an item which is no longer the pending one is refused: a card
        that is out of date never decides the item now waiting."""
        ws = self.config.work_session
        if not ws.enabled:
            raise DecisionRefused("work_session_off", "This goal's work session is switched off.")
        kind = action.get("action")
        if kind == "summary":
            return {"reply": self.summary() + "\nScorecard and audit check: " + _portal("audit/"),
                    "item_id": None, "decided": False, "presented": False}
        if kind == "batch":
            return self._batch_step(provenance, action.get("inputs_for"))
        waiting = self.pending()
        named = action.get("item_id")
        if named and (waiting is None or named != waiting["item_id"]):
            raise DecisionRefused("stale_card", "That card is out of date.")
        if waiting is None:
            raise DecisionRefused("nothing_pending", "No decision is waiting for confirmation.")
        if kind == "present":
            self.offer_buttons(waiting, provenance)
            return {"reply": self.card(waiting, fields), "item_id": waiting["item_id"],
                    "decided": False, "presented": True}
        if kind == "revise_prompt":
            return {"reply": ws.revise_prompt, "item_id": waiting["item_id"],
                    "decided": False, "presented": False}
        before = self.value_text(waiting.get("output") or {})
        if kind == "confirm":
            self.decide(decision=DECISION_CONFIRM, provenance=provenance)
            lead = f"Confirmed: {before}."
        elif kind == "revise":
            decided = self.decide(decision=DECISION_CORRECT,
                                  corrected_output=action.get("output"), provenance=provenance)
            lead = f"Revised: {before} → {self.value_text(decided['output'])}."
        else:
            raise DecisionRefused("unknown_action", f"Unknown work-session action {kind!r}.")
        return {"reply": "\n".join([lead] + self.notices()), "item_id": waiting["item_id"],
                "decided": True, "presented": False, "next_armed": self.next_armed}

    def _batch_step(self, provenance: Optional[Mapping[str, Any]],
                    inputs_for: Any) -> Dict[str, Any]:
        """Run the keg pass and say what happened in one message; then the
        items it left come to the operator one at a time, as usual."""
        if inputs_for is None:
            raise DecisionRefused(
                "no_reader", "This goal's tool did not say how to read its items.")
        result = self.batch_pass(provenance, inputs_for)
        one, many = self.config.item_name
        total, coded, left = result["total"], result["coded"], result["left"]
        if result["reason"] == "no_keg":
            lead = (f"No signed keg is serving, so nothing is decided in bulk. "
                    f"Bringing all {total} {one if total == 1 else many} to you one at a time.")
        elif result["reason"] == "not_green":
            lead = (f"Keg v{result['keg']['version']} is not signed to act without review, "
                    f"so nothing is decided in bulk. Bringing all {total} to you one at a time.")
        else:
            done = self.config.work_session.done_word.capitalize()
            lead = (f"{done} {coded} of {total} · {coded} by the keg "
                    f"v{result['keg']['version']} · 0 model calls.")
            if left:
                lead += (f"\n{left} {'needs' if left == 1 else 'need'} a model. "
                         f"Bringing {'it' if left == 1 else 'them'} to you one at a time.")
            else:
                lead += "\n" + self.summary() + "\nScorecard and audit check: " + _portal("audit/")
        # One presentation armed, exactly as after a decision: the next item
        # is its own freshly routed turn.
        armed = False
        if left:
            armed = self.present_next_after({"id": result["batch"]}, dict(provenance or {}))
        return {"reply": lead, "item_id": None, "decided": False, "presented": False,
                "next_armed": armed, "batch": result}

    def tally(self, batch: Optional[str] = None) -> Dict[str, int]:
        """How this run's decided items stand — or one batch's, when given.
        Three outcomes, never merged: confirmed by the operator, accepted by
        the keg without review, revised."""
        proposed, decided = self._state()
        done = [r for r in proposed.values()
                if r["id"] in decided and (batch is None or r.get("batch") == batch)]
        kinds = [decided[r["id"]].get("decision") for r in done]
        return {
            "decided": len(done),
            "by_keg": sum(1 for r in done if r.get("keg")),
            "confirmed": kinds.count(DECISION_CONFIRM),
            "accepted": kinds.count(DECISION_ACCEPTED),
            "revised": kinds.count(DECISION_CORRECT),
        }

    def summary(self) -> str:
        """The work so far in one line. In a batch: how many the keg coded
        and how many the operator reviewed. Otherwise: who decided, and how
        many were revised. Accepted items are named as not reviewed."""
        one, many = self.config.item_name
        batch = self.current_batch()
        t = self.tally(batch)
        n = t["decided"]
        if batch:
            reviewed = t["confirmed"] + t["revised"]
            return (
                f"{n} {self.config.work_session.done_word}: {t['accepted']} by the keg, "
                f"not reviewed; {reviewed} reviewed by you; {t['revised']} revised."
            )
        return (
            f"Queue complete: {n} {one if n == 1 else many} decided — "
            f"{t['by_keg']} by the keg, {n - t['by_keg']} by a model, {t['revised']} revised."
        )

    def history(self) -> List[Dict[str, Any]]:
        """This run's decided items as replay cases: what standard work served
        at the time, what the operator settled on, and whether a keg served
        it. Kaizen backtests a drafted keg over this."""
        proposed, decided = self._state()
        label_key = self.config.reference.key_input if self.config.reference else None
        table = self.reference()
        table_name = self.config.reference.path.name if self.config.reference else ""

        def _note(key: Any) -> str:
            """Why the reference table gives no single answer for this key."""
            if table is None:
                return ""
            values = table.values(key)
            if len(values) > 1:
                return f"{len(values)} values in {table_name}"
            if not values:
                return f"not in {table_name}"
            return ""

        cases = []
        for record in proposed.values():
            verdict = decided.get(record["id"])
            if verdict is None:
                continue
            inputs = dict(record.get("inputs") or {})
            cases.append({
                "ref": record["item_id"],
                "label": str(inputs.get(label_key, "")) if label_key else "",
                "inputs": inputs,
                "served": dict(record["output"]),
                # An item the keg accepted without review is not ground truth:
                # the operator never ruled on it.
                "confirmed": (None if verdict["decision"] == DECISION_ACCEPTED
                              else dict(verdict["output"])),
                "served_by_keg": bool(record.get("keg")),
                "note": _note(inputs.get(label_key)) if label_key else "",
                "decision": verdict["decision"],
                "turn_id": record.get("turn_id"),
            })
        return cases

    def apply_keg(
        self,
        spec: Mapping[str, Any],
        *,
        item_id: str,
        inputs: Mapping[str, Any],
        keg_ref: Mapping[str, Any],
        provenance: Optional[Mapping[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Decide the next item with a keg — no model. Returns the proposed
        record, or None when the keg does not answer this item (no rule
        matches, or the matching rule defers); the caller then hands the item
        back to the interpreter. The same turn checks apply as for a model."""
        from grove import keg as keg_mod

        output = keg_mod.evaluate(spec, inputs)
        if output is None:
            return None
        return self.record(
            item_id=item_id, inputs=inputs, output=output,
            reasoning=f"keg {keg_ref.get('name')} v{keg_ref.get('version')}",
            provenance=provenance, keg=keg_ref,
        )

    # -- Jidoka's evidence ------------------------------------------------

    def evidence(self) -> Dict[str, Any]:
        """Count this run's evidence for a tier-down pattern, per the goal's
        declared rule. Jidoka reads this; it flags, it never fixes.

        A confirmation counts when the item's key has a single value in the
        reference table and the confirmed output equals it. A correction
        counts AGAINST the pattern when the operator changed an output that
        equaled the table's single value — the table and the operator's
        judgment disagree, so there is no pattern to compile."""
        rule, ref_spec = self.config.evidence, self.config.reference
        if rule is None or ref_spec is None:
            return {"rule": None, "met": False, "confirmations": 0,
                    "corrections_against_reference": 0, "evidence": []}
        table = ReferenceTable(ref_spec)
        proposed, decided = self._state()
        confirmations: List[Dict[str, Any]] = []
        against: List[Dict[str, Any]] = []
        for record in proposed.values():
            verdict = decided.get(record["id"])
            if verdict is None:
                continue
            expected = table.single_value(record["inputs"].get(ref_spec.key_input))
            if expected is None:
                continue  # multi-value or unknown key: never evidence
            entry = {
                "item_id": record["item_id"], "turn_id": record.get("turn_id"),
                "turn_uid": record.get("turn_uid"), "decided_id": verdict["id"],
            }
            proposed_value = record["output"].get(ref_spec.value_output)
            if verdict["decision"] == DECISION_CONFIRM and proposed_value == expected:
                confirmations.append(entry)
            elif verdict["decision"] == DECISION_CORRECT and proposed_value == expected:
                against.append(entry)
        return {
            "rule": {"threshold": rule.threshold, "scope": rule.scope},
            "confirmations": len(confirmations),
            "corrections_against_reference": len(against),
            "met": len(confirmations) >= rule.threshold and not against,
            "evidence": confirmations,
            "against": against,
        }
