"""wiki-writer-structured-output-v1 P1 — emit_package contract pins.

Four pin families (CC-PROMPT CHANGE 4):
  * PARITY — the registered tool schema equals the capability-record
    declaration, per producer (GATE-B F5: record declares, harness derives).
  * LIFECYCLE — lock-on-emit (double-emit loud), no-emit → ONE re-prompt →
    no_package, truncation → raised-cap retry → Andon (real handler + real
    staging into a temp sink; run_worker driven whole with clean-seam stubs,
    the fleet-failure-forensics harness precedent).
  * BASENAME JAIL — arg filenames run the verbatim _is_safe_basename jail.
  * DUAL-READ — a sentinel-emitting worker under flag=sentinel stages
    byte-identically to baseline; a tool-flagged producer's sentinel output
    is still accepted (F6 migration acceptance).
Plus the loader emit-block pins (C1 non-destructive pattern) and the
_effective_finish_reason fold pin (P0 finding 1: OpenRouter's top-level
finish_reason lies; native_finish_reason is truth).
"""

import json
from pathlib import Path

import pytest

from grove.fleet import worker_entry
from tools import fleet_emit_tool
from tools.registry import ToolRegistry, invalidate_check_fn_cache


@pytest.fixture(autouse=True)
def _emit_tool_hygiene():
    """The emit tool module is process-global (= run-scoped in a real worker
    subprocess); tests share a process, so disarm around each one."""
    fleet_emit_tool.reset()
    invalidate_check_fn_cache()
    yield
    fleet_emit_tool.reset()
    invalidate_check_fn_cache()


# ── fixtures ────────────────────────────────────────────────────────────────

_FORGE_EMIT = {
    "transport": "tool",
    "files": {"required": ["resume.md", "cover-letter.md"]},
    # fleet-receipt-custody-v1 P1.1 — the model-facing floor mirrors the live
    # record: descriptive triad only; row_id is runtime-bound, never asked.
    "meta": {"required_keys": ["slug", "company", "role"]},
}

_FORGE_GOV = {
    "emission_preconditions": {
        "terminal_artifact": {
            "tool": "write_file",
            "path_pattern": "*.md",
            "emit": dict(_FORGE_EMIT),
        }
    }
}

_DRAFTER_GOV = {
    "emission_preconditions": {
        "terminal_artifact": {
            "tool": "write_file",
            "path_pattern": "draft-*.md",
            "emit": {"transport": "tool"},
        }
    }
}


def _configure_forge(tmp_path) -> Path:
    sink = tmp_path / "sink"
    sink.mkdir(parents=True, exist_ok=True)
    fleet_emit_tool.configure(
        expected_files=["resume.md", "cover-letter.md"],
        meta_required_keys=["slug", "company", "role"],
        sink=sink,
        slug=None,
        synth_meta=None,
    )
    return sink


def _configure_declarative(tmp_path, unit_id="unit1") -> Path:
    sink = tmp_path / "sink"
    sink.mkdir(parents=True, exist_ok=True)
    fleet_emit_tool.configure(
        expected_files=[f"draft-{unit_id}.md"],
        meta_required_keys=None,
        sink=sink,
        slug=unit_id,
        synth_meta=json.dumps({"unit_id": unit_id, "slug": unit_id}),
    )
    return sink


def _emit(args):
    return json.loads(fleet_emit_tool._handle_emit_package(args))


# ── loader pins (C1 non-destructive pattern, second sibling) ────────────────


def test_live_records_declare_expected_transports():
    """The five producer records load with valid emit declarations — forge +
    drafter on tool (the two proven-loss producers), the rest sentinel."""
    from grove.capability_registry import load_capabilities

    recs = load_capabilities()
    expect = {
        "skill.fleet.forge-jobsearch": "tool",
        "skill.fleet.drafter": "tool",
        "skill.fleet.scout": "sentinel",
        "skill.fleet.researcher": "sentinel",
        "skill.fleet.cultivator": "sentinel",
    }
    for rid, transport in expect.items():
        cap = recs[rid]
        decl = worker_entry._emit_declaration(cap)
        assert decl is not None, f"{rid}: emit declaration missing/errored"
        assert decl["transport"] == transport, rid
        ta = cap.governance["emission_preconditions"]["terminal_artifact"]
        assert "emit_error" not in ta, f"{rid}: loader flagged the emit block"


def test_malformed_emit_gets_nondestructive_error_sibling():
    from grove.capability import _validate_emit

    for bad in (
        {"transport": "telepathy"},
        {"transport": "tool", "surprise": 1},
        {"transport": "tool", "files": {"required": []}},
        {"transport": "tool", "meta": {"required_keys": [""]}},
        "not-a-mapping",
        {},
    ):
        gov = {"emission_preconditions": {"terminal_artifact": {"emit": bad}}}
        _validate_emit(gov, "test.record")
        ta = gov["emission_preconditions"]["terminal_artifact"]
        assert ta["emit"] == bad, "operator's block must never be destroyed"
        assert ta.get("emit_error"), f"no emit_error for {bad!r}"

        class _Cap:
            governance = gov

        assert worker_entry._emit_declaration(_Cap()) is None, (
            f"errored block must resolve ABSENT (sentinel default): {bad!r}"
        )


def test_valid_emit_clears_stale_error_and_roundtrips():
    from grove.capability import Capability, _validate_emit

    gov = {
        "emission_preconditions": {
            "terminal_artifact": {"emit": dict(_FORGE_EMIT), "emit_error": "stale"}
        }
    }
    _validate_emit(gov, "test.record")
    assert "emit_error" not in gov["emission_preconditions"]["terminal_artifact"]

    # Round-trip: the governance block (emit included) survives from_dict →
    # to_yaml. Validated against a REAL bundled record shape (the C1 loader
    # test precedent) so the pin exercises the exact production dict.
    import copy

    import yaml as _yaml

    repo = Path(__file__).resolve().parents[1]
    d = copy.deepcopy(
        _yaml.safe_load(
            (repo / "config" / "capabilities" / "skill__fleet__cultivator.yaml")
            .read_text(encoding="utf-8")
        )
    )
    d["id"] = "skill.fleet.testworker"
    d["governance"]["emission_preconditions"]["terminal_artifact"]["emit"] = dict(
        _FORGE_EMIT
    )
    cap = Capability.from_dict(d)
    reloaded = _yaml.safe_load(cap.to_yaml())
    assert (
        reloaded["governance"]["emission_preconditions"]["terminal_artifact"]["emit"]
        == _FORGE_EMIT
    )


