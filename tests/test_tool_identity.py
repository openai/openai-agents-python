"""Unit tests for src/agents/_tool_identity.py pure helpers.

These cover the small, pure functions in `_tool_identity` that build /
parse function-tool lookup keys and trace names. The module had no
direct test file even though it's imported across the runner, tracing,
and tool-output trimmer code paths.
"""

from __future__ import annotations

from typing import Any

import pytest

from agents import FunctionTool, function_tool
from agents._tool_identity import (
    deserialize_function_tool_lookup_key,
    get_function_tool_lookup_key,
    get_tool_call_name,
    get_tool_call_namespace,
    get_tool_call_qualified_name,
    get_tool_call_trace_name,
    is_reserved_synthetic_tool_namespace,
    serialize_function_tool_lookup_key,
    tool_qualified_name,
    tool_trace_name,
)
from agents.exceptions import UserError


class TestToolQualifiedName:
    def test_returns_name_when_no_namespace(self) -> None:
        assert tool_qualified_name("search") == "search"

    def test_returns_dotted_when_namespace_provided(self) -> None:
        assert tool_qualified_name("search", "tools") == "tools.search"

    def test_returns_none_for_empty_name(self) -> None:
        assert tool_qualified_name("") is None
        assert tool_qualified_name(None) is None

    def test_returns_none_for_non_string_name(self) -> None:
        assert tool_qualified_name(123) is None  # type: ignore[arg-type]

    def test_ignores_empty_namespace(self) -> None:
        assert tool_qualified_name("search", "") == "search"
        assert tool_qualified_name("search", None) == "search"


class TestIsReservedSyntheticToolNamespace:
    def test_true_when_name_equals_namespace(self) -> None:
        assert is_reserved_synthetic_tool_namespace("search", "search") is True

    def test_false_when_different(self) -> None:
        assert is_reserved_synthetic_tool_namespace("search", "tools") is False

    def test_false_when_either_missing(self) -> None:
        assert is_reserved_synthetic_tool_namespace("", "") is False
        assert is_reserved_synthetic_tool_namespace("search", None) is False
        assert is_reserved_synthetic_tool_namespace(None, "search") is False


class TestToolTraceName:
    def test_collapses_synthetic_namespace(self) -> None:
        # When namespace == name, trace name is just the bare name.
        assert tool_trace_name("search", "search") == "search"

    def test_qualifies_real_namespace(self) -> None:
        assert tool_trace_name("search", "tools") == "tools.search"

    def test_returns_bare_when_no_namespace(self) -> None:
        assert tool_trace_name("search", None) == "search"


class TestToolCallExtractors:
    def test_get_tool_call_name_from_dict(self) -> None:
        assert get_tool_call_name({"name": "search"}) == "search"

    def test_get_tool_call_name_from_object(self) -> None:
        class Call:
            name = "search"

        assert get_tool_call_name(Call()) == "search"

    def test_get_tool_call_name_returns_none_for_empty(self) -> None:
        assert get_tool_call_name({"name": ""}) is None
        assert get_tool_call_name({}) is None
        assert get_tool_call_name({"name": 123}) is None

    def test_get_tool_call_namespace(self) -> None:
        assert get_tool_call_namespace({"namespace": "tools"}) == "tools"
        assert get_tool_call_namespace({"namespace": ""}) is None
        assert get_tool_call_namespace({}) is None

    def test_get_tool_call_qualified_name_with_namespace(self) -> None:
        call = {"name": "search", "namespace": "tools"}
        assert get_tool_call_qualified_name(call) == "tools.search"

    def test_get_tool_call_qualified_name_without_namespace(self) -> None:
        assert get_tool_call_qualified_name({"name": "search"}) == "search"

    def test_get_tool_call_trace_name_collapses_synthetic_namespace(self) -> None:
        call = {"name": "search", "namespace": "search"}
        assert get_tool_call_trace_name(call) == "search"

    def test_get_tool_call_trace_name_qualifies_real_namespace(self) -> None:
        call = {"name": "search", "namespace": "tools"}
        assert get_tool_call_trace_name(call) == "tools.search"


