from __future__ import annotations

import asyncio
import copy
import dataclasses
import functools
import inspect
import json
import operator
import sys
from collections.abc import Callable
from types import ModuleType
from typing import Annotated, Any, Generic, TypeVar, cast

import pytest
from inline_snapshot import snapshot
from pydantic import BaseModel
from typing_extensions import Self

from agents import Agent, FunctionTool, UserError, function_tool
from agents.decorators import tool
from agents.run_context import RunContextWrapper
from agents.tool_context import ToolContext


class DummyContext:
    def __init__(self):
        self.data = "something"


def ctx_wrapper() -> ToolContext[DummyContext]:
    return ToolContext(
        context=DummyContext(), tool_name="dummy", tool_call_id="1", tool_arguments=""
    )


CallableValueT = TypeVar("CallableValueT")


@function_tool
def sync_no_context_no_args() -> str:
    return "test_1"


@pytest.mark.asyncio
async def test_sync_no_context_no_args_invocation():
    tool = sync_no_context_no_args
    output = await tool.on_invoke_tool(ctx_wrapper(), "")
    assert output == "test_1"


@function_tool
def sync_no_context_with_args(a: int, b: int) -> int:
    return a + b


@pytest.mark.asyncio
async def test_sync_no_context_with_args_invocation():
    tool = sync_no_context_with_args
    input_data = {"a": 5, "b": 7}
    output = await tool.on_invoke_tool(ctx_wrapper(), json.dumps(input_data))
    assert int(output) == 12


@function_tool
def sync_with_context(ctx: ToolContext[DummyContext], name: str) -> str:
    return f"{name}_{ctx.context.data}"


@pytest.mark.asyncio
async def test_sync_with_context_invocation():
    tool = sync_with_context
    input_data = {"name": "Alice"}
    output = await tool.on_invoke_tool(ctx_wrapper(), json.dumps(input_data))
    assert output == "Alice_something"


@function_tool
async def async_no_context(a: int, b: int) -> int:
    await asyncio.sleep(0)  # Just to illustrate async
    return a * b


@pytest.mark.asyncio
async def test_async_no_context_invocation():
    tool = async_no_context
    input_data = {"a": 3, "b": 4}
    output = await tool.on_invoke_tool(ctx_wrapper(), json.dumps(input_data))
    assert int(output) == 12


@function_tool
async def async_with_context(ctx: ToolContext[DummyContext], prefix: str, num: int) -> str:
    await asyncio.sleep(0)
    return f"{prefix}-{num}-{ctx.context.data}"


@pytest.mark.asyncio
async def test_async_with_context_invocation():
    tool = async_with_context
    input_data = {"prefix": "Value", "num": 42}
    output = await tool.on_invoke_tool(ctx_wrapper(), json.dumps(input_data))
    assert output == "Value-42-something"


@function_tool(name_override="my_custom_tool", description_override="custom desc")
def sync_no_context_override() -> str:
    return "override_result"


@pytest.mark.asyncio
async def test_sync_no_context_override_invocation():
    tool = sync_no_context_override
    assert tool.name == "my_custom_tool"
    assert tool.description == "custom desc"
    output = await tool.on_invoke_tool(ctx_wrapper(), "")
    assert output == "override_result"


@function_tool(failure_error_function=None)
def will_fail_on_bad_json(x: int) -> int:
    return x * 2  # pragma: no cover


@pytest.mark.asyncio
async def test_error_on_invalid_json():
    tool = will_fail_on_bad_json
    # Passing an invalid JSON string
    with pytest.raises(Exception) as exc_info:
        await tool.on_invoke_tool(ctx_wrapper(), "{not valid json}")
    assert "Invalid JSON input for tool" in str(exc_info.value)


def sync_error_handler(ctx: RunContextWrapper[Any], error: Exception) -> str:
    return f"error_{error.__class__.__name__}"


@function_tool(failure_error_function=sync_error_handler)
def will_not_fail_on_bad_json(x: int) -> int:
    return x * 2  # pragma: no cover


@pytest.mark.asyncio
async def test_no_error_on_invalid_json():
    tool = will_not_fail_on_bad_json
    # Passing an invalid JSON string
    result = await tool.on_invoke_tool(ctx_wrapper(), "{not valid json}")
    assert result == "error_ModelBehaviorError"