def test_absent_emit_resolves_sentinel_default():
    class _Cap:
        governance = {
            "emission_preconditions": {"terminal_artifact": {"tool": "write_file"}}
        }

    assert worker_entry._emit_declaration(_Cap()) is None

    class _Bare:
        pass  # no governance attribute at all (test-stub / recordless case)

    assert worker_entry._emit_declaration(_Bare()) is None


# ── parity pins: registered schema == record declaration, per producer ──────


def _registered_function_block():
    reg = ToolRegistry()
    fleet_emit_tool.register(reg)
    invalidate_check_fn_cache()
    defs = reg.get_definitions({"emit_package"})
    assert len(defs) == 1 and defs[0]["type"] == "function"
    return defs[0]["function"]


def test_parity_forge_registered_equals_declared(tmp_path):
    """Record → _derive_emit_spec → configure → registry.get_definitions:
    the schema the model sees IS the declaration-derived one."""

    class _Cap:
        governance = _FORGE_GOV

    decl = worker_entry._emit_declaration(_Cap())
    expected, meta_keys, slug, synth = worker_entry._derive_emit_spec(
        decl,
        declarative=False,
        content_files=None,
        payload={"rows": []},
        worker_id="forge",
    )
    assert (expected, meta_keys, slug, synth) == (
        ["resume.md", "cover-letter.md"],
        ["slug", "company", "role"],  # P1.1 — descriptive floor; row_id never asked
        None,
        None,
    )
    _configure_forge(tmp_path)
    fn = _registered_function_block()
    assert fn == fleet_emit_tool.build_schema(expected, meta_keys)
    # Declaration facts surface in the schema itself:
    assert fn["parameters"]["properties"]["files"]["required"] == expected
    assert fn["parameters"]["properties"]["files"]["additionalProperties"] is False
    assert fn["parameters"]["properties"]["meta"]["required"] == ["slug", "company", "role"]
    assert "row_id" not in fn["parameters"]["properties"]["meta"]["properties"]
    assert "meta" in fn["parameters"]["required"]


def test_parity_drafter_registered_equals_declared(tmp_path):
    """Declarative producer: files derive from path_pattern + unit_id (the
    existing _declarative_content_files derivation — no duplicate declaration);
    no meta arg (identity is runtime-synthesized)."""

    class _Cap:
        governance = _DRAFTER_GOV

    payload = {"units": [1], "unit_id": "moon-bot", "source_path": "/s", "source_name": "s"}
    content_files = worker_entry._declarative_content_files(_Cap(), payload, "drafter")
    assert content_files == ["draft-moon-bot.md"]
    decl = worker_entry._emit_declaration(_Cap())
    expected, meta_keys, slug, synth = worker_entry._derive_emit_spec(
        decl,
        declarative=True,
        content_files=content_files,
        payload=payload,
        worker_id="drafter",
    )
    assert expected == ["draft-moon-bot.md"]
    assert meta_keys is None and slug == "moon-bot"
    assert json.loads(synth)["unit_id"] == "moon-bot"
    _configure_declarative(tmp_path, "moon-bot")
    fn = _registered_function_block()
    assert fn == fleet_emit_tool.build_schema(["draft-moon-bot.md"], None)
    assert "meta" not in fn["parameters"]["properties"]


def test_thin_tool_declaration_is_loud_for_self_authored():
    from grove.fleet.errors import FleetWorkerAndon

    with pytest.raises(FleetWorkerAndon):
        worker_entry._derive_emit_spec(
            {"transport": "tool"},  # no files.required / meta.required_keys
            declarative=False,
            content_files=None,
            payload={"rows": []},
            worker_id="forge",
        )


def test_unconfigured_tool_is_not_offered():
    reg = ToolRegistry()
    fleet_emit_tool.register(reg)
    invalidate_check_fn_cache()
    assert reg.get_definitions({"emit_package"}) == []  # check_fn gates it off


def test_fleet_floor_ceiling_includes_emit_package():
    """L2 floor source pin: the config-blind fleet ceiling admits emit_package
    (the L1 per-spawn allow-list enables it only for tool-transport spawns)."""
    import inspect

    from grove.dispatcher import Dispatcher

    src = inspect.getsource(Dispatcher.get_authorized_tools)
    assert '"emit_package"' in src.split("_FLEET_FLOOR = ")[1].split("\n")[0]


@pytest.mark.guard
def test_fleet_floor_tools_classify_green_through_real_zone_path():
    """P1.1 bake-Andon pin: every tool on the fleet L2 floor must resolve
    GREEN through the REAL zone classification path (repo zones.schema.yaml →
    ZoneClassifier.classify), not the stubbed-Dispatcher harness. A grant-less
    worker's non_interactive_deny_handler refuses Yellow/Red, so a floor tool
    that is registered and offered but NOT zone-declared is dead on execution
    — the exact live failure of bake run p1bake20260710aaaaaaaa ('That action
    needs your approval.'). The floor is parsed from the Dispatcher source so
    a FUTURE floor addition without a zone declaration fails HERE, not live."""
    import ast
    import inspect

    from grove.dispatcher import Dispatcher
    from grove.zones import ZoneClassifier

    src = inspect.getsource(Dispatcher.get_authorized_tools)
    floor = ast.literal_eval(src.split("_FLEET_FLOOR = ")[1].split("\n")[0])
    assert set(floor) >= {"read_file", "skill_view", "emit_package"}

    repo = Path(__file__).resolve().parents[1]
    clf = ZoneClassifier(repo / "config" / "zones.schema.yaml")
    for tool in floor:
        res = clf.classify(tool)
        assert res.zone == "green" and res.source == "tool_zones", (
            f"fleet floor tool {tool!r} classifies {res.zone!r} (source "
            f"{res.source!r}) — a grant-less worker cannot execute it; declare "
            f"it in config/zones.schema.yaml tool_zones"
        )


# ── lifecycle pins: real handler, real staging, temp sink ───────────────────

