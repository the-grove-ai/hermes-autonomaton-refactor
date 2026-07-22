"""fleet-receipt-custody-v1 P1 — runtime-bound identity pins.

Contract under test (SPEC banked in Notion): identity on a fleet unit is
minted by the runtime and echoed — a worker never names what it worked on.
Every receipt (success or failure) carries the unit identity the host
dispatched.

  T1  tool-transport forge: a model that emits a WRONG meta.row_id stages a
      package whose meta.json carries the DISPATCHED row id, not the model's.
  T2  the same binding on the sentinel (delimited) forge path.
  T3  a no_package failure receipt carries the dispatched unit identity.
  T4  regression fence: declarative producers' runtime-synthesized identity
      stays byte-identical (tool synth_meta override + sentinel stray-meta
      discard) — the fix must not drift them.

Integration through the REAL emit handler, staging jail, extraction, and
event assembly (temp GROVE home + sink); upstream stubbed at the same clean
seams as the test_fleet_emit_contract harness. Descriptive metadata (slug,
company, role) stays MODEL-authored — only identity is host-bound.
"""

import json
from pathlib import Path

import pytest

from grove.fleet import worker_entry
from tools import fleet_emit_tool
from tools.registry import invalidate_check_fn_cache


@pytest.fixture(autouse=True)
def _emit_tool_hygiene():
    fleet_emit_tool.reset()
    invalidate_check_fn_cache()
    yield
    fleet_emit_tool.reset()
    invalidate_check_fn_cache()


# ── shared clean-seam harness (test_fleet_emit_contract precedent) ──────────


class _FakeSessionDB:
    def __init__(self, *a, **k):
        pass


class _ScriptedAgent:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.max_tokens = 100

    def run_conversation(self, prompt, conversation_history=None, task_id=None):
        self.calls.append({"prompt": prompt, "history": conversation_history})
        step = self.script.pop(0) if self.script else {"messages": [], "completed": True}
        return step() if callable(step) else step


def _drive_worker(
    monkeypatch, tmp_path, cap_gov, payload, script,
    worker_id="forge", cap_id="skill.fleet.forge-jobsearch", run_id="rid1",
    captured_out=None,
):
    """``captured_out``: optional dict FILLED IN PLACE with the Dispatcher's
    construction kwargs (which lands BEFORE the first agent turn) — lets a
    script step reach run-scoped seams like sovereign_prompt_handler."""
    from grove.fleet import paths as _paths

    class _Cap:
        id = cap_id
        governance = cap_gov

        class tier_rule:
            preferred = 2

        model_binding = None

    agent = _ScriptedAgent(script)
    captured = captured_out if captured_out is not None else {}

    class _Dispatcher:
        def __init__(self, *a, **k):
            captured.update(k)
            self.agent = agent

    class _RuntimeContext:
        def __init__(self, **k):
            self.config = k.get("config")

    monkeypatch.setattr(_paths, "get_hermes_home", lambda: str(tmp_path))
    monkeypatch.setattr(worker_entry, "_load_capability_for", lambda wid: _Cap())
    monkeypatch.setattr(
        worker_entry, "_resolve_declared_sink", lambda cap, wid: tmp_path / "sink"
    )
    monkeypatch.setattr(
        worker_entry, "_derive_skill_name", lambda cap, wid: f"fleet/{wid}"
    )
    monkeypatch.setattr(
        worker_entry, "_resolve_worker_runtime",
        lambda cap, wid: ("m", 100, {"provider": "p"}),
    )
    monkeypatch.setattr("gateway.session_context.set_session_vars", lambda **k: object())
    monkeypatch.setattr("gateway.session_context.clear_session_vars", lambda *a, **k: None)
    monkeypatch.setattr("grove.grants.get_grant_store", lambda *a, **k: None)
    monkeypatch.setattr(
        "grove.fleet.read_surfaces.enforce_declared_surfaces", lambda *a, **k: []
    )
    monkeypatch.setattr("hermes_cli.config.load_config", lambda *a, **k: {})
    monkeypatch.setattr("hermes_state.SessionDB", _FakeSessionDB)
    monkeypatch.setattr("grove.dispatcher.Dispatcher", _Dispatcher)
    monkeypatch.setattr("grove.dispatcher.RuntimeContext", _RuntimeContext)
    ev = worker_entry.run_worker(worker_id, run_id, payload)
    return ev, agent, captured