class TestGetFunctionToolLookupKey:
    def test_bare_when_no_namespace(self) -> None:
        assert get_function_tool_lookup_key("search") == ("bare", "search")

    def test_namespaced_when_namespace_present(self) -> None:
        assert get_function_tool_lookup_key("search", "tools") == (
            "namespaced",
            "tools",
            "search",
        )

    def test_deferred_top_level_when_namespace_equals_name(self) -> None:
        assert get_function_tool_lookup_key("search", "search") == (
            "deferred_top_level",
            "search",
        )

    def test_returns_none_for_empty_name(self) -> None:
        assert get_function_tool_lookup_key("") is None
        assert get_function_tool_lookup_key(None) is None


class TestSerializeRoundTrip:
    @pytest.mark.parametrize(
        "lookup_key",
        [
            ("bare", "search"),
            ("namespaced", "tools", "search"),
            ("deferred_top_level", "search"),
        ],
    )
    def test_roundtrip(self, lookup_key) -> None:
        serialized = serialize_function_tool_lookup_key(lookup_key)
        assert serialized is not None
        assert deserialize_function_tool_lookup_key(serialized) == lookup_key

    def test_serialize_none_returns_none(self) -> None:
        assert serialize_function_tool_lookup_key(None) is None

    def test_deserialize_invalid_returns_none(self) -> None:
        assert deserialize_function_tool_lookup_key(None) is None
        assert deserialize_function_tool_lookup_key({}) is None
        assert deserialize_function_tool_lookup_key({"kind": "bare"}) is None
        assert deserialize_function_tool_lookup_key({"kind": "bare", "name": ""}) is None
        assert deserialize_function_tool_lookup_key({"kind": "unknown", "name": "x"}) is None
        # namespaced kind requires a non-empty namespace
        assert deserialize_function_tool_lookup_key({"kind": "namespaced", "name": "x"}) is None


def test_validate_function_tool_namespace_shape_rejects_synthetic() -> None:
    """The internal validator must refuse synthetic name==namespace shapes."""
    from agents._tool_identity import validate_function_tool_namespace_shape

    # Valid shapes don't raise.
    validate_function_tool_namespace_shape("search", "tools")
    validate_function_tool_namespace_shape("search", None)

    # The reserved synthetic shape (name == namespace) is rejected.
    with pytest.raises(UserError, match="reserves the synthetic namespace"):
        validate_function_tool_namespace_shape("search", "search")


class TestFunctionToolNameValidation:
    """Explicit tool names are rejected; derived names are sanitized instead."""

    @pytest.mark.parametrize(
        "name",
        [
            "my tool!",  # space and punctuation
            "выборка",  # non-ASCII
            "x" * 65,  # over the 64 character API limit
        ],
    )
    def test_explicit_name_is_rejected(self, name: str) -> None:
        with pytest.raises(UserError, match="Invalid function tool name"):

            @function_tool(name_override=name)
            def f(a: str) -> str:
                """F."""
                return a

    def test_function_tool_dataclass_rejects_explicit_name(self) -> None:
        async def on_invoke(ctx: Any, args: str) -> str:
            return "ok"

        with pytest.raises(UserError, match="Invalid function tool name"):
            FunctionTool(
                name="my tool!",
                description="d",
                params_json_schema={"type": "object", "properties": {}},
                on_invoke_tool=on_invoke,
            )

    @pytest.mark.parametrize("name", ["search", "search-v2", "tools.search", "a" * 64])
    def test_valid_explicit_names_pass(self, name: str) -> None:
        @function_tool(name_override=name)
        def f(a: str) -> str:
            """F."""
            return a

        assert f.name == name

    def test_derived_lambda_name_is_sanitized_not_rejected(self) -> None:
        assert function_tool(lambda: "ok").name == "_lambda_"

    def test_derived_over_long_name_is_shortened(self) -> None:
        namespace: dict[str, Any] = {}
        exec("def " + "b" * 100 + '(a: str) -> str:\n    """F."""\n    return a', namespace)

        name = function_tool(namespace["b" * 100]).name
        assert len(name) == 64
        assert name.startswith("b" * 55)

    def test_valid_derived_name_is_untouched(self) -> None:
        def MyTool(a: str) -> str:  # noqa: N802 - case must survive sanitization
            """F."""
            return a

        assert function_tool(MyTool).name == "MyTool"