def async_error_handler(ctx: RunContextWrapper[Any], error: Exception) -> str:
    return f"error_{error.__class__.__name__}"


@function_tool(failure_error_function=sync_error_handler)
def will_not_fail_on_bad_json_async(x: int) -> int:
    return x * 2  # pragma: no cover


@pytest.mark.asyncio
async def test_no_error_on_invalid_json_async():
    tool = will_not_fail_on_bad_json_async
    result = await tool.on_invoke_tool(ctx_wrapper(), "{not valid json}")
    assert result == "error_ModelBehaviorError"


@function_tool(defer_loading=True)
def deferred_lookup(customer_id: str) -> str:
    return customer_id


def test_function_tool_defer_loading():
    assert deferred_lookup.defer_loading is True


def test_tool_exposes_original_callable_without_mutating_it() -> None:
    def original(value: int) -> int:
        """Increment a value."""
        return value + 1

    original.__dict__["extra_metadata"] = "preserved"
    original_dict = original.__dict__.copy()
    original_name = original.__name__
    original_doc = original.__doc__
    original_signature = inspect.signature(original)

    wrapped_tool = tool(original)

    assert wrapped_tool.__wrapped__ is original
    direct_callable = cast(Callable[[int], int], wrapped_tool.__wrapped__)
    assert direct_callable(1) == 2
    assert not callable(wrapped_tool)
    assert original.__dict__ == original_dict
    assert original.__name__ == original_name
    assert original.__doc__ == original_doc
    assert inspect.signature(wrapped_tool.__wrapped__) == original_signature

    with pytest.raises(AttributeError):
        cast(Any, wrapped_tool).__wrapped__ = original


def test_wrapped_callable_descriptor_is_hidden_on_function_tool_classes() -> None:
    @dataclasses.dataclass(init=False)
    class FunctionToolSubclass(FunctionTool):
        pass

    assert not hasattr(FunctionTool, "__wrapped__")
    assert not hasattr(FunctionToolSubclass, "__wrapped__")


def test_configured_tool_exposes_original_callable() -> None:
    def original(value: int) -> int:
        return value + 1

    configured_tool = tool(name_override="increment")
    wrapped_tool = configured_tool(original)

    assert wrapped_tool.__wrapped__ is original
    assert wrapped_tool.name == "increment"


def test_wrapped_callable_identity_for_supported_function_shapes() -> None:
    def sync_function(value: int) -> int:
        return value

    async def async_function(value: int) -> int:
        return value

    def context_function(ctx: ToolContext[Any], value: int) -> int:
        return value

    class Handler:
        def method(self, value: int) -> int:
            return value

    bound_method = Handler().method

    for original in (sync_function, async_function, context_function, bound_method):
        assert function_tool(original).__wrapped__ is original


@pytest.mark.asyncio
async def test_callable_instance_identity_survives_tool_clone_paths() -> None:
    class Counter:
        def __init__(self) -> None:
            self.calls: list[int] = []

        async def __call__(self, value: int) -> int:
            self.calls.append(value)
            return value

    counter = Counter()
    wrapped_tool = function_tool(counter)
    copied_tool = copy.copy(wrapped_tool)
    deep_copied_tool = copy.deepcopy(wrapped_tool)
    replaced_tool = dataclasses.replace(wrapped_tool, name="renamed")

    for cloned_tool in (wrapped_tool, copied_tool, deep_copied_tool, replaced_tool):
        assert cloned_tool.__wrapped__ is counter

    direct_callable = cast(Callable[[int], Any], wrapped_tool.__wrapped__)
    assert await direct_callable(1) == 1
    assert await copied_tool.on_invoke_tool(ctx_wrapper(), '{"value": 2}') == 2
    assert await deep_copied_tool.on_invoke_tool(ctx_wrapper(), '{"value": 3}') == 3
    assert await replaced_tool.on_invoke_tool(ctx_wrapper(), '{"value": 4}') == 4
    assert counter.calls == [1, 2, 3, 4]


