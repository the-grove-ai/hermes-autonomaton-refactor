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

# What becomes of a session's content once the work is done.
#   records_only — the work's knowledge lives in its records (the decision
#       log, the ledger, the keg). Work turns are never summarized into the
#       Cellar or mined into memory; only what was said OUTSIDE the work is.
#   compact — the session is summarized like any other conversation.
# A goal that will not READ ambient memory does not WRITE to it either, so an
# isolated goal defaults to records_only.
SESSION_MEMORY_RECORDS_ONLY = "records_only"
SESSION_MEMORY_COMPACT = "compact"

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


BATCH_KEG_FIRST = "keg_first"
BATCH_ITEM_ORDER = "item_order"
# A transient goal note: the items next in the queue that the serving keg
# (named by version) does not answer, so each goes straight to a model.
_NEXT_FOR_MODEL = "for_model:"


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
    # A second way a rule is earned, for a key the reference table does not
    # list at all: this many model-decided items with that key, every one
    # confirmed by the operator with the same output, and none revised. The
    # operator's own confirmations are the evidence. None: not declared.
    confirmed_key_threshold: Optional[int] = None
    # A third way: a NEW key that is an existing key under another name (a
    # sender that was renamed, an account that changed hands).
    # ``alias_confirmations``
    # operator-confirmed decisions, plus an identity match the goal declares:
    # an input that is the SAME on the new key's item as on the existing
    # key's items (``alias_same``), or an input whose text NAMES the existing
    # key (``alias_names``). None: not declared.
    alias_confirmations: Optional[int] = None
    alias_same: Tuple[str, ...] = ()
    alias_names: Tuple[str, ...] = ()


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
    # Phrases the operator approved in conversation, as (verb, phrase). They
    # are already merged into the verb's list above; kept apart here so the
    # session rule lists only what the Dock declares.
    learned: Tuple[Tuple[str, str], ...] = ()


# What a pattern counts, and what it may become. A pattern is declared in the
# Dock; a new kind of count or outcome is the only thing that takes code.
COUNTS_PHRASE_READ_AS = "phrase_read_as"            # a model read a phrase as a verb
COUNTS_TURNS_FAILED_UPWARD = "turns_failed_upward"  # the ladder rule moved a turn up
BECOMES_ALIAS = "alias"                # a phrase for a verb; approved in conversation
BECOMES_ROUTING_KEG = "routing_keg"    # where a class of work starts; signed
PATTERN_OUTCOMES = {
    COUNTS_PHRASE_READ_AS: BECOMES_ALIAS,
    COUNTS_TURNS_FAILED_UPWARD: BECOMES_ROUTING_KEG,
}
# Verbs whose reading by a model leaves a record that can be counted: the
# decision it recorded. Only these can earn an alias.
READABLE_VERBS = frozenset({"confirm"})
ANSWER_WORDS = ("yes", "y", "yeah", "yep", "sure", "ok", "okay", "no", "nope", "not now")
REFUSE_WORDS = ("no", "not", "don't", "dont", "never", "wrong", "hmm", "maybe", "wait",
                "but", "why", "what")


@dataclass(frozen=True)
class Pattern:
    """One thing the system counts in this goal's records, the count that
    matters, and what reaching it becomes."""
    id: str
    counts: str
    threshold: int
    becomes: str
    verb: Optional[str] = None
    propose: bool = True
    max_words: int = 4


@dataclass(frozen=True)
class Adaptation:
    """What this goal's work may learn about how the operator talks.
    ``enabled`` False (or no block) changes nothing anywhere."""
    enabled: bool = False
    patterns: Tuple[Pattern, ...] = ()
    # While a question from the system is open, these typed words could be
    # about it or about the item waiting. They do neither.
    answer_words: Tuple[str, ...] = ANSWER_WORDS
    # A phrase containing one of these is never offered as an alias.
    refuse_words: Tuple[str, ...] = REFUSE_WORDS
    forget: Tuple[str, ...] = ("forget",)
    buttons: Tuple[Tuple[str, str], ...] = (("yes", "Yes"), ("later", "Not now"))

    def alias_verbs(self) -> Tuple[str, ...]:
        if not self.enabled:
            return ()
        return tuple(dict.fromkeys(
            p.verb for p in self.patterns if p.becomes == BECOMES_ALIAS and p.verb))


@dataclass(frozen=True)
class TicketModel:
    """What a change to standard work would have cost as an engineering
    ticket, as the operator's own pricing model states it. Declared, with its
    source; read by reports to label an ESTIMATE. Decides nothing."""
    hours_per_ticket: float
    loaded_rate: float                      # dollars an hour, fully loaded
    source: str
    price_per_month: Optional[float] = None         # what a dock costs, dollars a month


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
    # Optional: a folder of further items that are NOT in the queue until
    # they are released into it (work that "arrives later" — a backlog).
    backlog: Optional[Path] = None
    # The backlog as STAGES, released one at a time, in order: (folder, label).
    # A single ``backlog: <folder>`` is one stage. Each stage is a period on
    # the goal's reports ("Month 2", "Month 3").
    backlog_stages: Tuple[Tuple[Path, str], ...] = ()
    # See SESSION_MEMORY_*.
    session_memory: str = SESSION_MEMORY_RECORDS_ONLY
    # How long one model call may go without answering before the attempt is
    # stopped and the request goes one tier up (the ladder rule). None: no
    # budget. Declared as ``call_budget_seconds``: one number of seconds for
    # every tier, or a mapping of tier to seconds (``default`` for the rest);
    # 0 switches it off.
    call_budget_seconds: Any = None
    # How a released backlog is worked (declared as ``batch:``). ``order``:
    # ``keg_first`` decides everything the keg covers at once and leaves the
    # rest for the end; ``item_order`` works the queue in its own order, the
    # keg deciding each run of items it covers and a model taking each item
    # between. ``hold_on_proposal``: whether the batch waits when Kaizen
    # proposes a change, until the operator signs it, sends it back or says
    # later.
    batch_order: str = BATCH_KEG_FIRST
    hold_on_proposal: bool = True
    adaptation: Adaptation = field(default_factory=Adaptation)
    ticket_model: Optional[TicketModel] = None

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
        confirmed_key = ev_raw.get("confirmed_key")
        ck_threshold = None
        if confirmed_key is not None:
            ck_threshold = (confirmed_key.get("threshold")
                            if isinstance(confirmed_key, Mapping) else None)
            if (not isinstance(ck_threshold, int) or isinstance(ck_threshold, bool)
                    or ck_threshold < 1):
                raise ValueError(
                    f"goal {goal.id!r}: evidence.confirmed_key needs a threshold of 1 or more")
        alias = ev_raw.get("alias")
        alias_n, alias_same, alias_names = None, (), ()
        if alias is not None:
            where = f"goal {goal.id!r}: evidence.alias"
            if not isinstance(alias, Mapping):
                raise ValueError(f"{where} must be a mapping")
            alias_n = alias.get("confirmations")
            if not isinstance(alias_n, int) or isinstance(alias_n, bool) or alias_n < 1:
                raise ValueError(f"{where} needs confirmations of 1 or more")

            def _named(key: str) -> Tuple[str, ...]:
                names = alias.get(key) or ()
                if not isinstance(names, (list, tuple)) or not all(
                        isinstance(n, str) and n in inputs for n in names):
                    raise ValueError(f"{where}.{key} must list declared inputs")
                return tuple(names)

            alias_same, alias_names = _named("same"), _named("names")
            if not (alias_same or alias_names):
                raise ValueError(
                    f"{where} needs an identity match: an input under 'same' or 'names'")
        evidence = EvidenceRule(threshold=threshold, scope=scope,
                                confirmed_key_threshold=ck_threshold,
                                alias_confirmations=alias_n, alias_same=alias_same,
                                alias_names=alias_names)

    isolation = raw.get("isolation")
    if isolation not in (None, ISOLATION_SOURCES_ONLY):
        raise ValueError(f"goal {goal.id!r}: unknown isolation {isolation!r}")
    on_unclean = raw.get("on_unclean")
    if on_unclean not in (None, ON_UNCLEAN_OPEN_CLEAN):
        raise ValueError(f"goal {goal.id!r}: unknown on_unclean {on_unclean!r}")

    session_memory = raw.get("session_memory")
    if session_memory is None:
        session_memory = (SESSION_MEMORY_RECORDS_ONLY if isolation == ISOLATION_SOURCES_ONLY
                          else SESSION_MEMORY_COMPACT)
    if session_memory not in (SESSION_MEMORY_RECORDS_ONLY, SESSION_MEMORY_COMPACT):
        raise ValueError(
            f"goal {goal.id!r}: session_memory must be "
            f"{SESSION_MEMORY_RECORDS_ONLY!r} or {SESSION_MEMORY_COMPACT!r}")

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

    adaptation = _load_adaptation(raw.get("adaptation"), str(goal.id), session)
    stages = _load_backlog(raw.get("backlog"), str(goal.id), root, session)

    resolved = getattr(goal, "resolved_sources", None)
    cfg = DecisionWorkConfig(
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
        backlog=(stages[0][0] if stages else None),
        backlog_stages=stages,
        session_memory=session_memory,
        call_budget_seconds=_call_budget(raw.get("call_budget_seconds", CALL_BUDGET_DEFAULT),
                                         str(goal.id)),
        **_load_batch(raw.get("batch"), str(goal.id)),
        adaptation=adaptation,
        ticket_model=_load_ticket_model(raw.get("ticket_model"), str(goal.id)),
    )
    return _with_learned(cfg)