def _emit_step(args):
    def _step():
        res = json.loads(fleet_emit_tool._handle_emit_package(args))
        assert res.get("staged") is True, res
        return {"messages": [{"role": "assistant", "content": "done"}], "completed": True}

    return _step


def _sentinel_messages(tag, files):
    blocks = "\n".join(
        f"@@@FILE_START: {name} [{tag}]@@@\n{body}\n@@@FILE_END: {name} [{tag}]@@@"
        for name, body in files.items()
    )
    return [{"role": "assistant", "content": blocks}]


# ── fixtures: the REAL notion_query dispatch shape (resolvers.py:166-173) ───

_DISPATCHED = "38f780a7-8eef-4dispatched-row"
_MODEL_WRONG = "row-model-authored-WRONG"

_FORGE_PAYLOAD = {
    "rows": [{"id": _DISPATCHED, "Fit Score": 0.91}],
    "data_source": "https://notion.example/ds",
    "filter": {},
    "unit_id": _DISPATCHED,
}

_FORGE_TOOL_GOV = {
    "emission_preconditions": {
        "terminal_artifact": {
            "tool": "write_file",
            "path_pattern": "*.md",
            "emit": {
                "transport": "tool",
                "files": {"required": ["resume.md", "cover-letter.md"]},
                "meta": {"required_keys": ["slug", "company", "role", "row_id"]},
            },
        }
    }
}

_FORGE_SENTINEL_GOV = {
    "emission_preconditions": {
        "terminal_artifact": {
            "tool": "write_file",
            "path_pattern": "*.md",
            "emit": {"transport": "sentinel"},
        }
    }
}

_WRONG_META_ARGS = {
    "files": {
        "resume.md": "# Résumé body\n",
        "cover-letter.md": "Cover body\n",
    },
    "meta": {
        "slug": "acme-pm",
        "company": "Acme",
        "role": "PM",
        "row_id": _MODEL_WRONG,
    },
}


# ── T1: tool transport — staged meta.row_id is the DISPATCHED id ────────────


def test_t1_tool_wrong_model_row_id_staged_meta_carries_dispatched(
    monkeypatch, tmp_path
):
    ev, _agent, _ = _drive_worker(
        monkeypatch, tmp_path, _FORGE_TOOL_GOV, _FORGE_PAYLOAD,
        [_emit_step(_WRONG_META_ARGS)],
    )
    assert ev["status"] == "success"
    meta = json.loads(
        (tmp_path / "sink" / "acme-pm" / "meta.json").read_text(encoding="utf-8")
    )
    # IDENTITY is host-bound: the dispatched row id, never the model's value.
    assert meta["row_id"] == _DISPATCHED
    assert meta["row_id"] != _MODEL_WRONG
    # DESCRIPTIVE metadata stays model-authored — do not widen the binding.
    assert meta["slug"] == "acme-pm"
    assert meta["company"] == "Acme"
    assert meta["role"] == "PM"
    # The receipt echoes the same host identity.
    assert ev["row_id"] == _DISPATCHED


# ── T2: sentinel transport — same binding on the delimited path ─────────────


def test_t2_sentinel_wrong_model_row_id_staged_meta_carries_dispatched(
    monkeypatch, tmp_path
):
    run_id = "rid42"
    files = {
        "resume.md": "# Résumé sentinel body",
        "cover-letter.md": "Cover sentinel body",
        "meta.json": json.dumps(
            {"slug": "acme-pm", "company": "Acme", "role": "PM",
             "row_id": _MODEL_WRONG}
        ),
    }
    msgs = _sentinel_messages(run_id[:8], files)
    ev, _agent, _ = _drive_worker(
        monkeypatch, tmp_path, _FORGE_SENTINEL_GOV, _FORGE_PAYLOAD,
        [{"messages": msgs, "completed": True}], run_id=run_id,
    )
    assert ev["status"] == "success"
    meta = json.loads(
        (tmp_path / "sink" / "acme-pm" / "meta.json").read_text(encoding="utf-8")
    )
    assert meta["row_id"] == _DISPATCHED
    assert meta["row_id"] != _MODEL_WRONG
    assert meta["slug"] == "acme-pm"
    assert meta["company"] == "Acme"
    assert meta["role"] == "PM"
    assert ev["row_id"] == _DISPATCHED


# ── T3: a no_package failure receipt carries the dispatched identity ────────