_FORGE_ARGS = {
    "files": {
        "resume.md": "# Résumé — “quoted”, C:\\path\\file\n",
        "cover-letter.md": "Dear team,\nliteral \"quotes\" and\nnewlines.\n",
    },
    # forge-publish-meta-hotfix-v1 P1 — the canonical success fixture carries the
    # COMPLETE publish triad (company/role/row_id) so a "success" run is a clean,
    # publish-ready package with no meta_defect marker.
    "meta": {"slug": "acme-pm", "row_id": "r1", "company": "Acme", "role": "PM"},
}


def test_lock_on_emit_stages_bytes_then_refuses_second(tmp_path):
    sink = _configure_forge(tmp_path)
    out = _emit(_FORGE_ARGS)
    assert out["staged"] is True and out["locked"] is True and out["slug"] == "acme-pm"
    # byte-exact staging through the SAME jailed stage_package primitive:
    for name, body in _FORGE_ARGS["files"].items():
        assert (sink / "acme-pm" / name).read_text(encoding="utf-8") == body
    meta_disk = json.loads((sink / "acme-pm" / "meta.json").read_text(encoding="utf-8"))
    # P1.1 (A6 RULED): row_id is no longer on the model-facing floor — a
    # habit-emitted one is STRIPPED and recorded; no bind here (direct
    # configure without a dispatched identity), so it is simply absent.
    assert meta_disk == {k: v for k, v in _FORGE_ARGS["meta"].items() if k != "row_id"}
    emitted = fleet_emit_tool.emitted()
    assert emitted["slug"] == "acme-pm" and len(emitted["staged"]) == 3
    assert emitted["stripped_meta_keys"] == ["row_id"]  # the strip is RECORDED

    second = _emit(_FORGE_ARGS)
    assert "already emitted" in second["error"]
    # the lock held: nothing re-staged, state unchanged
    assert fleet_emit_tool.emitted() == emitted


def test_rejection_does_not_lock_and_correction_succeeds(tmp_path):
    sink = _configure_forge(tmp_path)
    bad = {"files": {"resume.md": "only one file\n"}, "meta": {"slug": "s1"}}
    out = _emit(bad)
    assert "missing required file(s)" in out["error"]
    assert fleet_emit_tool.emitted() is None  # no lock on rejection
    assert not list((sink).glob("**/*.md"))  # nothing staged
    ok = _emit(_FORGE_ARGS)  # model corrected and re-called
    assert ok["staged"] is True


def test_basename_jail_on_arg_filenames(tmp_path):
    sink = _configure_forge(tmp_path)
    for evil in ("../evil.md", "/abs/evil.md", "a/b.md", "..", ".", ""):
        args = {
            "files": {evil: "x\n", "resume.md": "r\n", "cover-letter.md": "c\n"},
            "meta": {"slug": "s1"},
        }
        out = _emit(args)
        assert "unsafe filename" in out["error"], evil
        assert fleet_emit_tool.emitted() is None
    assert not list(sink.rglob("*")), "traversal attempt must stage NOTHING"


def test_forge_meta_contract_loud(tmp_path):
    _configure_forge(tmp_path)
    ok_files = dict(_FORGE_ARGS["files"])
    assert "meta" in _emit({"files": ok_files})["error"]
    assert "slug" in _emit({"files": ok_files, "meta": {"role": "x"}})["error"]
    assert "safe slug" in _emit(
        {"files": ok_files,
         "meta": {"slug": "../up", "company": "x", "role": "y"}}
    )["error"]
    # identity travels as the meta ARG, not a meta.json file:
    with_meta_file = {**ok_files, "meta.json": "{}"}
    out = _emit({"files": with_meta_file, "meta": {"slug": "s1"}})
    assert "unexpected file(s)" in out["error"] and "meta" in out["error"]


# ── forge-meta-admission-hotfix-v1 HF-1 — emit floor == publish contract ────


def _configure_forge_full(tmp_path) -> Path:
    """The LIVE forge floor (P1.1): slug + the descriptive pair; row_id is the
    HOST's — bound at emit from the dispatched unit identity."""
    sink = tmp_path / "sink"
    sink.mkdir(parents=True, exist_ok=True)
    fleet_emit_tool.configure(
        expected_files=["resume.md", "cover-letter.md"],
        meta_required_keys=["slug", "company", "role"],
        sink=sink,
        slug=None,
        synth_meta=None,
        bound_row_id="pg-1",
    )
    return sink


def test_hf1_slug_only_meta_refused_then_corrected_call_stages(tmp_path):
    """A slug-only meta is REFUSED at the emit floor with the missing keys
    named (the 260712-fractional live-incident shape — a stub meta staged as a
    defect and dead-ended every portal verb). The refusal does NOT lock, so
    the model's corrected re-call stages a complete, publish-ready package."""
    sink = _configure_forge_full(tmp_path)
    ok_files = dict(_FORGE_ARGS["files"])
    out = _emit({"files": ok_files, "meta": {"slug": "260712-acme-pm"}})
    err = out["error"]
    assert "missing required key" in err
    for key in ("company", "role"):
        assert key in err
    assert "row_id" not in err  # P1.1 — never asked of the model
    assert not (sink / "260712-acme-pm").exists()  # nothing staged
    # Corrected call carries a habit-emitted row_id: STRIPPED + recorded, and
    # the staged identity is the HOST's bound value regardless.
    out2 = _emit({"files": ok_files, "meta": {
        "slug": "260712-acme-pm", "company": "Acme", "role": "PM",
        "row_id": "model-habit-value",
    }})
    assert out2["staged"] is True and out2["locked"] is True
    meta = json.loads(
        (sink / "260712-acme-pm" / "meta.json").read_text(encoding="utf-8")
    )
    assert meta["company"] == "Acme" and meta["row_id"] == "pg-1"
    assert fleet_emit_tool.emitted()["stripped_meta_keys"] == ["row_id"]


def test_hf1_live_forge_record_declares_full_floor():
    """The repo DEFINITION pins the floor: the forge record's
    emit.meta.required_keys carries slug + the full publish contract
    (_FORGE_META_REQUIRED), so the tool refuses stub metas at emit time."""
    from grove.capability_registry import load_capabilities

    cap = load_capabilities()["skill.fleet.forge-jobsearch"]
    decl = worker_entry._emit_declaration(cap)
    # fleet-receipt-custody-v1 P1.1 — the model-facing floor is the descriptive
    # triad ONLY: row_id is minted by the runtime and bound at emit, so the
    # model is never asked to name the row it worked on.
    assert decl["meta"]["required_keys"] == ["slug", "company", "role"]
    assert "row_id" not in decl["meta"]["required_keys"]