def _load_backlog(raw: Any, goal_id: str, root: Path,
                  session: WorkSession) -> Tuple[Tuple[Path, str], ...]:
    """The goal's backlog as stages. ``backlog: <folder>`` is one stage named
    by the work session's batch label; a list gives each stage its folder and
    its own label, in the order they are released."""
    if not raw:
        return ()
    if isinstance(raw, str):
        return ((_resolve(root, raw), session.batch_label),)
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"goal {goal_id!r}: backlog must be a folder or a list of stages")
    stages = []
    for entry in raw:
        if (not isinstance(entry, Mapping) or not isinstance(entry.get("folder"), str)
                or not isinstance(entry.get("label"), str) or not entry["label"].strip()):
            raise ValueError(
                f"goal {goal_id!r}: each backlog stage needs a folder and a label")
        stages.append((_resolve(root, entry["folder"]), entry["label"].strip()))
    if len({label for _folder, label in stages}) != len(stages):
        raise ValueError(f"goal {goal_id!r}: backlog stage labels must be different")
    return tuple(stages)


def _load_ticket_model(raw: Any, goal_id: str) -> Optional[TicketModel]:
    if raw is None:
        return None
    where = f"goal {goal_id!r}: ticket_model"
    if not isinstance(raw, Mapping):
        raise ValueError(f"{where} must be a mapping")

    def _number(key: str, required: bool = True) -> Optional[float]:
        value = raw.get(key)
        if value is None and not required:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f"{where}.{key} must be a number above zero")
        return float(value)

    source = raw.get("source")
    if not isinstance(source, str) or not source.strip():
        raise ValueError(f"{where}.source must say where the figures come from")
    return TicketModel(
        hours_per_ticket=_number("hours_per_ticket"), loaded_rate=_number("loaded_rate"),
        source=source.strip(),
        price_per_month=_number("dock_price", required=False))


def _load_adaptation(raw: Any, goal_id: str, session: WorkSession) -> Adaptation:
    """The goal's ``adaptation`` block. Malformed is refused, as everything
    else in the declaration is."""
    if raw is None:
        return Adaptation()
    if not isinstance(raw, Mapping):
        raise ValueError(f"goal {goal_id!r}: adaptation must be a mapping")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError(f"goal {goal_id!r}: adaptation.enabled must be true or false")

    def _words(key: str, default: Tuple[str, ...]) -> Tuple[str, ...]:
        value = raw.get(key)
        if value is None:
            return default
        if not isinstance(value, (list, tuple)) or not all(
                isinstance(v, str) and v.strip() for v in value):
            raise ValueError(
                f"goal {goal_id!r}: adaptation.{key} must be a list of words "
                f"(quote each one: a bare yes or no is read as true or false)")
        return tuple(v.strip() for v in value)

    patterns = []
    listed = raw.get("patterns") or ()
    if not isinstance(listed, (list, tuple)):
        raise ValueError(f"goal {goal_id!r}: adaptation.patterns must be a list")
    for entry in listed:
        if not isinstance(entry, Mapping) or not str(entry.get("id") or "").strip():
            raise ValueError(f"goal {goal_id!r}: each adaptation pattern needs an id")
        pid, counts = str(entry["id"]).strip(), entry.get("counts")
        where = f"goal {goal_id!r}: adaptation pattern {pid!r}"
        if counts not in PATTERN_OUTCOMES:
            raise ValueError(f"{where} counts {counts!r}; it can count "
                             f"{sorted(PATTERN_OUTCOMES)}")
        becomes = entry.get("becomes", PATTERN_OUTCOMES[counts])
        if becomes != PATTERN_OUTCOMES[counts]:
            raise ValueError(f"{where}: {counts} can only become "
                             f"{PATTERN_OUTCOMES[counts]!r}, not {becomes!r}")
        threshold = entry.get("threshold")
        if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 1:
            raise ValueError(f"{where} needs a threshold of 1 or more")
        verb, propose = entry.get("verb"), entry.get("propose", becomes == BECOMES_ALIAS)
        if not isinstance(propose, bool):
            raise ValueError(f"{where}: propose must be true or false")
        if becomes == BECOMES_ALIAS:
            if verb not in READABLE_VERBS:
                raise ValueError(
                    f"{where}: an alias needs a verb whose reading leaves a record "
                    f"to count; today that is {sorted(READABLE_VERBS)}, not {verb!r}")
        elif propose:
            raise ValueError(
                f"{where}: {becomes} is counted but not yet proposed; set propose: false")
        max_words = entry.get("max_words", Pattern.max_words)
        if not isinstance(max_words, int) or isinstance(max_words, bool) or max_words < 1:
            raise ValueError(f"{where}: max_words must be 1 or more")
        patterns.append(Pattern(
            id=pid, counts=str(counts), threshold=threshold, becomes=str(becomes),
            verb=str(verb) if verb else None, propose=propose, max_words=max_words))
    if len({p.id for p in patterns}) != len(patterns):
        raise ValueError(f"goal {goal_id!r}: adaptation pattern ids must be unique")
    if enabled and not session.enabled:
        raise ValueError(
            f"goal {goal_id!r}: adaptation needs an enabled work_session to learn in")
    return Adaptation(
        enabled=enabled, patterns=tuple(patterns),
        answer_words=_words("answer_words", ANSWER_WORDS),
        refuse_words=_words("refuse_words", REFUSE_WORDS),
        forget=_words("forget", ("forget",)),
    )