def test_wrapped_callable_follows_standard_unwrap_chain() -> None:
    def original(value: int) -> int:
        return value

    @functools.wraps(original)
    def intermediate(value: int) -> int:
        return original(value)

    wrapped_tool = function_tool(intermediate)

    assert wrapped_tool.__wrapped__ is intermediate
    assert inspect.unwrap(wrapped_tool.__wrapped__) is original
    assert inspect.unwrap(cast(Callable[..., Any], wrapped_tool)) is original


def test_non_decorator_function_tools_have_no_wrapped_callable() -> None:
    async def manual_invoker(ctx: ToolContext[Any], input_json: str) -> str:
        return input_json

    manual_tool = FunctionTool(
        name="manual",
        description="",
        params_json_schema={"type": "object", "properties": {}},
        on_invoke_tool=manual_invoker,
    )
    agent_tool = Agent(name="Nested").as_tool(
        tool_name="nested",
        tool_description="Run the nested agent.",
    )

    for non_decorator_tool in (manual_tool, agent_tool):
        assert not hasattr(non_decorator_tool, "__wrapped__")
        with pytest.raises(AttributeError):
            _ = non_decorator_tool.__wrapped__
        assert inspect.unwrap(cast(Callable[..., Any], non_decorator_tool)) is non_decorator_tool


def test_replacing_invoker_removes_wrapped_callable() -> None:
    def original(value: int) -> int:
        return value

    async def replacement(ctx: ToolContext[Any], input_json: str) -> str:
        return input_json

    wrapped_tool = function_tool(original)
    assert wrapped_tool.__wrapped__ is original

    wrapped_tool.on_invoke_tool = replacement

    assert not hasattr(wrapped_tool, "__wrapped__")


@function_tool(strict_mode=False)
def optional_param_function(a: int, b: int | None = None) -> str:
    if b is None:
        return f"{a}_no_b"
    return f"{a}_{b}"


@pytest.mark.asyncio
async def test_non_strict_mode_function():
    tool = optional_param_function

    assert tool.strict_json_schema is False, "strict_json_schema should be False"

    assert tool.params_json_schema.get("required") == ["a"], "required should only be a"

    input_data = {"a": 5}
    output = await tool.on_invoke_tool(ctx_wrapper(), json.dumps(input_data))
    assert output == "5_no_b"

    input_data = {"a": 5, "b": 10}
    output = await tool.on_invoke_tool(ctx_wrapper(), json.dumps(input_data))
    assert output == "5_10"


@function_tool(strict_mode=False)
def all_optional_params_function(
    x: int = 42,
    y: str = "hello",
    z: int | None = None,
) -> str:
    if z is None:
        return f"{x}_{y}_no_z"
    return f"{x}_{y}_{z}"


@pytest.mark.asyncio
async def test_all_optional_params_function():
    tool = all_optional_params_function

    assert tool.strict_json_schema is False, "strict_json_schema should be False"

    assert tool.params_json_schema.get("required") is None, "required should be empty"

    input_data: dict[str, Any] = {}
    output = await tool.on_invoke_tool(ctx_wrapper(), json.dumps(input_data))
    assert output == "42_hello_no_z"

    input_data = {"x": 10, "y": "world"}
    output = await tool.on_invoke_tool(ctx_wrapper(), json.dumps(input_data))
    assert output == "10_world_no_z"

    input_data = {"x": 10, "y": "world", "z": 99}
    output = await tool.on_invoke_tool(ctx_wrapper(), json.dumps(input_data))
    assert output == "10_world_99"


@function_tool
def get_weather(city: str) -> str:
    """Get the weather for a given city.

    Args:
        city: The city to get the weather for.
    """
    return f"The weather in {city} is sunny."


@pytest.mark.asyncio
async def test_extract_descriptions_from_docstring():
    """Ensure that we extract function and param descriptions from docstrings."""

    tool = get_weather
    assert tool.description == "Get the weather for a given city."
    params_json_schema = tool.params_json_schema
    assert params_json_schema == snapshot(
        {
            "type": "object",
            "properties": {
                "city": {
                    "description": "The city to get the weather for.",
                    "title": "City",
                    "type": "string",
                }
            },
            "title": "get_weather_args",
            "required": ["city"],
            "additionalProperties": False,
        }
    )