def test_declarative_synthesized_identity(tmp_path):
    sink = _configure_declarative(tmp_path, "moon-bot")
    out = _emit({"files": {"draft-moon-bot.md": "# Draft\nbody\n"}})
    assert out["staged"] is True and out["slug"] == "moon-bot"
    meta = json.loads((sink / "moon-bot" / "meta.json").read_text(encoding="utf-8"))
    assert meta == {"unit_id": "moon-bot", "slug": "moon-bot"}  # runtime's, not model's
    assert (sink / "moon-bot" / "draft-moon-bot.md").read_text(
        encoding="utf-8"
    ) == "# Draft\nbody\n"


def test_empty_body_and_unconfigured_are_loud(tmp_path):
    assert "not available" in _emit({"files": {"a.md": "x"}})["error"]  # unconfigured
    _configure_declarative(tmp_path, "u1")
    assert "empty or non-string body" in _emit({"files": {"draft-u1.md": "  "}})["error"]


# ── run_worker lifecycle: real run_worker, scripted agent, temp sink ────────


class _FakeSessionDB:
    def __init__(self, *a, **k):
        pass


class _ScriptedAgent:
    """Scripted run_conversation: each entry is a result dict OR a callable
    (invoked with the real emit handler available) returning one. Records
    every call's (prompt, history, max_tokens-at-call)."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.max_tokens = 100

    def run_conversation(self, prompt, conversation_history=None, task_id=None):
        self.calls.append(
            {
                "prompt": prompt,
                "history": conversation_history,
                "max_tokens": self.max_tokens,
            }
        )
        step = self.script.pop(0) if self.script else {"messages": [], "completed": True}
        return step() if callable(step) else step


def _drive_worker(monkeypatch, tmp_path, cap_gov, payload, script, run_id="rid1"):
    """Drive the REAL run_worker through the P1 emit lifecycle. Upstream is
    stubbed at the same clean seams as the fleet-failure-forensics harness;
    the emit tool, staging jail, extraction, and event assembly are REAL."""
    from grove.fleet import paths as _paths

    class _Cap:
        id = "skill.fleet.forge-jobsearch"
        governance = cap_gov
        # binding-governance-surfaces-v1 P4 — see forge-worker harness note.
        class tier_rule:
            preferred = 2
        model_binding = None

    agent = _ScriptedAgent(script)

    class _Dispatcher:
        captured = {}

        def __init__(self, *a, **k):
            _Dispatcher.captured["runtime_ctx"] = k.get("runtime_ctx")
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
        worker_entry, "_derive_skill_name", lambda cap, wid: "fleet/forge-jobsearch"
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
    ev = worker_entry.run_worker("forge", run_id, payload)
    return ev, agent, _Dispatcher.captured


# The REAL dispatch shape: the resolver mints unit_id (resolvers.py:173) and
# the runtime binds it as meta.row_id at emit (fleet-receipt-custody-v1 P1.1).
_ROWS_PAYLOAD = {"rows": [{"id": "r1", "Fit Score": 0.91}], "unit_id": "r1"}
# Legacy pre-unit_id payload — pins the bind's NO-OP path (sentinel byte-parity
# baselines must stay byte-identical when no host identity was dispatched).
_ROWS_PAYLOAD_LEGACY = {"rows": [{"id": "r1", "Fit Score": 0.91}]}


def _emit_step(args):
    """A script step that performs the model's emit_package call for real."""

    def _step():
        res = json.loads(fleet_emit_tool._handle_emit_package(args))
        assert res.get("staged") is True, res
        return {"messages": [{"role": "assistant", "content": "done"}], "completed": True}

    return _step


def test_tool_transport_success_event_from_locked_emit(monkeypatch, tmp_path):
    ev, agent, cap = _drive_worker(
        monkeypatch, tmp_path, _FORGE_GOV, _ROWS_PAYLOAD, [_emit_step(_FORGE_ARGS)]
    )
    assert ev["status"] == "success"
    assert ev["slug"] == "acme-pm"
    assert "transport=tool" in ev["detail"]
    assert ev["row_id"] == "r1" and ev["fit_score"] == 0.91  # identity runtime-bound (P1.1)
    assert ev["stripped_meta_keys"] == ["row_id"]  # A6 telemetry rider
    assert len(ev["staged"]) == 3
    for p in ev["staged"]:
        assert Path(p).is_file()
    assert len(agent.calls) == 1  # no ladder engaged
    # L1 allow-list admitted emit_package for the tool-transport spawn:
    allow = cap["runtime_ctx"].config["fleet_offered_allowlist"]
    assert allow == ["read_file", "skill_view", "emit_package"]
    # and the prompt is the tool contract, sentinel protocol DROPPED:
    assert "emit_package" in agent.calls[0]["prompt"]
    assert "@@@FILE_START" not in agent.calls[0]["prompt"]


def test_no_emit_one_reprompt_then_no_package(monkeypatch, tmp_path):
    prose = {"messages": [{"role": "assistant", "content": "prose only"}], "completed": True}
    ev, agent, _ = _drive_worker(
        monkeypatch, tmp_path, _FORGE_GOV, _ROWS_PAYLOAD, [prose, prose, prose]
    )
    assert ev["status"] == "failed" and ev["check"] == "no_package"
    assert "emit_package was never called" in ev["detail"]
    assert len(agent.calls) == 2, "exactly ONE bounded re-prompt"
    assert "Call emit_package NOW" in agent.calls[1]["prompt"]
    assert agent.calls[1]["history"] == prose["messages"]  # conversation continues


def test_reprompt_recovery_succeeds(monkeypatch, tmp_path):
    prose = {"messages": [{"role": "assistant", "content": "…"}], "completed": True}
    ev, agent, _ = _drive_worker(
        monkeypatch, tmp_path, _FORGE_GOV, _ROWS_PAYLOAD, [prose, _emit_step(_FORGE_ARGS)]
    )
    assert ev["status"] == "success" and len(agent.calls) == 2


_TRUNC = {
    "messages": [{"role": "assistant", "content": None, "finish_reason": "length"}],
    "completed": False,
    "partial": True,
    "error": "Response truncated due to output length limit",
}