def _with_learned(cfg: "DecisionWorkConfig") -> "DecisionWorkConfig":
    """Merge the phrases the operator approved in conversation into the work
    session's verbs. Honored ONLY while all of these hold: adaptation is on,
    the phrase's verb is one the session rule lets the vocabulary supply, and
    the operator's signature on that rule stands. Otherwise the vocabulary
    file is ignored — a phrase can never act under a rule nobody signed."""
    verbs = cfg.adaptation.alias_verbs()
    if not verbs:
        return cfg
    from grove import adaptation as lane

    learned = lane.load(cfg.goal_id)
    if not learned:
        return cfg
    unsigned = sorted(v for v in learned if v not in verbs)
    if unsigned:
        raise ValueError(
            f"goal {cfg.goal_id!r}: the vocabulary names {unsigned}, which the "
            f"session rule does not let it supply phrases for ({sorted(verbs)})")
    if session_rule_grant(cfg) is None:
        return cfg
    from dataclasses import replace

    pairs = tuple((verb, phrase) for verb in verbs for phrase in learned.get(verb, ()))
    merged = {verb: tuple(dict.fromkeys(
        (*getattr(cfg.work_session, verb), *learned.get(verb, ())))) for verb in verbs}
    return replace(cfg, work_session=replace(cfg.work_session, learned=pairs, **merged))


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


# A press on a question card (not an item card): the answer and the id of the
# proposal the card was about.
ALIAS_PRESS = re.compile(r"alias\s+(yes|later)\s+#(\S+)", re.IGNORECASE)
# "Later" on a proposal card: carry on with the work; the proposal stays waiting.
PROPOSAL_PRESS = re.compile(r"proposal\s+later\s+#(\S+)", re.IGNORECASE)


def proposal_message(proposal_id: str) -> str:
    return f"proposal later #{proposal_id.split(':')[-1][:12]}"


def alias_message(answer: str, proposal_id: str) -> str:
    return f"alias {answer} #{proposal_id}"


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
        learned = set(ws.learned)

        def _declared(verb: str) -> List[str]:
            return [p for p in getattr(ws, verb) if (verb, p) not in learned]

        rule["work_session"] = {
            "start": _declared("start"), "confirm": _declared("confirm"),
            "revise": _declared("revise"), "pause": _declared("pause"),
            "after_a_decision": "present_the_next_item",
        }
        if ws.batch:
            rule["work_session"]["batch"] = list(ws.batch)
        verbs = cfg.adaptation.alias_verbs()
        if verbs:
            # The one line that lets the goal's vocabulary supply further
            # phrases for verbs ALREADY in this rule. Present only while
            # adaptation is on, so switching it off leaves the rule as signed.
            rule["work_session"]["vocabulary"] = {
                "verbs": list(verbs), "match": "exact",
                "added_by": "the operator, in conversation",
            }
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


# ── what a session's content becomes ──────────────────────────────────


def goals_worked_in(session_id: str, *, directory: Optional[Path] = None) -> List[str]:
    """The goals a session recorded decisions for, read off the decision
    logs. This — not the isolation latch, which a pause or a reset clears —
    is the lasting record that a session did a goal's work."""
    base = Path(directory) if directory is not None else default_decisions_dir()
    if not base.is_dir():
        return []
    needle = f'"session_id": "{session_id}"'
    out = []
    for path in sorted(base.glob("*.jsonl")):
        try:
            if needle in path.read_text(encoding="utf-8"):
                out.append(path.stem)
        except OSError:
            continue
    return out


def _turns(transcript: List[Mapping[str, Any]]) -> List[List[Mapping[str, Any]]]:
    """A transcript as turns: each begins at an operator message and runs to
    the next. Anything before the first operator message is dropped."""
    turns: List[List[Mapping[str, Any]]] = []
    for message in transcript:
        if not isinstance(message, Mapping):
            continue
        if message.get("role") == "user":
            turns.append([message])
        elif turns:
            turns[-1].append(message)
    return turns


def _said(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, list):
        content = " ".join(
            str(p.get("text", "")) for p in content if isinstance(p, Mapping))
    return " ".join(str(content or "").casefold().split())[:40]


def conversation_only(
    session_id: str, transcript: List[Mapping[str, Any]],
    intent_rows: List[Mapping[str, Any]], *, dock: Any = None,
    directory: Optional[Path] = None,
) -> Tuple[List[Mapping[str, Any]], Dict[str, Any]]:
    """A session's transcript with a goal's WORK turns taken out, leaving only
    what was said outside the work — and a report of what was done.

    A session that worked a ``records_only`` goal keeps its knowledge in that
    goal's records; its work turns must never reach a summarizer. Which turns
    were work is read off each turn's own record (``goal_session`` in its
    telemetry). A turn is matched to its record by the operator's words, in
    order. When the two cannot be matched — or the session's records predate
    the marker — NOTHING is returned: leaving a work turn in is the failure
    to avoid, so when in doubt the whole session stays out.

    A session that worked no such goal is returned unchanged."""
    goals = goals_worked_in(session_id, directory=directory)
    held: List[str] = []
    for goal_id in goals:
        try:
            cfg = config_for_goal(goal_id, dock=dock)
        except ValueError:
            held.append(goal_id)       # no longer declared: keep its work out
            continue
        if cfg.session_memory == SESSION_MEMORY_RECORDS_ONLY:
            held.append(goal_id)
    turns = _turns(transcript)
    report: Dict[str, Any] = {"goals": held, "turns": len(turns), "work_turns": 0,
                              "kept_turns": len(turns), "reason": None}
    if not held:
        return list(transcript), report

    latest: Dict[int, Mapping[str, Any]] = {}
    for row in intent_rows:
        if not isinstance(row, Mapping):
            # The intent store hands back record objects; read them as rows.
            import dataclasses
            row = dataclasses.asdict(row) if dataclasses.is_dataclass(row) else vars(row)
        if row.get("session_id") != session_id:
            continue
        try:
            ordinal = int(str(row.get("turn_id", "")).rsplit("#", 1)[1])
        except (IndexError, ValueError):
            continue
        latest[ordinal] = row
    records = [latest[k] for k in sorted(latest)]
    marked = any(
        "goal_session" in ((r.get("stages") or {}).get("telemetry") or {}) for r in records)
    if not marked:
        report.update(work_turns=len(turns), kept_turns=0,
                      reason="its turn records predate the work marker")
        return [], report

    kept: List[Mapping[str, Any]] = []
    cursor, last = 0, None
    for turn in turns:
        said = _said(turn[0])
        found = None
        for k in range(cursor, min(cursor + 3, len(records))):
            if " ".join(str(records[k].get("user_message_stem") or "").casefold().split())[:40] == said:
                found = k
                break
        if found is not None:
            telemetry = (records[found].get("stages") or {}).get("telemetry") or {}
            is_work = bool(telemetry.get("goal_session"))
            cursor, last = found + 1, (said, is_work)
        elif last is not None and last[0] == said:
            is_work = last[1]          # the same exchange, saved twice
        else:
            report.update(work_turns=len(turns), kept_turns=0,
                          reason="its transcript could not be matched to its turn records")
            return [], report
        if is_work:
            report["work_turns"] += 1
        else:
            kept.extend(turn)
    report["kept_turns"] = len(turns) - report["work_turns"]
    return kept, report