def test_t3_no_package_failure_receipt_carries_dispatched_identity(
    monkeypatch, tmp_path
):
    prose = {
        "messages": [{"role": "assistant", "content": "prose only"}],
        "completed": True,
    }
    ev, _agent, _ = _drive_worker(
        monkeypatch, tmp_path, _FORGE_TOOL_GOV, _FORGE_PAYLOAD,
        [prose, prose, prose],
    )
    assert ev["status"] == "failed" and ev["check"] == "no_package"
    # The host dispatched this identity; the model's meta never existed —
    # the receipt must carry it anyway (work happened against THIS row).
    assert ev["row_id"] == _DISPATCHED


# ── P1.2 Commit A: governed_denial receipts carry dispatched identity ───────


def test_governed_denial_receipt_carries_dispatched_identity(monkeypatch, tmp_path):
    """A governed denial recurs deterministically — the worker is blocked, so
    every retry produces the identical failure. Both denial sites (first run
    and the emit-ladder re-prompt) must stamp the dispatched identity or the
    purest poison pill stays uncountable."""
    from grove.governance_halt import GovernanceHaltContext, TerminalGovernanceHalt

    def _deny():
        raise TerminalGovernanceHalt(
            GovernanceHaltContext(trigger="deny_hard", tool_name="write_file")
        )

    # Site 1 — denial on the FIRST run_conversation.
    ev, _agent, _ = _drive_worker(
        monkeypatch, tmp_path, _FORGE_TOOL_GOV, _FORGE_PAYLOAD, [_deny]
    )
    assert ev["status"] == "failed" and ev["check"] == "governed_denial"
    assert ev["row_id"] == _DISPATCHED

    # Site 2 — denial during the emit-ladder re-prompt (no emit on turn one).
    prose = {
        "messages": [{"role": "assistant", "content": "prose only"}],
        "completed": True,
    }
    ev2, _agent2, _ = _drive_worker(
        monkeypatch, tmp_path, _FORGE_TOOL_GOV, _FORGE_PAYLOAD, [prose, _deny],
        run_id="rid2",
    )
    assert ev2["status"] == "failed" and ev2["check"] == "governed_denial"
    assert ev2["row_id"] == _DISPATCHED


# ── P1.2 Commit C: the terminal-receipt identity INVARIANT ──────────────────
#
# Every terminal receipt carries the identity of the unit it was dispatched
# for, unless it falls in a NAMED structural exception:
#   1. no_work at the empty-payload gate (worker_entry run_worker) — payload
#      is None; no unit exists. SOURCE-level exception: the only _event call
#      allowed to omit identity tokens entirely.
#   2. inbox_missing / worker_not_registered — fail before a payload exists
#      (inbox_missing strictly; worker_not_registered when the inbox also
#      failed). VALUE-level exception: the identity KEY is stamped by the
#      main() catch-all mechanism, the value is null.
# Auto-enrolling, same pattern as the byte-parity canary: the AST scan
# enumerates every _event call site in worker_entry at collection time, so a
# NEW failure branch added without identity fails this pin.


def _discover_event_receipt_sites():
    """Discover every ``_event(...)`` receipt-builder call site across the
    ``grove`` package (fleet-receipt-custody-v1 P2 C2b).

    Matches BOTH call shapes — bare-name ``_event(...)`` and attribute
    ``<mod>._event(...)`` — so the identity invariant cannot be bypassed by
    import style. Discovery replaces the hand-maintained module list, which had
    grown twice (+runner in C1, +staging in C2b) and is itself a drift magnet: a
    receipt writer added ANYWHERE under ``grove/`` enrols here at collection
    time, no list edit required.

    RESIDUAL FENCE (stated, not hidden): the scan is scoped to the ``grove``
    package (~200ms, package-wide). A receipt writer added in ANOTHER top-level
    package would escape it. Widen the root if the receipt surface ever leaves
    ``grove``.

    Returns ``(modules: set[str], segments: list[str])``.
    """
    import ast
    import pathlib

    import grove

    root = pathlib.Path(grove.__file__).parent
    modules: set = set()
    segments: list = []
    for py in sorted(root.rglob("*.py")):
        try:
            src = py.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except (OSError, SyntaxError):
            continue
        rel = str(py.relative_to(root.parent))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if (isinstance(f, ast.Name) and f.id == "_event") or (
                isinstance(f, ast.Attribute) and f.attr == "_event"
            ):
                modules.add(rel)
                seg = ast.get_source_segment(src, node)
                if seg:
                    segments.append(seg)
    return modules, segments