def test_truncation_raised_cap_retry_then_success(monkeypatch, tmp_path):
    ev, agent, _ = _drive_worker(
        monkeypatch, tmp_path, _FORGE_GOV, _ROWS_PAYLOAD, [_TRUNC, _emit_step(_FORGE_ARGS)]
    )
    assert ev["status"] == "success"
    assert len(agent.calls) == 2
    # P0 finding 2: identical-at-cap is deterministic — the retry RAISES the cap
    # and re-runs the ORIGINAL prompt fresh (no broken history):
    assert agent.calls[1]["max_tokens"] == 200  # doubled from 100
    assert agent.calls[1]["prompt"] == agent.calls[0]["prompt"]
    assert agent.calls[1]["history"] is None


def test_truncation_exhausted_is_emit_truncation_andon(monkeypatch, tmp_path):
    ev, agent, _ = _drive_worker(
        monkeypatch, tmp_path, _FORGE_GOV, _ROWS_PAYLOAD, [_TRUNC, _TRUNC, _TRUNC, _TRUNC]
    )
    assert ev["status"] == "failed"
    assert ev["check"] == "emit_truncation"  # distinct Andon class, not no_package
    assert "truncation-shaped even after the bounded raised-cap retry" in ev["detail"]
    # bounded ladder: initial + ONE raised-cap + ONE re-prompt = 3 calls, no more
    assert len(agent.calls) == 3
    assert agent.calls[1]["max_tokens"] == 200
    assert ev["raw_text_path"] is not None  # forensics preserved


# ── dual-read pins (GATE-B F6) ──────────────────────────────────────────────


def _sentinel_messages(tag, files):
    blocks = "\n".join(
        f"@@@FILE_START: {name} [{tag}]@@@\n{body}\n@@@FILE_END: {name} [{tag}]@@@"
        for name, body in files.items()
    )
    return [{"role": "assistant", "content": blocks}]


_SENTINEL_FILES = {
    "resume.md": "# Résumé sentinel body",
    "cover-letter.md": "Cover sentinel body",
    # forge-publish-meta-hotfix-v1 P1 — complete publish triad (clean success).
    "meta.json": '{"slug": "acme-pm", "row_id": "r1", "company": "Acme", "role": "PM"}',
}

_SENTINEL_GOV = {
    "emission_preconditions": {
        "terminal_artifact": {
            "tool": "write_file",
            "path_pattern": "*.md",
            "emit": {"transport": "sentinel"},
        }
    }
}


def test_sentinel_transport_stages_identically_to_baseline(monkeypatch, tmp_path):
    """flag=sentinel → the pre-P1 pipeline byte-for-byte: sentinel prompt
    contract offered, 2-tool allow-list, package staged from the delimited
    parse with bodies byte-equal to the parsed fixture.

    P1.2 NOTE: the _ROWS_PAYLOAD_LEGACY shape (no unit_id key) no longer
    occurs in production — both registered resolvers mint unit_id
    unconditionally (resolvers.py:173 notion_query, :675 file_source). This
    pin now covers _bind_identity's defensive no-op branch only (no bound
    identity → staged bytes untouched)."""
    run_id = "rid42"
    msgs = _sentinel_messages(run_id[:8], _SENTINEL_FILES)
    ev, agent, cap = _drive_worker(
        monkeypatch,
        tmp_path,
        _SENTINEL_GOV,
        _ROWS_PAYLOAD_LEGACY,  # no dispatched identity → bind no-op → bytes hold
        [{"messages": msgs, "completed": True}],
        run_id=run_id,
    )
    assert ev["status"] == "success" and ev["slug"] == "acme-pm"
    assert len(agent.calls) == 1  # no emit ladder on the sentinel path
    assert "@@@FILE_START" in agent.calls[0]["prompt"]  # sentinel contract
    assert "emit_package" not in agent.calls[0]["prompt"]
    allow = cap["runtime_ctx"].config["fleet_offered_allowlist"]
    assert allow == ["read_file", "skill_view"]  # baseline surface, unchanged
    sink = tmp_path / "sink" / "acme-pm"
    # byte-diff vs the fixture package (dual-read pin):
    for name, body in _SENTINEL_FILES.items():
        assert (sink / name).read_text(encoding="utf-8") == body
    assert "transport=tool" not in ev["detail"]  # event shape is baseline's


def test_tool_flagged_producer_sentinel_output_still_accepted(monkeypatch, tmp_path):
    """F6 acceptance: a tool-flagged producer that emits sentinel blocks anyway
    (never calling emit_package) is still staged this migration phase — after
    the bounded re-prompt also yields sentinels.

    P1.2 NOTE: the _ROWS_PAYLOAD_LEGACY shape (no unit_id key) no longer
    occurs in production — both registered resolvers mint unit_id
    unconditionally (resolvers.py:173, :675). This pin now covers
    _bind_identity's defensive no-op branch only."""
    run_id = "rid77"
    msgs = {"messages": _sentinel_messages(run_id[:8], _SENTINEL_FILES), "completed": True}
    ev, agent, _ = _drive_worker(
        monkeypatch, tmp_path, _FORGE_GOV, _ROWS_PAYLOAD_LEGACY,  # bind no-op pin
        [msgs, msgs], run_id=run_id,
    )
    assert ev["status"] == "success" and ev["slug"] == "acme-pm"
    assert len(agent.calls) == 2  # ladder re-prompted once, then dual-read accepted
    sink = tmp_path / "sink" / "acme-pm"
    for name, body in _SENTINEL_FILES.items():
        assert (sink / name).read_text(encoding="utf-8") == body


# ── forge-publish-meta-hotfix-v1 P1: emit-time meta-completeness gate ────────


def test_forge_meta_defects_predicate():
    """The defect predicate mirrors the publish endpoint's all(meta.get(k)) check
    and reports missing keys in the declared order — never raises."""
    f = worker_entry._forge_meta_defects
    assert f('{"slug": "s", "company": "Acme", "role": "PM", "row_id": "r1"}') == []
    assert f('{"slug": "s"}') == ["company", "role", "row_id"]  # stub
    assert f('{"slug": "s", "company": "Acme", "row_id": "r1"}') == ["role"]  # partial
    assert f('{"slug": "s", "company": "", "role": "PM", "row_id": "r1"}') == ["company"]  # empty-string is not present
    assert f("not json") == ["company", "role", "row_id"]  # unparseable -> all missing, loud
    assert f(None) == ["company", "role", "row_id"]


