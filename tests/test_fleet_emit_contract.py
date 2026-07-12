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
    "meta": {"required_keys": ["slug"]},
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
        meta_required_keys=["slug"],
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
        ["slug"],
        None,
        None,
    )
    _configure_forge(tmp_path)
    fn = _registered_function_block()
    assert fn == fleet_emit_tool.build_schema(expected, meta_keys)
    # Declaration facts surface in the schema itself:
    assert fn["parameters"]["properties"]["files"]["required"] == expected
    assert fn["parameters"]["properties"]["files"]["additionalProperties"] is False
    assert fn["parameters"]["properties"]["meta"]["required"] == ["slug"]
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
    "meta": {"slug": "acme-pm", "row_id": "r1", "company": "Acme"},
}


def test_lock_on_emit_stages_bytes_then_refuses_second(tmp_path):
    sink = _configure_forge(tmp_path)
    out = _emit(_FORGE_ARGS)
    assert out["staged"] is True and out["locked"] is True and out["slug"] == "acme-pm"
    # byte-exact staging through the SAME jailed stage_package primitive:
    for name, body in _FORGE_ARGS["files"].items():
        assert (sink / "acme-pm" / name).read_text(encoding="utf-8") == body
    meta_disk = json.loads((sink / "acme-pm" / "meta.json").read_text(encoding="utf-8"))
    assert meta_disk == _FORGE_ARGS["meta"]
    emitted = fleet_emit_tool.emitted()
    assert emitted["slug"] == "acme-pm" and len(emitted["staged"]) == 3

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
    assert "safe slug" in _emit({"files": ok_files, "meta": {"slug": "../up"}})["error"]
    # identity travels as the meta ARG, not a meta.json file:
    with_meta_file = {**ok_files, "meta.json": "{}"}
    out = _emit({"files": with_meta_file, "meta": {"slug": "s1"}})
    assert "unexpected file(s)" in out["error"] and "meta" in out["error"]


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


_ROWS_PAYLOAD = {"rows": [{"id": "r1", "Fit Score": 0.91}]}


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
    assert ev["row_id"] == "r1" and ev["fit_score"] == 0.91  # identity from meta arg
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
    "meta.json": '{"slug": "acme-pm", "row_id": "r1"}',
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
    parse with bodies byte-equal to the parsed fixture."""
    run_id = "rid42"
    msgs = _sentinel_messages(run_id[:8], _SENTINEL_FILES)
    ev, agent, cap = _drive_worker(
        monkeypatch,
        tmp_path,
        _SENTINEL_GOV,
        _ROWS_PAYLOAD,
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
    the bounded re-prompt also yields sentinels."""
    run_id = "rid77"
    msgs = {"messages": _sentinel_messages(run_id[:8], _SENTINEL_FILES), "completed": True}
    ev, agent, _ = _drive_worker(
        monkeypatch, tmp_path, _FORGE_GOV, _ROWS_PAYLOAD, [msgs, msgs], run_id=run_id
    )
    assert ev["status"] == "success" and ev["slug"] == "acme-pm"
    assert len(agent.calls) == 2  # ladder re-prompted once, then dual-read accepted
    sink = tmp_path / "sink" / "acme-pm"
    for name, body in _SENTINEL_FILES.items():
        assert (sink / name).read_text(encoding="utf-8") == body


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