@pytest.mark.guard
def test_terminal_receipt_identity_invariant_enumerates_all_branches():
    import re

    modules, segments = _discover_event_receipt_sites()

    # Vacuity — BOTH legs. A broken scan must not pass by discovering nothing,
    # nor by silently dropping a module. The set is non-empty AND contains every
    # known receipt writer: worker_entry (in-worker events), runner (C1 genesis
    # aborts), staging (C2b synthetic kill/crash receipts).
    assert segments, "discovery found ZERO _event call sites — the scan is vacuous"
    known = {
        "grove/fleet/worker_entry.py",
        "grove/fleet/runner.py",
        "grove/fleet/staging.py",
    }
    missing = known - modules
    assert not missing, (
        f"discovery missed known receipt writer(s) {sorted(missing)} — the scan "
        "is broken or a writer moved; the invariant would be silently narrowed"
    )

    # Exactly ONE source-level named exception (the no_work empty-payload gate).
    exceptions = [seg for seg in segments if "empty payload" in seg]
    assert len(exceptions) == 1, (
        "exactly ONE source-level named exception is allowed (the no_work "
        f"empty-payload gate); found {len(exceptions)}: {exceptions!r}"
    )
    unstamped = [
        seg
        for seg in segments
        if seg not in exceptions
        and not re.search(r"\b(unit_id|row_id|event_kw)\b", seg)
    ]
    assert not unstamped, (
        "terminal-receipt identity invariant violated — _event call site(s) "
        "without a dispatched-identity field (unit_id/row_id/event_kw) and "
        "not in the named-exception set. Thread the dispatched identity "
        f"like the sibling branches:\n" + "\n---\n".join(unstamped)
    )


def test_main_catchall_receipt_carries_dispatched_identity(monkeypatch, tmp_path):
    """Behavioral leg: a FleetWorkerAndon raised INSIDE run_worker reaches
    main()'s catch-all with the payload in scope — the receipt carries the
    dispatched identity. The inbox_missing named exception stamps the key
    with a null value (no payload ever existed)."""
    from grove.fleet import paths as _paths
    from grove.fleet.errors import FleetWorkerAndon

    monkeypatch.setattr(_paths, "get_hermes_home", lambda: str(tmp_path))
    _paths.events_dir("forge").mkdir(parents=True, exist_ok=True)

    # Case A — structural Andon after the payload exists (e.g. path_escape).
    monkeypatch.setattr(
        worker_entry, "_read_inbox_payload", lambda w, r: dict(_FORGE_PAYLOAD)
    )

    def _boom(w, r, p):
        raise FleetWorkerAndon("staged path escaped", worker_id=w, check="path_escape")

    monkeypatch.setattr(worker_entry, "run_worker", _boom)
    rc = worker_entry.main(["--worker-id", "forge", "--run-id", "cc1"])
    assert rc == 1
    ev = json.loads(_paths.event_path("forge", "cc1").read_text(encoding="utf-8"))
    assert ev["status"] == "failed" and ev["check"] == "path_escape"
    assert ev["row_id"] == _DISPATCHED

    # Case B — NAMED exception: inbox_missing fails BEFORE a payload exists.
    # The mechanism still stamps the identity key; the value is null.
    def _no_inbox(w, r):
        raise FleetWorkerAndon("no inbox payload", worker_id=w, check="inbox_missing")

    monkeypatch.setattr(worker_entry, "_read_inbox_payload", _no_inbox)
    rc2 = worker_entry.main(["--worker-id", "forge", "--run-id", "cc2"])
    assert rc2 == 1
    ev2 = json.loads(_paths.event_path("forge", "cc2").read_text(encoding="utf-8"))
    assert ev2["status"] == "failed" and ev2["check"] == "inbox_missing"
    assert ev2["row_id"] is None  # key present, value null — the named shape


# ── P2 Commit B: a Yellow deferral is its own check class ───────────────────