# stub = slug only (missing the descriptive pair); partial = one field short
# (role). P1.1 — the defect-belt scenarios keep their own THIN floors: under
# the live floor the tool refuses these at emit (HF-1); the belt covers thin
# records and the staged-meta completeness stamp behind it. row_id is runtime-
# bound on every path now, so it is never among the stamped defects here.
_STUB_FORGE_ARGS = {"files": dict(_FORGE_ARGS["files"]), "meta": {"slug": "acme-pm"}}
_PARTIAL_FORGE_ARGS = {
    "files": dict(_FORGE_ARGS["files"]),
    "meta": {"slug": "acme-pm", "company": "Acme"},
}


def _thin_floor_gov(*keys):
    gov = json.loads(json.dumps(_FORGE_GOV))  # deep copy
    gov["emission_preconditions"]["terminal_artifact"]["emit"]["meta"][
        "required_keys"
    ] = list(keys)
    return gov


def test_forge_full_meta_stages_clean_no_defect(monkeypatch, tmp_path):
    """A complete triad stages a clean success — meta_defect is None."""
    ev, _agent, _ = _drive_worker(
        monkeypatch, tmp_path, _FORGE_GOV, _ROWS_PAYLOAD, [_emit_step(_FORGE_ARGS)]
    )
    assert ev["status"] == "success"
    assert ev["meta_defect"] is None
    assert "meta_defect=" not in ev["detail"]


def test_forge_stub_meta_stages_with_defect_and_forensics(monkeypatch, tmp_path):
    """A stub meta (slug only) STILL stages (surface-regardless) but rides a
    meta_defect marker naming the whole missing triad, appends it to detail, and
    persists the raw output to the forensic sidecar."""
    ev, _agent, _ = _drive_worker(
        monkeypatch, tmp_path, _thin_floor_gov("slug"), _ROWS_PAYLOAD,
        [_emit_step(_STUB_FORGE_ARGS)],
    )
    assert ev["status"] == "success"  # NEVER un-staged
    assert len(ev["staged"]) == 3 and all(Path(p).is_file() for p in ev["staged"])
    # P1.1: row_id is runtime-bound (dispatched unit r1), so the stub's defect
    # set is the descriptive pair only — identity can no longer be a defect.
    assert ev["meta_defect"] == "missing:company,role"
    assert "meta_defect=missing:company,role" in ev["detail"]
    assert ev["raw_text_path"] is not None  # forensics attached


def test_forge_partial_meta_one_field_missing_is_defect(monkeypatch, tmp_path):
    """One field short (role) is treated exactly like a stub — staged + marked."""
    ev, _agent, _ = _drive_worker(
        monkeypatch, tmp_path, _thin_floor_gov("slug", "company"), _ROWS_PAYLOAD,
        [_emit_step(_PARTIAL_FORGE_ARGS)],
    )
    assert ev["status"] == "success"
    assert len(ev["staged"]) == 3
    assert ev["meta_defect"] == "missing:role"
    assert "meta_defect=missing:role" in ev["detail"]
    assert ev["raw_text_path"] is not None


# ── truncation-shape + finish-reason fold pins ──────────────────────────────


def test_is_truncation_result_shapes():
    f = worker_entry._is_truncation_result
    assert f(_TRUNC) is True
    assert f({"error": "Response remained truncated after 3 continuation attempts"}) is True
    assert (
        f({"messages": [{"role": "assistant", "content": "x", "finish_reason": "length"}]})
        is True
    )
    assert f({"messages": [{"role": "assistant", "finish_reason": "stop"}]}) is False
    assert f({"messages": [], "completed": True}) is False
    assert f(None) is False


def test_effective_finish_reason_folds_native_truth():
    """P0 finding 1 pin: OpenRouter rewrites a cap-hit to finish_reason=
    'tool_calls'; the native_finish_reason='length' extra field is truth and
    must mark the response truncated EVEN when the args-shape heuristic
    misses (truncation that happens to parse)."""
    from run_agent import _effective_finish_reason as f

    assert f("tool_calls", False, "length") == "length"  # the lie, corrected
    assert f("stop", False, "length") == "length"
    assert f("tool_calls", True, None) == "length"  # args-shape heuristic
    assert f("tool_calls", False, None) == "tool_calls"
    assert f("stop", False, "stop") == "stop"
    assert f(None, False, None) == "stop"
    assert f("length", False, None) == "length"


# ── post-staging quality gate (drafter-quality-checks-v1 P3) ────────────────
#
# The ONE transport-agnostic gate site: staged outcome → evaluate → (on fail)
# one governed redraft → success event with the four rider fields. The gate
# informs disposition; it never withholds staged work.


_QUALITY_GATE = {
    "rubric_version": "1.0",
    "criteria": ["specific claim", "grounded evidence"],
    "threshold": 0.7,
    "redraft_limit": 1,
    "evaluator_tier": "T1",
}

_GATED_GOV = {
    "write_zone": {
        "staging_dir": "prod/pending_review",
        "canonical_dir": "prod",
        "retention": {"policy": "persist", "archive_dir": ".archive"},
    },
    "emission_preconditions": {
        "terminal_artifact": {
            "tool": "write_file",
            "path_pattern": "draft-*.md",
            "emit": {"transport": "tool"},
        }
    },
    "quality_gate": dict(_QUALITY_GATE),
}

_UNIT_PAYLOAD = {"units": [1], "unit_id": "u1", "source_path": "/s", "source_name": "s"}
_DRAFT1_ARGS = {"files": {"draft-u1.md": "# Draft one\nfirst body\n"}}
_DRAFT2_ARGS = {"files": {"draft-u1.md": "# Draft two\nrevised body\n"}}

_QUALITY_KEYS = ("quality_score", "rubric_version", "redraft_count", "evaluator_model")


def _verdict(status, score, issues=(), model="stub/eval-model"):
    return {
        "status": status,
        "quality_score": score,
        "complete": status == "pass",
        "accurate": status == "pass",
        "issues": list(issues),
        "rubric_version": "1.0",
        "threshold": 0.7,
        "evaluator_tier": "T1",
        "evaluator_model": None if status == "skipped_oversize" else model,
        "context_keys_used": [],
        "context_keys_missing": [],
        "detail": "",
    }


