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

DECISION_CONFIRM = "confirm"
DECISION_CORRECT = "correct"

SCOPE_SINGLE_VALUE_KEYS = "single_value_keys"


class DecisionRefused(Exception):
    """A decision the turn was not entitled to make. ``reason`` is a short
    machine kind; the message is what the operator reads."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


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

    @property
    def isolated(self) -> bool:
        return self.isolation == ISOLATION_SOURCES_ONLY


def _resolve(root: Path, raw: Any) -> Path:
    p = Path(str(raw)).expanduser()
    return p if p.is_absolute() else (Path(root) / p)


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


def isolating_goal_for(message: str, *, dock: Any = None) -> Optional[str]:
    """The isolating goal a message opens work on, by the goal's own declared
    keywords — deterministic, no model. None when no isolating goal matches.
    A Dock or declaration fault returns None and is logged by the caller's
    loader; isolation is then enforced at the point of action instead (a
    session that was never isolated cannot record a decision)."""
    if dock is None:
        from grove.dock import load_dock
        dock = load_dock()
    for goal in (getattr(dock, "goals", None) or ()):
        cfg = load_config(goal)
        if cfg is not None and cfg.isolated and _keyword_matches(message, goal.keywords):
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

    def next_item(self) -> Optional[Path]:
        """The first queued item with no proposal in the current run."""
        proposed, _ = self._state()
        for path in self.queue_items():
            if path.stem not in proposed:
                return path
        return None

    # -- checks -----------------------------------------------------------

    def check_turn(self, provenance: Optional[Mapping[str, Any]]) -> None:
        """Refuse unless this turn may decide for this goal. Only an isolated
        goal checks; the reasons are stated plainly for the operator."""
        if not self.config.isolated:
            return
        if not provenance:
            raise DecisionRefused(
                "no_provenance",
                "This decision cannot be recorded: the turn's provenance is "
                "not available, so its sources cannot be verified.",
            )
        if provenance.get("isolation_goal") != self.config.goal_id:
            raise DecisionRefused(
                "session_not_isolated",
                f"This session includes turns outside the {self.config.goal_id} "
                f"goal, so its context is not clean. Start a new session with "
                f"/new, then ask again.",
            )
        sections = RECALL_SECTIONS & set(provenance.get("sections") or ())
        tools = RECALL_TOOLS & set(provenance.get("tools_yielded") or ())
        hits = int(provenance.get("cellar_hits") or 0)
        if sections or tools or hits:
            found = sorted(sections) + sorted(tools) + (["cellar hits"] if hits else [])
            raise DecisionRefused(
                "contaminated_turn",
                "This decision cannot be recorded: the turn drew on recalled "
                f"context ({', '.join(found)}). Work for this goal uses its "
                "declared sources only. Start a new session with /new, then "
                "ask again.",
            )

    def check_output(self, output: Mapping[str, Any]) -> None:
        missing = [k for k in self.config.outputs if output.get(k) in (None, "")]
        if missing:
            raise DecisionRefused(
                "missing_output", f"The decision is missing: {', '.join(missing)}.")
        unknown = [k for k in output if k not in self.config.outputs]
        if unknown:
            raise DecisionRefused(
                "undeclared_output", f"Not a declared output: {', '.join(unknown)}.")
        for domain in self.config.output_domains:
            value = str(output[domain.output]).strip()
            if value not in _domain_values(domain):
                raise DecisionRefused(
                    "output_not_in_domain",
                    f"{domain.output} {value!r} is not in {domain.path.name} "
                    f"({domain.column}).",
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
        self.check_output(output)
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
        })

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
            self.check_output(corrected_output)
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
        return self.log.append({
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