def test_yellow_deferral_receipt_is_approval_deferred_with_identity(
    monkeypatch, tmp_path
):
    """A headless run that hits a Yellow gate mid-run gets a denial
    Observation and keeps going ('Paused is not failed') — pre-B it fell off
    the emit ladder as no_package, masquerading as an emission failure. The
    receipt now carries its own check class, the gated tool name(s) in the
    deferred_actions rider, AND the dispatched identity (the P1.2C invariant
    holds on this branch — it rides the same identity-stamped call site)."""
    from types import SimpleNamespace

    holder = {}  # filled by the harness at Dispatcher construction

    def _gated_then_prose():
        # Simulate the Dispatcher invoking the installed sovereign handler on
        # a Yellow AndonHalt mid-turn (the run-scoped closure under test),
        # then the model ending its turn with prose, never emitting.
        halt = SimpleNamespace(
            intents=[SimpleNamespace(tool_name="write_file", arguments={})],
            triggering_index=0,
            zone="yellow",
        )
        assert holder["sovereign_prompt_handler"](halt) == "deny"  # contract untouched
        return {
            "messages": [{"role": "assistant",
                          "content": "That action needs your approval."}],
            "completed": True,
        }

    prose = {
        "messages": [{"role": "assistant", "content": "still waiting"}],
        "completed": True,
    }
    ev, _agent, _ = _drive_worker(
        monkeypatch, tmp_path, _FORGE_TOOL_GOV, _FORGE_PAYLOAD,
        [_gated_then_prose, prose],  # deferral, then ladder exhaustion
        captured_out=holder,
    )
    assert ev["status"] == "failed"
    assert ev["check"] == "approval_deferred"  # its OWN class, not no_package
    assert ev["deferred_actions"] == ["write_file"]  # the gated tool, named
    assert ev["row_id"] == _DISPATCHED  # identity invariant holds on this branch


# ── P2 Commit A: one writer per sink — seal the interactive door ────────────
#
# stage_package is the ONLY path that writes into a fleet staging sink. No
# skill prose (record context.payload or repo SKILL.md) may instruct a model
# to write into one directly — that door bypasses the slug/basename jail, the
# clean-room wipe, the meta-last atomic ordering, AND the P1.1 identity bind,
# and its model-authored row_id feeds the dispatch skip list unchecked.
# Auto-enrolling, same shape as the AST identity-invariant pin: sinks are
# DISCOVERED from the fleet records' declared write_zone.staging_dir and the
# document set is every payload-bearing record + every repo SKILL.md, so a
# new skill (or a new sink) enrols at collection time.

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DIRECT_WRITE_TOKENS = __import__("re").compile(r"\b(write_file|mkdir)\b")


def _fleet_staging_sinks():
    import yaml

    sinks = set()
    for yml in sorted((_REPO_ROOT / "config" / "capabilities").glob("skill__fleet__*.yaml")):
        doc = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
        staging = (((doc.get("governance") or {}).get("write_zone")) or {}).get(
            "staging_dir"
        )
        if staging:
            sinks.add(staging)
    return sinks


def _model_facing_prose():
    import yaml

    docs = []
    for yml in sorted((_REPO_ROOT / "config" / "capabilities").glob("*.yaml")):
        try:
            doc = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
        except Exception:
            continue  # malformed record is another pin's business
        payload = (doc.get("context") or {}).get("payload")
        if isinstance(payload, str) and payload.strip():
            docs.append((str(yml.relative_to(_REPO_ROOT)), payload))
    for md in sorted((_REPO_ROOT / "skills").rglob("SKILL.md")):
        docs.append((str(md.relative_to(_REPO_ROOT)), md.read_text(encoding="utf-8")))
    return docs


@pytest.mark.guard
def test_no_prose_instructs_direct_writes_into_fleet_staging_sinks():
    import re

    sinks = _fleet_staging_sinks()
    # Vacuity guards — the scan must never silently match zero.
    assert len(sinks) >= 4, f"vacuity: only {len(sinks)} staging sinks discovered"
    assert "forge/pending_review" in sinks, (
        "vacuity: the known forge sink was not discovered — did the "
        "write_zone.staging_dir schema move?"
    )
    docs = _model_facing_prose()
    assert len(docs) >= 20, f"vacuity: only {len(docs)} prose documents scanned"

    # A sink reference counts only as a PATH: path-shaped sinks (containing
    # '/') match verbatim; bare sink names must appear anchored under .grove/
    # so prose using the plain word ('researcher', 'scout') never false-flags.
    sink_pats = [
        re.compile(re.escape(s)) if "/" in s
        else re.compile(r"\.grove/" + re.escape(s) + r"(/|\b)")
        for s in sorted(sinks)
    ]

    offenders = []
    for name, body in docs:
        for para in re.split(r"\n\s*\n", body):
            if not _DIRECT_WRITE_TOKENS.search(para):
                continue
            if any(p.search(para) for p in sink_pats):
                offenders.append((name, para.strip()[:200]))

    # RATCHET ALLOWLIST — known legacy direct-write prose, named per document
    # (P2 Commit A census). Forge is deliberately ABSENT: its interactive door
    # is sealed by this commit. The rest is banked debt for the follow-up
    # sweep; a NEW skill can never hide here, and an allowlisted document that
    # stops offending MUST be removed (the ratchet only tightens).
    # P2 A2 swept cultivator/drafter/researcher. The two SCOUTS remain by
    # RULING-PENDING exception: they are NOT fleet workers (absent from
    # config/fleet_workers.yaml), they run on the interactive surface where
    # write_file IS offered, and their direct-write prose is LOAD-BEARING —
    # scout's ~/.grove/scout/ digests are cultivator's fleet input
    # (fleet_workers.yaml input_state.source_dir: scout). Sealing them needs
    # a sanctioned writer or fleet migration first, not a prose deletion.
    allowed = {
        "config/capabilities/skill__fleet__scout.yaml",
        "config/capabilities/skill__fleet__scout_jobsearch.yaml",
        "skills/fleet/scout/SKILL.md",
        "skills/fleet/scout-jobsearch/SKILL.md",
    }
    live = [(n, p) for n, p in offenders if n not in allowed]
    assert not live, (
        "prose instructs a direct write into a fleet staging sink — "
        "stage_package is the ONLY writer for these sinks (P2 Commit A):\n"
        + "\n---\n".join(f"{n}: {p}" for n, p in live)
    )
    stale_allow = allowed - {n for n, _ in offenders}
    assert not stale_allow, (
        "ratchet: allowlisted document(s) no longer offend — remove them so "
        f"the debt list only ever shrinks: {sorted(stale_allow)}"
    )