def _operator_said(provenance: Optional[Mapping[str, Any]],
                   cfg: "DecisionWorkConfig") -> Optional[str]:
    """The operator's own words behind a decision, when they said something
    of substance: an explanation with a revision, an answer to a question.
    Not kept when the turn ran with no model (a button, an exact phrase) or
    when the message only asked for the work — those carry no reasoning."""
    prov = provenance or {}
    said = str(prov.get("request") or "").strip()
    if not said or prov.get("tier") == "T0" or prov.get("session_step"):
        return None
    if BUTTON_PRESS.fullmatch(said) or asks_for_work(said, cfg):
        return None
    ws = cfg.work_session
    if ws.enabled and (says(said, ws.confirm) or says(said, ws.revise)):
        return None
    return said[:500]


# ── starting over, and work that arrives later ────────────────────────


def _quoted(value: Any) -> str:
    """A value as a keg condition writes it (single-quoted)."""
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _stages(cfg: "DecisionWorkConfig") -> Tuple[Tuple[Path, str], ...]:
    """The goal's backlog stages; a bare ``backlog`` folder is one stage."""
    if cfg.backlog_stages:
        return cfg.backlog_stages
    if cfg.backlog is not None:
        return ((cfg.backlog, cfg.work_session.batch_label),)
    return ()


def _stage_files(folder: Path) -> set:
    if not folder.is_dir():
        return set()
    return {p.name for p in folder.iterdir() if p.is_file() and not p.name.startswith(".")}


def backlog_state(cfg: "DecisionWorkConfig") -> Dict[str, Any]:
    """Where the goal's backlog stands: how many items it holds and how many
    of those are in the queue now — in all, and stage by stage when the
    backlog is staged."""
    stages = _stage_states(cfg)
    out: Dict[str, Any] = {
        "declared": bool(stages),
        "items": sum(s["items"] for s in stages),
        "released": sum(s["released"] for s in stages),
    }
    if len(stages) > 1:
        out["stages"] = stages         # only when the backlog is staged
    return out


def _stage_states(cfg: "DecisionWorkConfig") -> List[Dict[str, Any]]:
    queued = ({p.name for p in cfg.queue.iterdir() if p.is_file()}
              if cfg.queue.is_dir() else set())
    out = []
    for index, (folder, label) in enumerate(_stages(cfg)):
        names = _stage_files(folder)
        out.append({"index": index, "label": label, "items": len(names),
                    "released": len(names & queued)})
    return out


def next_backlog_stage(cfg: "DecisionWorkConfig") -> Optional[Dict[str, Any]]:
    """The first backlog stage not yet fully in the queue — the one a release
    would put there — or None when every stage is."""
    for stage in _stage_states(cfg):
        if stage["items"] and stage["released"] < stage["items"]:
            return stage
    return None


def release_backlog(cfg: "DecisionWorkConfig") -> int:
    """Put the NEXT backlog stage's items into the queue (copies; the backlog
    keeps its own). One stage at a time, in the declared order. Returns how
    many were added; 0 when every stage is already in the queue."""
    import shutil

    stages = _stages(cfg)
    if not stages or not any(f.is_dir() for f, _ in stages):
        raise ValueError(f"goal {cfg.goal_id!r} declares no backlog folder")
    upcoming = next_backlog_stage(cfg)
    if upcoming is None:
        return 0
    folder = stages[upcoming["index"]][0]
    added = 0
    for path in sorted(folder.iterdir()):
        target = cfg.queue / path.name
        if path.is_file() and not path.name.startswith(".") and not target.exists():
            shutil.copy2(path, target)
            added += 1
    if added and cfg.work_session.enabled:
        from grove import reissue
        reissue.note_goal(cfg.goal_id, "backlog_released")
    if added:
        # When the stage arrived is part of the record: it is where "release
        # to last item done" is measured from.
        from grove.kaizen_ledger import KaizenLedger
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        KaizenLedger(f"operator-{stamp}").record(
            "operator_applied", action="backlog_released", goal=cfg.goal_id,
            applied_by="operator", stage=upcoming["label"], items=added)
    return added


def withhold_backlog(cfg: "DecisionWorkConfig") -> int:
    """Take the backlog's items back out of the queue, every stage: only
    files a backlog folder itself holds, never anything else. Returns how
    many were removed."""
    removed = 0
    for folder, _label in _stages(cfg):
        for name in _stage_files(folder):
            target = cfg.queue / name
            if target.is_file():
                target.unlink()
                removed += 1
    return removed