@function_tool(
    timeout=1.25,
    timeout_behavior="raise_exception",
    timeout_error_function=sync_error_handler,
)
async def timeout_configured_tool() -> str:
    return "ok"


def test_decorator_timeout_configuration_is_applied() -> None:
    assert timeout_configured_tool.timeout_seconds == 1.25
    assert timeout_configured_tool.timeout_behavior == "raise_exception"
    assert timeout_configured_tool.timeout_error_function is sync_error_handler


@pytest.mark.asyncio
async def test_async_callable_object_works_as_bare_function_tool() -> None:
    class AsyncCallable:
        """Double a value.

        Args:
            value: The value to double.
        """

        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, value: int) -> int:
            self.calls += 1
            await asyncio.sleep(0)
            return value * 2

    handler = AsyncCallable()
    tool = function_tool(handler)

    assert tool.name == "AsyncCallable"
    assert tool.description == "Double a value."
    assert tool.params_json_schema["properties"]["value"] == {
        "description": "The value to double.",
        "title": "Value",
        "type": "integer",
    }
    assert await tool.on_invoke_tool(ctx_wrapper(), '{"value": 4}') == 8
    assert handler.calls == 1


@pytest.mark.asyncio
async def test_slotted_async_callable_object_works_as_function_tool() -> None:
    class AsyncCallable:
        __slots__ = ()

        async def __call__(self, value: int) -> int:
            return value * 2

    tool = function_tool(AsyncCallable())

    assert tool.params_json_schema["properties"]["value"]["type"] == "integer"
    assert await tool.on_invoke_tool(ctx_wrapper(), '{"value": 4}') == 8


@pytest.mark.asyncio
async def test_callable_object_uses_call_docstring_when_class_docstring_missing() -> None:
    class AsyncCallable:
        async def __call__(self, value: int) -> int:
            """Double a value.

            Args:
                value: The value to double.
            """
            return value * 2

    tool = function_tool(AsyncCallable())

    assert tool.description == "Double a value."
    assert tool.params_json_schema["properties"]["value"] == {
        "description": "The value to double.",
        "title": "Value",
        "type": "integer",
    }
    assert await tool.on_invoke_tool(ctx_wrapper(), '{"value": 4}') == 8


def test_callable_object_combines_class_summary_with_call_parameter_docs() -> None:
    class AsyncCallable:
        """Configure a reusable multiplier."""

        async def __call__(self, value: Annotated[int, "Annotated fallback."]) -> int:
            """Multiply a value.

            Args:
                value: The value supplied to this invocation.
            """
            return value * 2

    tool = function_tool(AsyncCallable())

    assert tool.description == "Configure a reusable multiplier."
    assert tool.params_json_schema["properties"]["value"] == {
        "description": "The value supplied to this invocation.",
        "title": "Value",
        "type": "integer",
    }


@pytest.mark.parametrize("class_name", ["Café", "A" * 65])
def test_callable_object_requires_override_for_invalid_fallback_name(class_name: str) -> None:
    async def call(self: Any, value: int) -> int:
        return value

    handler = type(class_name, (), {"__call__": call})()

    with pytest.raises(UserError, match="Pass name_override"):
        function_tool(handler)

    assert function_tool(handler, name_override="safe_name").name == "safe_name"


@pytest.mark.asyncio
async def test_async_callable_object_works_with_configured_function_tool() -> None:
    class AsyncCallable:
        async def __call__(self, value: int) -> int:
            return value + 1

    configured_function_tool = function_tool(
        name_override="increment",
        description_override="Increment a value.",
        timeout=1,
    )
    tool = configured_function_tool(AsyncCallable())

    assert tool.name == "increment"
    assert tool.description == "Increment a value."
    assert await tool.on_invoke_tool(ctx_wrapper(), '{"value": 4}') == 5


@pytest.mark.asyncio
async def test_configured_async_callable_ignores_annotated_class_state() -> None:
    class AsyncCallable:
        value: str
        factor: int

        def __init__(self, factor: int) -> None:
            self.factor = factor

        async def __call__(self, value: int) -> int:
            return value * self.factor

    tool = function_tool(AsyncCallable(3), name_override="multiply")

    assert list(tool.params_json_schema["properties"]) == ["value"]
    assert tool.params_json_schema["properties"]["value"]["type"] == "integer"
    assert await tool.on_invoke_tool(ctx_wrapper(), '{"value": 4}') == 12


