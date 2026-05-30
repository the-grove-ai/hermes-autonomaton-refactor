"""Sprint 32.x bugfix — executor MUST thread tool args into completion callbacks.

The ``log_tool_complete_line`` and ``tool_display_close`` callbacks in
``ObservabilityCallbacks`` now take ``(idx, name, args, result, duration)``
and ``(name, args, result, duration)`` respectively. Pre-bugfix the args
slot was missing; downstream consumers (``run_agent._log_tool_complete_line``)
fell back to ``{}`` and the display layer rendered ``? 0.0s`` placeholders
that looked like a silent tool failure.

These tests lock the contract by inspecting the callback signature via
``inspect`` so a future refactor that drops args fails loud.
"""

from __future__ import annotations

import inspect
import typing

from grove.tool_executor import ObservabilityCallbacks


def _resolve_callback_param_types(field_name: str) -> tuple:
    """Resolve ``Optional[Callable[[...], None]]`` on a dataclass field
    to the inner positional-args type tuple. ``__future__ annotations``
    keeps the dataclass field types as strings, so direct
    ``get_args(field.type)`` fails — use ``get_type_hints`` which
    evaluates forward refs against the module's namespace."""
    hints = typing.get_type_hints(ObservabilityCallbacks)
    optional_callable = hints[field_name]
    # Optional[X] is Union[X, None]
    callable_type, _none = typing.get_args(optional_callable)
    # Callable[[a, b, c], R] → ([a, b, c], R)
    callable_args = typing.get_args(callable_type)
    return callable_args[0]


def test_log_tool_complete_line_signature_carries_args():
    """The callback type alias accepts (int, str, dict, Any, float).
    Args dict is the third positional param."""
    param_types = _resolve_callback_param_types("log_tool_complete_line")
    assert len(param_types) == 5, (
        f"log_tool_complete_line MUST accept 5 positional args "
        f"(idx, name, args, result, duration); got {param_types}"
    )
    # Third positional must be `dict` — the args dict the LLM emitted.
    assert param_types[2] is dict, (
        f"log_tool_complete_line third param MUST be `dict` (the "
        f"tool args dict); got {param_types[2]}. Sprint 32.x bugfix "
        f"required this so the downstream cute-message renderer can "
        f"show the real action / target / content instead of `?`."
    )


def test_tool_display_close_signature_carries_args():
    """The callback type alias accepts (str, dict, Any, float).
    Args dict is the second positional param."""
    param_types = _resolve_callback_param_types("tool_display_close")
    assert len(param_types) == 4, (
        f"tool_display_close MUST accept 4 positional args "
        f"(name, args, result, duration); got {param_types}"
    )
    assert param_types[1] is dict, (
        f"tool_display_close second param MUST be `dict` (the "
        f"tool args dict); got {param_types[1]}. Sprint 32.x bugfix."
    )


def test_run_agent_completion_callbacks_accept_args():
    """The run_agent.py callback methods MUST accept ``function_args``
    so the executor's invocations land cleanly."""
    from run_agent import AIAgent

    for method_name in (
        "_log_tool_complete_line",
        "_log_tool_complete_line_sequential",
        "_tool_display_close",
    ):
        method = getattr(AIAgent, method_name)
        sig = inspect.signature(method)
        param_names = list(sig.parameters.keys())
        assert "function_args" in param_names, (
            f"AIAgent.{method_name} MUST accept `function_args` "
            f"after Sprint 32.x; got {param_names}"
        )