def _stub_evaluator(monkeypatch, tmp_path, verdicts):
    """Stub evaluate_draft with a scripted verdict sequence; capture calls.
    Also pins GROVE home to tmp so the archive helper resolves there."""
    calls = []
    seq = list(verdicts)

    def fake_evaluate(record, staged_files, task_context=None):
        calls.append({"files": dict(staged_files), "task_context": task_context})
        v = seq.pop(0)
        if isinstance(v, Exception):
            raise v
        return dict(v)

    monkeypatch.setattr("grove.fleet.quality.evaluate_draft", fake_evaluate)
    monkeypatch.setattr(
        "grove.utils.fs_utils._grove_home_realpath", lambda: str(tmp_path)
    )
    return calls


def test_gated_pass_rides_event(monkeypatch, tmp_path):
    calls = _stub_evaluator(monkeypatch, tmp_path, [_verdict("pass", 0.85)])
    ev, agent, _ = _drive_worker(
        monkeypatch, tmp_path, _GATED_GOV, _UNIT_PAYLOAD, [_emit_step(_DRAFT1_ARGS)]
    )
    assert ev["status"] == "success"
    assert ev["quality_score"] == 0.85
    assert ev["rubric_version"] == "1.0"
    assert ev["redraft_count"] == 0
    assert ev["evaluator_model"] == "stub/eval-model"
    # detail is byte-identical to the ungated shape on a pass:
    assert ev["detail"] == "completed=True; unit=u1; transport=tool"
    assert len(agent.calls) == 1  # no redraft turn
    # the evaluator saw draft content, NEVER the identity envelope:
    assert "meta.json" not in calls[0]["files"]
    assert calls[0]["files"]["draft-u1.md"].startswith("# Draft one")
    # criteria-only record → no task context (A1):
    assert calls[0]["task_context"] is None


def test_ungated_event_carries_four_null_keys(monkeypatch, tmp_path):
    def forbidden(*a, **k):
        raise AssertionError("evaluator must not run for an ungated record")

    monkeypatch.setattr("grove.fleet.quality.evaluate_draft", forbidden)
    ev, _, _ = _drive_worker(
        monkeypatch, tmp_path, _FORGE_GOV, _ROWS_PAYLOAD, [_emit_step(_FORGE_ARGS)]
    )
    assert ev["status"] == "success"
    for key in _QUALITY_KEYS:
        assert key in ev and ev[key] is None, key


def test_gated_fail_redrafts_once_and_lock_reengages(monkeypatch, tmp_path):
    calls = _stub_evaluator(
        monkeypatch,
        tmp_path,
        [_verdict("fail", 0.4, issues=["issue-A verbatim", "issue-B verbatim"]),
         _verdict("pass", 0.9)],
    )
    ev, agent, _ = _drive_worker(
        monkeypatch,
        tmp_path,
        _GATED_GOV,
        _UNIT_PAYLOAD,
        [_emit_step(_DRAFT1_ARGS), _emit_step(_DRAFT2_ARGS)],
    )
    assert ev["status"] == "success"
    assert ev["quality_score"] == 0.9
    assert ev["redraft_count"] == 1
    assert "; redrafted draft1_archived=" in ev["detail"]
    # (c) the redraft re-prompt: fresh continuation carrying the issues
    # verbatim, authorizing exactly one further emit.
    assert len(agent.calls) == 2
    redraft_call = agent.calls[1]
    assert "issue-A verbatim" in redraft_call["prompt"]
    assert "issue-B verbatim" in redraft_call["prompt"]
    assert "EXACTLY ONE more time" in redraft_call["prompt"]
    assert redraft_call["history"] is not None  # emit-ladder continuation shape
    # (a) draft #1 archived to the write_zone archive location (never overwrite):
    archived = list((tmp_path / "prod" / ".archive").glob("u1-*"))
    assert len(archived) == 1
    assert (archived[0] / "draft-u1.md").read_text(encoding="utf-8").startswith(
        "# Draft one"
    )
    # (d) the re-staged package is draft #2:
    staged = tmp_path / "sink" / "u1" / "draft-u1.md"
    assert staged.read_text(encoding="utf-8").startswith("# Draft two")
    # both evaluations saw the respective drafts:
    assert calls[0]["files"]["draft-u1.md"].startswith("# Draft one")
    assert calls[1]["files"]["draft-u1.md"].startswith("# Draft two")
    # R-B4 post-condition: the lock re-engaged on the redraft emit — a THIRD
    # emit_package call hits the lock refusal.
    third = json.loads(fleet_emit_tool._handle_emit_package(_DRAFT1_ARGS))
    assert "already emitted and locked" in third["error"]


def test_gated_fail_after_redraft_still_proceeds(monkeypatch, tmp_path):
    """(e) proceed regardless — a second failing verdict still succeeds the
    run, final score attached."""
    _stub_evaluator(
        monkeypatch, tmp_path,
        [_verdict("fail", 0.4, issues=["x"]), _verdict("fail", 0.5, issues=["y"])],
    )
    ev, agent, _ = _drive_worker(
        monkeypatch, tmp_path, _GATED_GOV, _UNIT_PAYLOAD,
        [_emit_step(_DRAFT1_ARGS), _emit_step(_DRAFT2_ARGS)],
    )
    assert ev["status"] == "success"
    assert ev["quality_score"] == 0.5
    assert ev["redraft_count"] == 1
    assert len(agent.calls) == 2  # exactly ONE redraft cycle, no second loop


def test_redraft_no_package_restores_draft1(monkeypatch, tmp_path):
    """A redraft that never re-emits restores draft #1 from the archive and
    proceeds on the ORIGINAL verdict — the gate never withholds work."""
    _stub_evaluator(
        monkeypatch, tmp_path, [_verdict("fail", 0.4, issues=["z"])]
    )
    prose = {"messages": [{"role": "assistant", "content": "sorry"}], "completed": True}
    ev, agent, _ = _drive_worker(
        monkeypatch, tmp_path, _GATED_GOV, _UNIT_PAYLOAD,
        [_emit_step(_DRAFT1_ARGS), prose],
    )
    assert ev["status"] == "success"
    assert ev["quality_score"] == 0.4  # original verdict rides
    assert ev["redraft_count"] == 1
    assert "; redraft_no_package draft1_restored" in ev["detail"]
    # draft #1 is back in the staging sink; the archive slot is empty again:
    staged = tmp_path / "sink" / "u1" / "draft-u1.md"
    assert staged.read_text(encoding="utf-8").startswith("# Draft one")
    assert list((tmp_path / "prod" / ".archive").glob("u1-*")) == []