@pytest.mark.asyncio
async def test_callable_object_invokes_the_resolved_call_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Handler:
        async def __call__(self, value: int) -> int:
            return value + 1

    handler = Handler()
    tool = function_tool(handler)

    async def replacement(self: Handler, value: int) -> int:
        return value + 100

    monkeypatch.setattr(Handler, "__call__", replacement)

    assert await tool.on_invoke_tool(ctx_wrapper(), '{"value": 4}') == 5


@pytest.mark.asyncio
async def test_sync_callable_object_preserves_awaitable_result() -> None:
    class AwaitableReturningCallable:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, value: int) -> Any:
            self.calls += 1

            async def result() -> int:
                return value * 3

            return result()

    handler = AwaitableReturningCallable()
    tool = function_tool(handler)

    returned = await tool.on_invoke_tool(ctx_wrapper(), '{"value": 4}')
    assert inspect.isawaitable(returned)
    assert handler.calls == 1
    assert await returned == 12


@pytest.mark.asyncio
async def test_sync_function_preserves_awaitable_result() -> None:
    async def result() -> int:
        return 12

    awaitable = result()

    def handler() -> Any:
        return awaitable

    tool = function_tool(handler)

    returned = await tool.on_invoke_tool(ctx_wrapper(), "{}")
    assert returned is awaitable
    assert await returned == 12


@pytest.mark.asyncio
async def test_async_callable_object_preserves_positional_context() -> None:
    class Handler:
        async def __call__(self, ctx: ToolContext[Any], value: int) -> str:
            return f"{ctx.tool_name}:{value}"

    tool = function_tool(Handler(), name_override="handler")

    assert list(tool.params_json_schema["properties"]) == ["value"]
    assert await tool.on_invoke_tool(ctx_wrapper(), '{"value": 4}') == "dummy:4"


@pytest.mark.asyncio
async def test_callable_docstring_opt_out_does_not_read_dynamic_doc() -> None:
    class RaisingDoc:
        def __get__(self, instance: Any, owner: type[Any] | None = None) -> str:
            raise AssertionError("The callable docstring should not be read.")

    class Handler:
        def __call__(self, value: int) -> int:
            return value * 2

    cast(Any, Handler).__doc__ = RaisingDoc()
    tool = function_tool(
        Handler(),
        name_override="handler",
        use_docstring_info=False,
    )

    assert tool.description == ""
    assert tool.params_json_schema["properties"]["value"]["type"] == "integer"
    assert await tool.on_invoke_tool(ctx_wrapper(), '{"value": 4}') == 8


def test_callable_contract_rejects_unknown_call_descriptor() -> None:
    class CustomDescriptor:
        def __get__(self, instance: Any, owner: type[Any]) -> Callable[..., Any]:
            return lambda value: value

    class Handler:
        __call__ = CustomDescriptor()

    with pytest.raises(UserError, match="Unsupported callable object"):
        function_tool(Handler())