def reset_work(cfg: "DecisionWorkConfig", label: str = "", *,
               surface: str = "cli", apply: bool = True) -> Dict[str, Any]:
    """Start a goal's decision work over, without deleting any record.

    Opens a new, clearly marked RUN (the queue starts again at its first
    item, and everything that reads "the current run" stops seeing earlier
    decisions); retires the previous run's standard work for the goal (a keg
    drafted, serving or halted is revoked and stays on record as revoked; a
    keg proposal still waiting is withdrawn with a recorded disposition); and
    takes any released backlog back out of the queue. Earlier records and the
    audit trail are never touched. The reset itself goes on the ledger.

    ``apply`` False returns what WOULD happen and writes nothing."""
    from grove import keg as keg_mod
    from grove.eval import proposal_queue
    from grove.pattern_cache import (
        PatternCacheStore, STATUS_ACTIVE, STATUS_DEMOTED, STATUS_HALTED,
        STATUS_SUSPENDED,
    )

    work = DecisionWork(cfg)
    run = work.log.current_run()
    store = PatternCacheStore()
    kegs = [
        e for e in store.all()
        if (keg_mod.keg_record(e).get("keg") or {}).get("dock_goal") == cfg.goal_id
        and e.status in (STATUS_ACTIVE, STATUS_HALTED, STATUS_SUSPENDED)
    ]
    waiting = [
        p for p in proposal_queue.read_all()
        if ((p.payload or {}).get("keg") or {}).get("dock_goal") == cfg.goal_id
    ]
    out: Dict[str, Any] = {
        "goal": cfg.goal_id, "applied": bool(apply),
        "previous_run": (run or {}).get("run_number"),
        "previous_label": (run or {}).get("label") or "",
        "decisions": sum(1 for r in work.log.run_records() if r.get("kind") == KIND_PROPOSED),
        "queued": len(work.queue_items()),
        "kegs_revoked": [(e.pattern_id, e.status) for e in kegs],
        "proposals_withdrawn": len(waiting),
        "backlog_removed": backlog_state(cfg)["released"],
    }
    if not apply:
        return out
    from grove.flywheel_cli import _record_kaizen_disposition
    from grove.kaizen_ledger import KaizenLedger

    for proposal in waiting:
        proposal_queue.remove(proposal.proposal_id)
        _record_kaizen_disposition(
            proposal, disposition="withdrawn", reason=label or "demo reset")
    for entry in kegs:
        store.set_status(entry.pattern_id, STATUS_DEMOTED)
    out["backlog_removed"] = withhold_backlog(cfg)
    from grove import adaptation as lane
    out["vocabulary"] = lane.reset(cfg, surface=surface)
    from grove import reissue
    reissue.goal_note(cfg.goal_id, take=True)      # a fresh run owes no keg pass
    new = work.log.start_run(label or "demo reset")
    out["run"] = new["run_number"]
    out["label"] = new["label"]
    first = work.next_item()
    out["first_item"] = first.stem if first is not None else None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    KaizenLedger(f"operator-{stamp}").record(
        "operator_applied",
        action="work_reset", goal=cfg.goal_id, applied_by="operator",
        approval_surface=surface, run_opened=new["run_number"], label=new["label"],
        previous_run=out["previous_run"],
        kegs_revoked=[p for p, _ in out["kegs_revoked"]],
        proposals_withdrawn=out["proposals_withdrawn"],
        backlog_removed=out["backlog_removed"],
    )
    return out


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


CALL_BUDGET_DEFAULT = 30


def _load_batch(raw: Any, goal_id: str) -> Dict[str, Any]:
    """The goal's ``batch:`` block. Absent: today's behavior (keg first, hold
    on a proposal). A value the block does not know is refused, never guessed."""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping) or set(raw) - {"order", "hold_on_proposal"}:
        raise ValueError(
            f"goal {goal_id!r}: batch must be a mapping with order and/or hold_on_proposal")
    order, hold = raw.get("order", BATCH_KEG_FIRST), raw.get("hold_on_proposal", True)
    if order not in (BATCH_KEG_FIRST, BATCH_ITEM_ORDER):
        raise ValueError(
            f"goal {goal_id!r}: batch.order must be {BATCH_KEG_FIRST!r} or "
            f"{BATCH_ITEM_ORDER!r}, got {order!r}")
    if not isinstance(hold, bool):
        raise ValueError(f"goal {goal_id!r}: batch.hold_on_proposal must be true or false")
    return {"batch_order": order, "hold_on_proposal": hold}


def _call_budget(raw: Any, goal_id: str) -> Any:
    """The declared time budget for one model call: seconds for every tier,
    or ``{tier: seconds, "default": seconds}``. 0, false or null switch it
    off (for the goal, or for one tier). Anything else is refused: a budget is
    never guessed."""
    def _seconds(value: Any) -> Optional[float]:
        if value is None or value is False or (not isinstance(value, bool) and value == 0):
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError(
                f"goal {goal_id!r}: call_budget_seconds must be a number of seconds "
                f"(0 switches it off), or a mapping of tier to seconds; got {raw!r}")
        return float(value)

    if isinstance(raw, Mapping):
        budgets = {str(tier): _seconds(value) for tier, value in raw.items()}
        return budgets if any(v for v in budgets.values()) else None
    return _seconds(raw)