# ── T4: declarative producers — byte-identical regression fence ─────────────

_DRAFTER_TOOL_GOV = {
    "emission_preconditions": {
        "terminal_artifact": {
            "tool": "write_file",
            "path_pattern": "draft-*.md",
            "emit": {"transport": "tool"},
        }
    }
}

_DRAFTER_SENTINEL_GOV = {
    "emission_preconditions": {
        "terminal_artifact": {
            "tool": "write_file",
            "path_pattern": "draft-*.md",
            "emit": {"transport": "sentinel"},
        }
    }
}

_DRAFTER_PAYLOAD = {
    "units": [{"id": "u1"}],
    "unit_id": "u1",
    "source_path": "/src/u1.md",
    "source_name": "u1.md",
}


def test_t4_declarative_tool_synth_meta_byte_identical(monkeypatch, tmp_path):
    """Tool declarative: staged meta.json is EXACTLY the runtime-synthesized
    envelope — the fix must not perturb the existing override."""
    ev, _agent, _ = _drive_worker(
        monkeypatch, tmp_path, _DRAFTER_TOOL_GOV, _DRAFTER_PAYLOAD,
        [_emit_step({"files": {"draft-u1.md": "draft body\n"}})],
        worker_id="drafter", cap_id="skill.fleet.drafter",
    )
    assert ev["status"] == "success"
    staged_meta = (tmp_path / "sink" / "u1" / "meta.json").read_text(encoding="utf-8")
    expected = worker_entry._synthesize_meta(_DRAFTER_PAYLOAD, "drafter", "u1")
    assert staged_meta == expected  # byte-identical, not merely equivalent
    assert ev["unit_id"] == "u1"
    assert ev["slug"] == "u1"
    assert ev["row_id"] is None  # forge-only field stays absent


def test_t4_declarative_sentinel_stray_meta_still_discarded(monkeypatch, tmp_path):
    """Sentinel declarative: a stray skill-emitted meta.json is DISCARDED and
    the runtime envelope staged in its place — behavior unchanged."""
    run_id = "rid77"
    files = {
        "draft-u1.md": "draft body\n",
        "meta.json": '{"slug": "hijack", "row_id": "hijack"}',
    }
    msgs = _sentinel_messages(run_id[:8], files)
    ev, _agent, _ = _drive_worker(
        monkeypatch, tmp_path, _DRAFTER_SENTINEL_GOV, _DRAFTER_PAYLOAD,
        [{"messages": msgs, "completed": True}],
        worker_id="drafter", cap_id="skill.fleet.drafter", run_id=run_id,
    )
    assert ev["status"] == "success"
    staged_meta = (tmp_path / "sink" / "u1" / "meta.json").read_text(encoding="utf-8")
    expected = worker_entry._synthesize_meta(_DRAFTER_PAYLOAD, "drafter", "u1")
    assert staged_meta == expected
    assert json.loads(staged_meta)["slug"] == "u1"  # never the stray "hijack"
    assert ev["unit_id"] == "u1"