@pytest.mark.parametrize(
    "shape",
    [
        "partial",
        "partialmethod",
        "staticmethod",
        "classmethod",
        "decorated-call",
        "update-wrapper",
        "published-annotations",
        "published-annotate",
        "custom-signature",
        "method-signature",
        "local-annotation",
        "singledispatchmethod",
        "builtin",
        "nested-wrapper",
        "keyword-only-context",
        "variadic-context",
        "non-first-context",
        "generic",
        "generic-signature",
        "self",
        "pydantic-generic",
        pytest.param(
            "pep695-generic",
            marks=pytest.mark.skipif(
                sys.version_info < (3, 12),
                reason="PEP 695 requires Python 3.12",
            ),
        ),
        pytest.param(
            "pep695-context-alias",
            marks=pytest.mark.skipif(
                sys.version_info < (3, 12),
                reason="PEP 695 requires Python 3.12",
            ),
        ),
    ],
)
def test_unsupported_callable_shapes_require_explicit_wrappers(shape: str) -> None:
    async def target(value: int) -> int:
        return value

    handler: Any
    if shape == "partial":
        handler = functools.partial(target, 1)
    elif shape == "partialmethod":

        class PartialMethodHandler:
            __call__ = functools.partialmethod(target, 1)

        handler = PartialMethodHandler()
    elif shape == "staticmethod":

        class StaticMethodHandler:
            __call__ = staticmethod(target)

        handler = StaticMethodHandler()
    elif shape == "classmethod":

        class ClassMethodHandler:
            __call__: Any = classmethod(cast(Any, target))

        handler = ClassMethodHandler()
    elif shape == "decorated-call":

        class DecoratedCallHandler:
            @functools.wraps(target)
            async def __call__(self, *args: Any, **kwargs: Any) -> int:
                return await target(*args, **kwargs)

        handler = DecoratedCallHandler()
    elif shape == "update-wrapper":

        class UpdatedWrapper:
            def __init__(self, wrapped: Any) -> None:
                self.wrapped = wrapped
                functools.update_wrapper(self, wrapped)

            def __call__(self, *args: Any, **kwargs: Any) -> Any:
                return self.wrapped(*args, **kwargs)

        handler = UpdatedWrapper(target)
    elif shape == "published-annotations":

        class PublishedAnnotationsHandler:
            def __init__(self) -> None:
                self.__annotations__ = {"value": int, "return": int}

            async def __call__(self, value: int) -> int:
                return value

        handler = PublishedAnnotationsHandler()
    elif shape == "published-annotate":

        class PublishedAnnotateHandler:
            def __init__(self) -> None:
                self.__annotate__ = lambda _format: {"value": int, "return": int}

            async def __call__(self, value: int) -> int:
                return value

        handler = PublishedAnnotateHandler()
    elif shape == "custom-signature":

        class CustomSignatureHandler:
            __signature__ = inspect.Signature(
                [
                    inspect.Parameter(
                        "value",
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        annotation=int,
                    )
                ]
            )

            async def __call__(self, *args: Any, **kwargs: Any) -> int:
                return cast(int, args[0])

        handler = CustomSignatureHandler()
    elif shape == "method-signature":

        class MethodSignatureHandler:
            async def __call__(self, value: int) -> int:
                return value

        cast(Any, MethodSignatureHandler.__call__).__signature__ = inspect.Signature(
            [
                inspect.Parameter(
                    "value",
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    annotation=int,
                )
            ]
        )
        handler = MethodSignatureHandler()
    elif shape == "local-annotation":

        class LocalPayload(BaseModel):
            value: int

        class LocalAnnotationHandler:
            async def __call__(self, value: LocalPayload) -> int:
                return value.value

        handler = LocalAnnotationHandler()
    elif shape == "singledispatchmethod":

        class SingleDispatchHandler:
            __call__ = functools.singledispatchmethod(target)

        handler = SingleDispatchHandler()
    elif shape == "builtin":
        handler = operator.itemgetter(0)
    elif shape == "nested-wrapper":

        class NestedHandler:
            async def __call__(self, value: int) -> int:
                return value

        class NestedWrapper:
            def __init__(self, wrapped: Any) -> None:
                self.wrapped = wrapped
                functools.update_wrapper(self, wrapped)

            def __call__(self, *args: Any, **kwargs: Any) -> Any:
                return self.wrapped(*args, **kwargs)

        handler = NestedWrapper(NestedHandler())
    elif shape == "keyword-only-context":

        class KeywordOnlyContextHandler:
            async def __call__(self, *, ctx: ToolContext[Any], value: int) -> int:
                return value

        handler = KeywordOnlyContextHandler()
    elif shape == "variadic-context":

        class VariadicContextHandler:
            async def __call__(self, *ctx: ToolContext[Any]) -> int:
                return len(ctx)

        handler = VariadicContextHandler()
    elif shape == "non-first-context":

        class NonFirstContextHandler:
            async def __call__(self, value: int, ctx: ToolContext[Any]) -> int:
                return value

        handler = NonFirstContextHandler()
    elif shape == "generic":

        class GenericHandler(Generic[CallableValueT]):
            async def __call__(self, value: CallableValueT) -> CallableValueT:
                return value

        handler = GenericHandler[int]()
    elif shape == "generic-signature":

        class GenericSignatureHandler(Generic[CallableValueT]):
            __signature__ = inspect.Signature(
                [
                    inspect.Parameter(
                        "value",
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        annotation="CallableValueT",
                    )
                ]
            )

            async def __call__(self, *args: Any, **kwargs: Any) -> CallableValueT:
                return cast(CallableValueT, args[0])

        handler = GenericSignatureHandler[int]()
    elif shape == "self":

        class SelfHandler:
            async def __call__(self, other: Self) -> Self:
                return other

        handler = SelfHandler()
    elif shape == "pydantic-generic":

        class PydanticGenericHandler(BaseModel, Generic[CallableValueT]):
            async def __call__(self, value: CallableValueT) -> CallableValueT:
                return value

        handler = PydanticGenericHandler[int]()
    elif shape == "pep695-generic":
        namespace: dict[str, Any] = {}
        exec(
            "from __future__ import annotations\n"
            "class Handler[T]:\n"
            "    async def __call__(self, value: T) -> T:\n"
            "        return value\n",
            namespace,
        )
        handler = namespace["Handler"][int]()
    elif shape == "pep695-context-alias":
        namespace = {"Any": Any, "ToolContext": ToolContext}
        exec(
            "type LiveContext = ToolContext[Any]\n"
            "class AliasContextHandler:\n"
            "    async def __call__(self, ctx: LiveContext, value: int) -> int:\n"
            "        return value\n",
            namespace,
        )
        handler = namespace["AliasContextHandler"]()
    else:
        raise AssertionError(f"Unhandled shape: {shape}")

    with pytest.raises(
        UserError,
        match="explicit wrapper function|Unsupported generic|annotations resolvable",
    ):
        function_tool(handler)