def call_budget_for(budget: Any, tier: Any) -> Optional[float]:
    """The seconds one model call may take on ``tier`` under a declared
    budget (see :func:`_call_budget`), or None for no budget."""
    if isinstance(budget, Mapping):
        return budget.get(str(tier)) if str(tier) in budget else budget.get("default")
    return budget


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
                         "attempts": list(prov.get("attempts") or []),
                         # True when this request was already re-issued into a
                         # clean session: it is not re-issued a second time.
                         "reissued_clean": bool(prov.get("reissued_clean"))},
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

    def waits(self, message: Any) -> bool:
        """Whether a message that arrives while the next item is on its way
        is an answer about the vocabulary (a press on a question card, a
        phrase taken back). It is not a change of subject: it waits its turn
        and the session goes on."""
        if (self.config.work_session.enabled
                and PROPOSAL_PRESS.fullmatch(str(message or "").strip())):
            return True
        learning = getattr(self.config, "adaptation", None)
        if not (self.config.work_session.enabled and learning and learning.enabled):
            return False
        from grove import adaptation as lane
        action = lane.session_action(self, message) or {}
        return action.get("action") in ("alias_yes", "alias_later", "forget", "hold")

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
            from grove import reissue
            from grove.eval.proposal_queue import read_all
            carded = {h["proposal_id"] for h in reissue.holds()}
            for proposal in read_all():
                keg = (proposal.payload or {}).get("keg") or {}
                if proposal.proposal_id in carded:
                    continue          # on its own card, with its own buttons
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
        if prov.get("session_id"):
            # An answer is on its way to the record: whatever question a model
            # had open in this session has been answered.
            from grove import reissue
            reissue.clear_turn_note(str(prov["session_id"]))
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
            **({"batch": batch} if (batch := self.batch_for(item_id)) else {}),
            # What the operator said that led to this answer (an answer to a
            # question the model asked), in their words.
            **({"operator_said": said} if (said := _operator_said(prov, self.config)) else {}),
        })

    def _stage_of(self, item_id: Any) -> Optional[int]:
        """The backlog stage an item arrived in (its index), or None."""
        for index, (folder, _label) in enumerate(_stages(self.config)):
            if folder.is_dir() and any(p.stem == str(item_id) for p in folder.iterdir()
                                       if p.is_file()):
                return index
        return None

    def batch_for(self, item_id: Any, mint: bool = True) -> Optional[str]:
        """The batch an item's record carries. Keg first: the batch the run is
        in. Item order: one batch per backlog stage, whoever decides the item
        and in whatever order — the id already on a record of the same stage,
        else (``mint``) a new one. An item of no stage belongs to no batch."""
        if self.config.batch_order != BATCH_ITEM_ORDER:
            return self.current_batch()
        stage = self._stage_of(item_id)
        if stage is None:
            return None
        for record in self.log.run_records():
            if (record.get("kind") == KIND_PROPOSED and record.get("batch")
                    and self._stage_of(record.get("item_id")) == stage):
                return str(record["batch"])
        return uuid.uuid4().hex if mint else None

    def _for_model(self) -> List[str]:
        """Items the last batch segment found the serving keg does not
        answer, when that keg is still the one serving. Empty otherwise."""
        from grove import reissue
        note = str(reissue.goal_note(self.config.goal_id) or "")
        if not note.startswith(_NEXT_FOR_MODEL):
            return []
        version, _, items = note[len(_NEXT_FOR_MODEL):].partition(":")
        serving = self.serving_keg()
        if serving is None or str(serving[1].get("version")) != version:
            return []            # a new version is serving: it gets to look again
        return [i for i in items.split(",") if i]

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
        new_stage: bool = True,
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
        in_order = self.config.batch_order == BATCH_ITEM_ORDER
        # One batch per backlog stage. A further keg pass inside the same stage
        # (after a new rule is signed, say) continues that stage's batch; only
        # a newly released stage begins another.
        batch_id = (None if in_order else
                    (None if new_stage else self.current_batch()) or uuid.uuid4().hex)
        out["batch"] = batch_id
        stage_batches: Dict[Any, Optional[str]] = {}
        for position, path in enumerate(todo):
            try:
                inputs = dict(inputs_for(path))
                output = keg_mod.evaluate(spec, inputs)
            except (ValueError, OSError):
                output = None     # unreadable: the ordinary loop surfaces it
            if output is None:
                if not in_order:
                    continue
                # Item order: the run of items the keg covers ends here. This
                # item, and any straight after it the keg does not answer
                # either, each go to a model, in order.
                waiting = []
                for later in todo[position:]:
                    try:
                        if keg_mod.evaluate(spec, dict(inputs_for(later))) is not None:
                            break
                    except (ValueError, OSError):
                        pass
                    waiting.append(later.stem)
                from grove import reissue
                reissue.note_goal(self.config.goal_id, _NEXT_FOR_MODEL
                                  + f"{keg_ref.get('version')}:" + ",".join(waiting))
                out["for_model"] = waiting
                break
            if in_order:
                stage = self._stage_of(path.stem)
                if stage not in stage_batches:
                    stage_batches[stage] = self.batch_for(path.stem)
                batch_id = out["batch"] = stage_batches[stage]
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
        if not out["coded"] and not in_order:
            # Nothing covered: no batch began, so nothing later is labeled one.
            out["batch"] = None
        return out

    def rule_on(
        self, item_id: str, *, decision: str,
        corrected_output: Optional[Mapping[str, Any]] = None,
        provenance: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """The operator's LATER ruling on an item that is already decided.

        The operator can always change a call. An item the keg accepted
        without review can be confirmed (it becomes confirmed) or revised; an
        item the operator already confirmed or revised can be revised again.
        The earlier records stay; the new one is appended and is the item's
        standing decision. A revision is seen by Jidoka exactly as any
        correction is — when a keg produced the answer, that is a miss and
        the keg halts."""
        if decision not in (DECISION_CONFIRM, DECISION_CORRECT):
            raise DecisionRefused(
                "unknown_decision", "The decision must be 'confirm' or 'correct'.")
        proposed, decided = self._state()
        record = proposed.get(item_id)
        verdict = decided.get(record["id"]) if record else None
        if record is None or verdict is None:
            raise DecisionRefused("not_decided", f"{item_id} has not been decided.")
        standing = dict(verdict.get("output") or record["output"])
        if decision == DECISION_CONFIRM:
            if verdict.get("decision") != DECISION_ACCEPTED:
                raise DecisionRefused(
                    "already_ruled",
                    f"{item_id} is already decided as {self.value_text(standing)}. "
                    f"To change it, give the value it should be.")
            final = dict(record["output"])
        else:
            if not corrected_output:
                raise DecisionRefused(
                    "missing_correction", "A revision needs the revised value.")
            self.check_output(corrected_output, provenance)
            final = {k: str(v).strip() for k, v in corrected_output.items()}
            if final == standing:
                raise DecisionRefused(
                    "correction_matches",
                    f"{item_id} is already decided as {self.value_text(standing)}.")
        prov = dict(provenance or {})
        ruled = self.log.append({
            "kind": KIND_DECIDED, "run_id": record["run_id"], "ref": record["id"],
            "item_id": item_id, "decision": decision, "output": final,
            "after": verdict.get("decision"), "session_id": prov.get("session_id"),
            "turn_id": prov.get("turn_id"), "turn_uid": prov.get("turn_uid"),
            **({"operator_said": said} if (said := _operator_said(prov, self.config)) else {}),
        })
        self._observe(record, ruled)
        self._hold_for_signature(prov)
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
            # The operator's own words with this ruling, when they gave any.
            **({"operator_said": said} if (said := _operator_said(prov, self.config)) else {}),
        })
        self._observe(waiting, decided)
        self._hold_for_signature(prov)
        self.present_next_after(decided, prov)
        return decided

    # -- the work session -------------------------------------------------

    def _hold_for_signature(self, provenance: Mapping[str, Any]) -> int:
        """Kaizen answered this decision with a change to standard work. Put
        it in front of the operator as its own card — never a line inside
        another reply — and hold the session after the item in hand until they
        sign it, send it back, or say later. A batch already running is one
        turn and is not interrupted. Returns how many cards were offered."""
        self.cards_offered = 0
        cfg = self.config
        session_id = provenance.get("session_id")
        if not (cfg.work_session.enabled and session_id
                and provenance.get("isolation_goal") == cfg.goal_id):
            return 0
        from grove import reissue

        many = cfg.item_name[1]
        for event in self.last_observations:
            answer = event.get("answer") or {}
            if answer.get("kind") != "standard_work" or not answer.get("artifact"):
                continue
            pid = str(answer["artifact"])
            detail = answer.get("detail") or {}
            what = f"keg v{detail['version']}" if detail.get("version") else "a change"
            replay = (
                f"Replayed on {detail['replayed']} {many}: {detail.get('unchanged')} unchanged, "
                f"{detail.get('would_change')} would change, {detail.get('not_covered')} "
                f"not covered.\n" if detail.get("replayed") is not None else "")
            short = pid.split(":")[-1][:12]
            reissue.offer_card(str(session_id), {
                "proposal_id": pid,
                "text": (f"Kaizen proposed {what}. It needs your signature.\n{replay}"
                         + ("The work pauses here until you sign it, send it back, or tap "
                            "Later." if cfg.hold_on_proposal else
                            "The work carries on; it waits under To sign.")),
                "buttons": [
                    {"label": "Review and sign",
                     "url": _portal(f"proposals/pending?type=signature&at=proposal-{short}")},
                    ["Later", proposal_message(pid)],
                ],
            })
            if cfg.hold_on_proposal:
                reissue.hold(str(session_id),
                             {"goal": cfg.goal_id, "proposal_id": pid, "what": what})
            self.cards_offered += 1
        return self.cards_offered

    @staticmethod
    def _proposal_waiting(proposal_id: Any) -> bool:
        from grove.eval.proposal_queue import read
        return read(str(proposal_id)) is not None

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

        if reissue.held(str(session_id)):
            return False          # held for the operator's signature: nothing is brought
        reissue.arm({
            "request": cfg.keg.request, "authorized": getattr(grant, "id", None),
            "turn_uid": provenance.get("turn_uid"),
            "goal": cfg.goal_id, "advance": True,
        }, session_id=str(session_id))
        self.next_armed = True
        return True

    BACKLOG_FIRST_MESSAGE = "Starting on the backlog. The keg goes first."

    def backlog_first(self, provenance: Optional[Mapping[str, Any]]) -> bool:
        """The backlog has just been released and a MODEL turn is about to
        take its first item — the operator asked for it in words no declared
        phrase matched. Standard work goes first: instead of handing the model
        an item, arm the goal's own request, so the next turn is the keg pass.
        Returns whether it did. However the operator words it, the floor is
        laid before they meet an exception."""
        prov = provenance or {}
        if not self.config.work_session.enabled or self.pending() is not None:
            return False
        from grove import reissue
        if reissue.goal_note(self.config.goal_id) != "backlog_released":
            return False
        if self.serving_keg() is None:
            return False
        return self.present_next_after({"id": "backlog"}, dict(prov))

    def session_action(self, message: Any, *,
                       session_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
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
        action = self._held_action(message, session_id) or self._session_action(message)
        if action is None and session_id and self.pending() is None and (
                asks_for_work(str(message or ""), self.config)
                or routes(message, ws.batch, self.config)):
            # The work was asked for while a model's question about the next
            # item is still open. There is nothing to present until it is
            # answered: say the question again, with no model.
            from grove import reissue
            question = reissue.open_question(str(session_id))
            if question:
                action = {"action": "ask_again", "question": question}
        self.last_match = self.match_trace(message, action)
        return action

    def _held_action(self, message: Any, session_id: Optional[str]) -> Optional[Dict[str, Any]]:
        """What a message is while the session is held for a signature: a
        press on the proposal card's Later, or a request for the work, which
        is answered with what it is waiting on. A hold whose proposal has been
        signed or sent back in the meantime is lifted here, and the message is
        read as usual."""
        pressed = PROPOSAL_PRESS.fullmatch(str(message or "").strip())
        if pressed:
            return {"action": "proposal_later", "proposal": pressed.group(1), "button": True}
        if not session_id:
            return None
        from grove import reissue

        hold = reissue.held(str(session_id))
        if not hold or hold.get("goal") != self.config.goal_id:
            return None
        if not self._proposal_waiting(hold["proposal_id"]):
            reissue.release_hold(str(session_id))
            return None
        if self.pending() is None and (
                asks_for_work(str(message or ""), self.config)
                or routes(message, self.config.work_session.batch, self.config)):
            return {"action": "hold_signature", "what": hold.get("what"),
                    "proposal": str(hold["proposal_id"])}
        return None

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
                      "exact" if action.get("action") in (
                          "confirm", "revise", "revise_prompt", "hold", "forget",
                          "hold_signature")
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
        if self.config.adaptation.enabled:
            from grove import adaptation as lane
            learning = lane.session_action(self, message)
            if learning is not None:
                return learning
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
        if asks_for_work(str(message or ""), self.config):
            if self.next_item() is None:
                return {"action": "summary"}
            from grove import reissue
            upcoming = self.next_item()
            if (self.config.batch_order == BATCH_ITEM_ORDER
                    and self._stage_of(upcoming.stem) is not None
                    and reissue.goal_note(self.config.goal_id) != "backlog_released"):
                # A backlog worked in item order: the keg takes each run of
                # items it covers; an item the last pass found it does not
                # answer goes to a model (None: the turn is routed as usual).
                if upcoming.stem in self._for_model():
                    return None
                return {"action": "batch"}
            if reissue.goal_note(self.config.goal_id) == "backlog_released":
                # The backlog just arrived. The first request for the work
                # after that runs the keg pass: everything standard work
                # covers is decided at once, and the operator meets only the
                # exceptions. Once only — the note is consumed by the pass.
                return {"action": "batch"}
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
            if answer.get("kind") == "standard_work" and getattr(self, "cards_offered", 0):
                pass      # on its own card, with its own buttons
            elif answer.get("kind") == "standard_work":
                version = (answer.get("detail") or {}).get("version")
                lines.append(
                    "Kaizen proposed " + (f"keg v{version}" if version else "a change")
                    + ". Review in portal: " + _portal("proposals/pending"))
            elif answer.get("write_class") == "vocabulary_alias":
                pass      # asked on its own card, with its own buttons
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
        if kind in ("proposal_later", "hold_signature"):
            return self._signature_step(action, provenance)
        if kind == "ask_again":
            return {"reply": "Still waiting on your answer:\n" + str(action.get("question")),
                    "item_id": None, "decided": False, "presented": False}
        if kind in ("hold", "alias_yes", "alias_later", "forget"):
            from grove import adaptation as lane
            return lane.step(self, action, provenance)
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

    def _signature_step(self, action: Mapping[str, Any],
                        provenance: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        """Answer a message that arrived while the session is held for a
        signature. Nothing here decides an item or signs anything."""
        from grove import reissue

        prov = dict(provenance or {})
        session_id = str(prov.get("session_id") or "")
        out = {"item_id": None, "decided": False, "presented": False}
        if action.get("action") == "hold_signature":
            short = str(action.get("proposal") or "").split(":")[-1][:12]
            link = _portal(f"proposals/pending?type=signature&at=proposal-{short}")
            return {**out, "reply": (
                f"Paused for your signature on {action.get('what') or 'a proposal'}.\n"
                f"Review and sign: {link}\nOr tap Later on its card to carry on.")}
        hold = reissue.held(session_id)
        named = str(action.get("proposal") or "")
        if not hold or not str(hold["proposal_id"]).split(":")[-1].startswith(named):
            raise DecisionRefused("stale_card", "That card is out of date.")
        reissue.release_hold(session_id)
        what = str(hold.get("what") or "The proposal")
        still = self._proposal_waiting(hold["proposal_id"])
        armed = (self.present_next_after({"id": hold["proposal_id"]}, prov)
                 if self.pending() is None else False)
        lead = (f"OK, later. {what[0].upper() + what[1:]} stays under To sign." if still
                else f"{what[0].upper() + what[1:]} is no longer waiting.")
        return {**out, "reply": lead + " Carrying on.", "next_armed": armed}

    def _batch_step(self, provenance: Optional[Mapping[str, Any]],
                    inputs_for: Any) -> Dict[str, Any]:
        """Run the keg pass and say what happened in one message; then the
        items it left come to the operator one at a time, as usual."""
        if inputs_for is None:
            raise DecisionRefused(
                "no_reader", "This goal's tool did not say how to read its items.")
        from grove import reissue
        released = reissue.goal_note(self.config.goal_id, take=True)   # the pass runs once
        result = self.batch_pass(
            provenance, inputs_for,
            new_stage=bool(released) or self.current_batch() is None)
        one, many = self.config.item_name
        total, coded, left = result["total"], result["coded"], result["left"]
        if result["reason"] == "no_keg":
            lead = (f"No signed keg is serving, so nothing is decided in bulk. "
                    f"Bringing all {total} {one if total == 1 else many} to you one at a time.")
        elif result["reason"] == "not_green":
            lead = (f"Keg v{result['keg']['version']} is not signed to act without review, "
                    f"so nothing is decided in bulk. Bringing all {total} to you one at a time.")
        elif self.config.batch_order == BATCH_ITEM_ORDER and left:
            done = self.config.work_session.done_word.capitalize()
            ahead = len(result.get("for_model") or [])
            lead = ((f"{done} {coded} by the keg v{result['keg']['version']} · 0 model "
                     f"calls · {left} to go.\n" if coded else "")
                    + (f"The next {one} needs a model." if ahead <= 1 else
                       f"The next {ahead} {many} need a model."))
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
                # The operator's own words on this item, as evidence for a rule.
                "operator_said": verdict.get("operator_said") or record.get("operator_said"),
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

    def confirmed_key_evidence(self, key: Any) -> Dict[str, Any]:
        """Count this run's evidence that one reference key has an answer the
        operator keeps confirming, per the goal's declared ``confirmed_key``
        rule. Jidoka reads this; it flags, it never fixes.

        Only for a key the reference table does not list (a key with one
        value is already standard work; a key with several is reserved for
        judgment and never counts). A confirmation counts when a MODEL decided
        the item and the operator confirmed it. An item the keg decided, or
        accepted without review, never counts. One revision of any item with
        this key, or two different confirmed answers, and there is no pattern."""
        rule, ref_spec = self.config.evidence, self.config.reference
        out: Dict[str, Any] = {"met": False, "confirmations": 0, "evidence": [],
                               "output": None, "threshold": None}
        if (rule is None or ref_spec is None or not rule.confirmed_key_threshold
                or key in (None, "")):
            return out
        out["threshold"] = rule.confirmed_key_threshold
        if ReferenceTable(ref_spec).values(key):
            return out
        proposed, decided = self._state()
        answers: Dict[str, Dict[str, Any]] = {}
        rows: List[Dict[str, Any]] = []
        for record in proposed.values():
            if _norm_key(record["inputs"].get(ref_spec.key_input)) != _norm_key(key):
                continue
            verdict = decided.get(record["id"])
            if verdict is None:
                continue
            if verdict["decision"] == DECISION_CORRECT:
                return out                    # the operator revised one: no pattern
            if record.get("keg") or verdict["decision"] != DECISION_CONFIRM:
                continue                      # keg-decided or unreviewed: never evidence
            final = dict(verdict.get("output") or record["output"])
            answers[json.dumps(final, sort_keys=True)] = final
            rows.append({
                "item_id": record["item_id"], "turn_id": record.get("turn_id"),
                "turn_uid": record.get("turn_uid"), "decided_id": verdict["id"],
            })
        out["confirmations"] = len(rows)
        if len(answers) != 1:
            return out
        out.update(evidence=rows, output=next(iter(answers.values())),
                   met=len(rows) >= rule.confirmed_key_threshold)
        return out

    def alias_evidence(self, key: Any) -> Dict[str, Any]:
        """Count this run's evidence that ``key`` is an existing key under
        another name, per the goal's declared ``alias`` rule. Jidoka reads
        this; it flags, it never fixes.

        Met when ALL of these hold: the reference table does not list ``key``
        and the serving keg does not answer it; a model decided its items and
        the operator confirmed at least the declared number, none revised;
        exactly ONE key the serving keg answers by a plain rule is identified
        with it (a declared input is the same on both, or a declared input's
        text names that key); and what the operator confirmed is exactly what
        the keg answers for that key. Two candidates, or a different answer,
        and there is no alias."""
        from grove import keg as keg_mod

        rule, ref_spec = self.config.evidence, self.config.reference
        out: Dict[str, Any] = {"met": False, "confirmations": 0, "evidence": [],
                               "same_as": None, "identity": [], "output": None}
        if (rule is None or ref_spec is None or not rule.alias_confirmations
                or key in (None, "")):
            return out
        serving = self.serving_keg()
        if serving is None or ReferenceTable(ref_spec).values(key):
            return out
        spec, key_input = serving[0], ref_spec.key_input
        proposed, decided = self._state()
        mine: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        others: Dict[str, List[Dict[str, Any]]] = {}
        for record in proposed.values():
            verdict = decided.get(record["id"])
            if verdict is None:
                continue
            item_key = record["inputs"].get(key_input)
            if _norm_key(item_key) == _norm_key(key):
                if verdict["decision"] == DECISION_CORRECT:
                    return out                # the operator revised one: no pattern
                if not record.get("keg") and verdict["decision"] == DECISION_CONFIRM:
                    mine.append((record, verdict))
            else:
                others.setdefault(str(item_key), []).append(record)
        out["confirmations"] = len(mine)
        if len(mine) < rule.alias_confirmations:
            return out
        answers = {json.dumps(dict(v.get("output") or r["output"]), sort_keys=True)
                   for r, v in mine}
        if len(answers) != 1:
            return out
        confirmed = json.loads(next(iter(answers)))
        # Keys the serving keg answers with a plain "key == value" rule.
        plain = {}
        for condition in spec.get("conditions") or []:
            text = str(condition.get("if") or "")
            if condition.get("defer") or "then" not in condition:
                continue
            for other in others:
                if text == f"{key_input} == {_quoted(other)}":
                    plain[other] = dict(condition["then"])
        candidates: Dict[str, List[Dict[str, Any]]] = {}
        for record, _verdict in mine:
            for other, then in plain.items():
                for name in rule.alias_same:
                    value = str(record["inputs"].get(name) or "").strip()
                    if value and any(
                            str(r["inputs"].get(name) or "").strip() == value
                            for r in others[other]):
                        candidates.setdefault(other, []).append(
                            {"kind": "same", "input": name, "value": value})
                for name in rule.alias_names:
                    text = str(record["inputs"].get(name) or "")
                    if text and _norm_key(other) in _norm_key(text):
                        candidates.setdefault(other, []).append(
                            {"kind": "names", "input": name, "text": text[:300]})
        if len(candidates) != 1:
            return out                        # none, or ambiguous: never guessed
        same_as = next(iter(candidates))
        if plain[same_as] != confirmed:
            return out                        # the operator answered it differently
        identity = []
        for found in candidates[same_as]:
            if found not in identity:
                identity.append(found)
        out.update(
            met=True, same_as=same_as, identity=identity, output=confirmed,
            evidence=[{"item_id": r["item_id"], "turn_id": r.get("turn_id"),
                       "turn_uid": r.get("turn_uid"), "decided_id": v["id"]}
                      for r, v in mine])
        return out

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