def test_skipped_oversize_proceeds_with_null_score(monkeypatch, tmp_path):
    _stub_evaluator(monkeypatch, tmp_path, [_verdict("skipped_oversize", None)])
    ev, agent, _ = _drive_worker(
        monkeypatch, tmp_path, _GATED_GOV, _UNIT_PAYLOAD, [_emit_step(_DRAFT1_ARGS)]
    )
    assert ev["status"] == "success"
    assert ev["quality_score"] is None
    assert ev["rubric_version"] == "1.0"  # gated-and-skipped ≠ ungated
    assert ev["redraft_count"] == 0
    assert ev["evaluator_model"] is None
    assert len(agent.calls) == 1  # skip never redrafts


def test_evaluator_exception_is_loud_andon(monkeypatch, tmp_path):
    from grove.fleet.errors import FleetWorkerAndon

    _stub_evaluator(monkeypatch, tmp_path, [RuntimeError("provider 500")])
    with pytest.raises(FleetWorkerAndon) as exc_info:
        _drive_worker(
            monkeypatch, tmp_path, _GATED_GOV, _UNIT_PAYLOAD,
            [_emit_step(_DRAFT1_ARGS)],
        )
    assert exc_info.value.check == "evaluator_call_failed"
    assert "provider 500" in str(exc_info.value)


def test_context_inputs_resolved_from_dispatch_payload(monkeypatch, tmp_path):
    """A1 (R-A12): the gate resolves context_inputs against the dispatch
    payload and passes the PRESENT subset; a missing key never fails the run."""
    gov = {
        **_GATED_GOV,
        "quality_gate": dict(_QUALITY_GATE, context_inputs=["angle", "not_in_payload"]),
    }
    calls = _stub_evaluator(monkeypatch, tmp_path, [_verdict("pass", 0.8)])
    payload = dict(_UNIT_PAYLOAD, angle="contrarian take")
    ev, _, _ = _drive_worker(
        monkeypatch, tmp_path, gov, payload, [_emit_step(_DRAFT1_ARGS)]
    )
    assert ev["status"] == "success"
    assert calls[0]["task_context"] == {"angle": "contrarian take"}


def test_sentinel_gated_worker_evaluates_too(monkeypatch, tmp_path):
    """Transport-agnostic (R-A2): the SAME gate site fires on a sentinel
    producer's staged package."""
    gov = {**_SENTINEL_GOV, "quality_gate": dict(_QUALITY_GATE)}
    run_id = "rid55"
    msgs = _sentinel_messages(run_id[:8], _SENTINEL_FILES)
    calls = _stub_evaluator(monkeypatch, tmp_path, [_verdict("pass", 0.75)])
    ev, _, _ = _drive_worker(
        monkeypatch, tmp_path, gov, _ROWS_PAYLOAD,
        [{"messages": msgs, "completed": True}], run_id=run_id,
    )
    assert ev["status"] == "success" and ev["slug"] == "acme-pm"
    assert ev["quality_score"] == 0.75
    assert ev["redraft_count"] == 0
    # identity envelope excluded on the sentinel path too:
    assert "meta.json" not in calls[0]["files"]
    assert set(calls[0]["files"]) == {"resume.md", "cover-letter.md"}


# ── dead_pinned_slug classification (binding-telemetry-v1 P3) ────────────────
#
# Conservative signature matrix at main()'s uncaught chokepoint: ONLY the
# unambiguous provider model-does-not-exist class is classified; every
# ambiguity (routing artifacts, 5xx, timeouts, generic 400s) stays generic.


class _FakeStatusError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code


@pytest.mark.parametrize(
    "message,status,expected",
    [
        # UNAMBIGUOUS — classified:
        ("The model `x/dead` does not exist", 404, "dead_pinned_slug"),
        ("x/dead is not a valid model ID", 400, "dead_pinned_slug"),
        ("Model not found: x/dead", 404, "dead_pinned_slug"),
        ("no such model 'x/dead'", 400, "dead_pinned_slug"),
        ("Unknown model x/dead", 404, "dead_pinned_slug"),
        # AMBIGUOUS — untouched:
        ("No endpoints found for x/dead", 404, "uncaught"),   # routing/retention artifact
        ("model not found", 502, "uncaught"),                  # wrong status class
        ("model not found", None, "uncaught"),                 # no status (timeout/conn)
        ("invalid request: bad temperature", 400, "uncaught"), # generic 400
        ("The resource does not exist", 404, "uncaught"),      # no 'model' in message
        ("upstream connect error", 503, "uncaught"),
    ],
)
def test_dead_pinned_slug_signature_matrix(message, status, expected):
    exc = _FakeStatusError(message, status_code=status)
    assert worker_entry._uncaught_check(exc) == expected


def test_stamped_check_always_wins():
    from grove.fleet.errors import FleetWorkerAndon

    exc = FleetWorkerAndon("boom", check="evaluator_call_failed")
    assert worker_entry._uncaught_check(exc) == "evaluator_call_failed"
    plain = RuntimeError("kaboom")
    assert worker_entry._uncaught_check(plain) == "uncaught"


def test_main_event_carries_dead_pinned_slug(monkeypatch, tmp_path):
    """Drive main() whole: an unambiguous provider 404 out of run_worker →
    the terminal event carries check=dead_pinned_slug + traceback."""
    from grove.fleet import paths as _paths

    monkeypatch.setattr(_paths, "get_hermes_home", lambda: str(tmp_path))
    monkeypatch.setattr(
        worker_entry, "_read_inbox_payload", lambda w, r: {"rows": []}
    )

    def boom(worker_id, run_id, payload):
        raise _FakeStatusError(
            "The model `prov/dead-slug` does not exist", status_code=404
        )

    monkeypatch.setattr(worker_entry, "run_worker", boom)
    rc = worker_entry.main(["--worker-id", "forge", "--run-id", "deadrun1"])
    assert rc == 1
    ev = json.loads(_paths.event_path("forge", "deadrun1").read_text())
    assert ev["status"] == "failed"
    assert ev["check"] == "dead_pinned_slug"
    assert "dead-slug" in ev["detail"]
    assert ev["quality_score"] is None  # rider keys present on failed shape too