@pytest.mark.asyncio
async def test_callable_object_resolves_class_scoped_call_annotations() -> None:
    class BaseHandler:
        class Payload(BaseModel):
            value: int

        async def __call__(self, payload: Payload) -> int:
            return payload.value

    class Handler(BaseHandler):
        pass

    tool = function_tool(Handler())

    assert tool.params_json_schema["properties"]["payload"] == {"$ref": "#/$defs/Payload"}
    assert await tool.on_invoke_tool(ctx_wrapper(), '{"payload": {"value": 4}}') == 4


def test_inherited_callable_resolves_defining_module_annotations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_module_name = "tests._callable_base_module"
    subclass_module_name = "tests._callable_subclass_module"
    base_module = ModuleType(base_module_name)
    subclass_module = ModuleType(subclass_module_name)
    monkeypatch.setitem(sys.modules, base_module_name, base_module)
    monkeypatch.setitem(sys.modules, subclass_module_name, subclass_module)

    exec(
        "from __future__ import annotations\n"
        "from pydantic import BaseModel\n"
        "class Payload(BaseModel):\n"
        "    value: int\n"
        "class BaseHandler:\n"
        "    async def __call__(self, payload: Payload) -> int:\n"
        "        return payload.value\n",
        base_module.__dict__,
    )
    subclass_module.__dict__["BaseHandler"] = base_module.__dict__["BaseHandler"]
    exec(
        "from __future__ import annotations\nclass Handler(BaseHandler):\n    pass\n",
        subclass_module.__dict__,
    )

    tool = function_tool(subclass_module.__dict__["Handler"]())

    assert tool.params_json_schema["properties"]["payload"]["$ref"] == "#/$defs/Payload"


@pytest.mark.asyncio
async def test_callable_object_ignores_class_state_annotations() -> None:
    class Handler:
        value: str

        async def __call__(self, value: int) -> int:
            return value * 2

    tool = function_tool(Handler())

    assert tool.params_json_schema["properties"]["value"]["type"] == "integer"
    assert await tool.on_invoke_tool(ctx_wrapper(), '{"value": 4}') == 8


def test_function_tool_timeout_arguments_are_keyword_only() -> None:
    signature = inspect.signature(function_tool)

    assert signature.parameters["timeout"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["timeout_behavior"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["timeout_error_function"].kind is inspect.Parameter.KEYWORD_ONLY
