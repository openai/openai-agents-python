from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest

from agents import Agent, RunConfig, RunContextWrapper, Runner, ToolErrorFormatterArgs, UserError
from agents.decorators import tool
from agents.testing import ScriptedModel
from agents.tool import default_tool_error_function

from .test_responses import get_function_tool_call, get_text_message
from .testing_processor import SPAN_PROCESSOR_TESTING

SECRET = "synthetic-private-exception-detail"
GENERIC = "An error occurred while running the tool. Please try again."
APPROVED = "Order IDs must start with ORD-."


def make_model(name: str) -> ScriptedModel:
    return ScriptedModel(
        [[get_function_tool_call(name, "{}", call_id="call-5324")], [get_text_message("done")]]
    )


def model_output(model: ScriptedModel) -> Any:
    items = model.calls[-1].input
    assert isinstance(items, list)
    return next(item["output"] for item in items if item.get("type") == "function_call_output")


def assert_redacted(caplog: pytest.LogCaptureFixture, result: Any, model: ScriptedModel) -> None:
    assert SECRET not in json.dumps(model.calls[-1].input)
    assert SECRET not in json.dumps(result.to_input_list())
    spans = [span.export() for span in SPAN_PROCESSOR_TESTING.get_ordered_spans()]
    assert spans
    assert SECRET not in json.dumps(spans)
    for record in caplog.records:
        assert SECRET not in repr(record.__dict__)
        assert record.exc_info is None
        assert record.exc_text is None
        assert SECRET not in logging.Formatter().format(record)


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("async_handler", [False, True])
async def test_run_formatter_returns_only_approved_feedback(
    streamed: bool, async_handler: bool, caplog: pytest.LogCaptureFixture
) -> None:
    class ToolInputError(Exception):
        pass

    def fail() -> str:
        try:
            raise RuntimeError(f"cause-{SECRET}")
        except RuntimeError as cause:
            raise ToolInputError(SECRET) from cause

    async def fail_async() -> str:
        return fail()

    failing_tool = tool(fail_async if async_handler else fail)
    received: list[ToolErrorFormatterArgs[Any]] = []

    def formatter(args: ToolErrorFormatterArgs[Any]) -> str:
        received.append(args)
        assert isinstance(args.error, ToolInputError)
        return APPROVED

    async def async_formatter(args: ToolErrorFormatterArgs[Any]) -> str:
        await asyncio.sleep(0)
        return formatter(args)

    config = RunConfig(
        tool_error_formatter=async_formatter if async_handler else formatter,
        trace_include_sensitive_data=True,
    )
    model = make_model(failing_tool.name)
    agent = Agent(name="test", tools=[failing_tool], model=model)
    with caplog.at_level(logging.DEBUG, logger="openai.agents"):
        if streamed:
            result = Runner.run_streamed(agent, "start", run_config=config)
            async for _ in result.stream_events():
                pass
        else:
            result = await Runner.run(agent, "start", run_config=config)
    assert result.final_output == "done"
    assert model_output(model) == APPROVED
    assert len(received) == 1
    args = received[0]
    assert (args.kind, args.tool_type, args.tool_name, args.call_id) == (
        "tool_exception",
        "function",
        failing_tool.name,
        "call-5324",
    )
    assert args.default_message == GENERIC
    assert_redacted(caplog, result, model)


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ["none", "raise", "invalid", "empty"])
async def test_formatter_fallback_does_not_expose_exception_or_bad_return(
    response: str, caplog: pytest.LogCaptureFixture
) -> None:
    class UnprintableError(Exception):
        def __str__(self) -> str:
            raise AssertionError("must not stringify private exception")

    class UnprintableResult:
        def __str__(self) -> str:
            raise AssertionError("must not stringify invalid result")

        def __repr__(self) -> str:
            raise AssertionError("must not repr invalid result")

    @tool
    async def fail() -> str:
        raise UnprintableError(SECRET)

    async def formatter(args: ToolErrorFormatterArgs[Any]) -> Any:
        await asyncio.sleep(0)
        if response == "raise":
            raise UnprintableError(SECRET) from args.error
        if response == "invalid":
            return UnprintableResult()
        if response == "empty":
            return ""
        return None

    model = make_model(fail.name)
    with caplog.at_level(logging.DEBUG, logger="openai.agents"):
        result = await Runner.run(
            Agent(name="test", tools=[fail], model=model),
            "start",
            run_config=RunConfig(tool_error_formatter=formatter, trace_include_sensitive_data=True),
        )
    assert result.final_output == "done"
    assert model_output(model) == ("" if response == "empty" else GENERIC)
    assert_redacted(caplog, result, model)


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["custom", "propagate", "explicit-default"])
async def test_explicit_tool_policy_wins_over_run_formatter(policy: str) -> None:
    def per_tool(ctx: RunContextWrapper[Any], error: Exception) -> str:
        return "per-tool message"

    @tool(
        failure_error_function=(
            per_tool
            if policy == "custom"
            else default_tool_error_function
            if policy == "explicit-default"
            else None
        )
    )
    async def fail() -> str:
        raise ValueError(SECRET)

    called: list[ToolErrorFormatterArgs[Any]] = []

    def formatter(args: ToolErrorFormatterArgs[Any]) -> str:
        called.append(args)
        return APPROVED

    model = make_model(fail.name)
    agent = Agent(name="test", tools=[fail], model=model)
    config = RunConfig(tool_error_formatter=formatter)
    if policy == "propagate":
        with pytest.raises(UserError, match=SECRET):
            await Runner.run(agent, "start", run_config=config)
    else:
        await Runner.run(agent, "start", run_config=config)
        assert model_output(model) == ("per-tool message" if policy == "custom" else GENERIC)
    assert not called


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel", "timeout"])
async def test_timeout_and_tool_cancellation_keep_existing_policies(failure: str) -> None:
    @tool(timeout=0.01 if failure == "timeout" else None)
    async def fail() -> str:
        if failure == "cancel":
            raise asyncio.CancelledError(SECRET)
        await asyncio.Event().wait()
        return "unreachable"

    called: list[ToolErrorFormatterArgs[Any]] = []

    def formatter(args: ToolErrorFormatterArgs[Any]) -> str:
        called.append(args)
        return APPROVED

    model = make_model(fail.name)
    await Runner.run(
        Agent(name="test", tools=[fail], model=model),
        "start",
        run_config=RunConfig(tool_error_formatter=formatter),
    )
    assert not called
    assert model_output(model) == (
        "Tool 'fail' timed out after 0.01 seconds." if failure == "timeout" else GENERIC
    )


@pytest.mark.asyncio
async def test_concurrent_runs_keep_their_own_formatter_and_parent_cancellation() -> None:
    @tool
    async def fail() -> str:
        raise ValueError(SECRET)

    entered = asyncio.Event()
    release = asyncio.Event()

    async def waiting_formatter(args: ToolErrorFormatterArgs[Any]) -> str:
        entered.set()
        await release.wait()
        return "first run"

    first_model = make_model(fail.name)
    second_model = make_model(fail.name)
    first = asyncio.create_task(
        Runner.run(
            Agent(name="first", tools=[fail], model=first_model),
            "start",
            run_config=RunConfig(tool_error_formatter=waiting_formatter),
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        second = await Runner.run(
            Agent(name="second", tools=[fail], model=second_model),
            "start",
            run_config=RunConfig(tool_error_formatter=lambda args: "second run"),
        )
        assert second.final_output == "done"
        assert model_output(second_model) == "second run"
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert len(first_model.calls) == 1
    finally:
        release.set()
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)


def test_formatter_args_preserve_old_positional_constructor() -> None:
    context = RunContextWrapper(None)
    args = ToolErrorFormatterArgs(
        "tool_not_found", "function", "missing", "call", "default", context
    )
    assert args.run_context is context
    assert args.error is None


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_redacted", [False, True])
async def test_formatter_failure_respects_local_diagnostic_opt_in(
    tool_redacted: bool, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import agents._debug as debug

    monkeypatch.setattr(debug, "DONT_LOG_TOOL_DATA", tool_redacted)

    @tool
    async def fail() -> str:
        raise ValueError(SECRET)

    def formatter(args: ToolErrorFormatterArgs[Any]) -> str:
        raise RuntimeError(f"formatter-{SECRET}") from args.error

    model = make_model(fail.name)
    with caplog.at_level(logging.DEBUG, logger="openai.agents"):
        result = await Runner.run(
            Agent(name="test", tools=[fail], model=model),
            "start",
            run_config=RunConfig(tool_error_formatter=formatter, trace_include_sensitive_data=True),
        )
    assert model_output(model) == GENERIC
    assert SECRET not in json.dumps(result.to_input_list())
    assert SECRET not in json.dumps(
        [span.export() for span in SPAN_PROCESSOR_TESTING.get_ordered_spans()]
    )
    if tool_redacted:
        assert_redacted(caplog, result, model)
    else:
        assert SECRET in caplog.text
        assert any(record.exc_info is not None for record in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("program_call", [False, True])
async def test_schema_backed_tool_retains_program_failure_policy(program_call: bool) -> None:
    from openai.types.responses.response_function_tool_call import CallerProgram
    from openai.types.responses.response_output_item import Program

    from agents import ProgrammaticToolCallingTool

    @tool(
        allowed_callers=["direct", "programmatic"],
        output_json_schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
    )
    async def fail() -> str:
        raise ValueError(SECRET)

    called: list[ToolErrorFormatterArgs[Any]] = []

    def formatter(args: ToolErrorFormatterArgs[Any]) -> str:
        called.append(args)
        return APPROVED

    call = get_function_tool_call(fail.name, "{}")
    if program_call:
        call.caller = CallerProgram(type="program", caller_id="synthetic-program")
    program = Program(
        id="program-item",
        call_id="synthetic-program",
        code="fail()",
        fingerprint="synthetic-fingerprint",
        type="program",
    )
    model = ScriptedModel([[program, call] if program_call else [call], [get_text_message("done")]])
    agent = Agent(name="test", tools=[fail, ProgrammaticToolCallingTool()], model=model)
    config = RunConfig(tool_error_formatter=formatter)
    if program_call:
        with pytest.raises(UserError, match=SECRET):
            await Runner.run(agent, "start", run_config=config)
        assert not called
    else:
        await Runner.run(agent, "start", run_config=config)
        assert len(called) == 1
        assert model_output(model) == APPROVED


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["omitted", "custom", "explicit-default", "propagate"])
async def test_agent_tool_retains_factory_failure_policy(
    policy: str, caplog: pytest.LogCaptureFixture
) -> None:
    async def extract(result: Any) -> str:
        raise ValueError(SECRET)

    options: dict[str, Any] = {}
    if policy != "omitted":
        options["failure_error_function"] = (
            (lambda ctx, error: "per-tool")
            if policy == "custom"
            else default_tool_error_function
            if policy == "explicit-default"
            else None
        )
    child = Agent(name="child", model=ScriptedModel([[get_text_message("child done")]]))
    child_tool = child.as_tool("child", "Run child", custom_output_extractor=extract, **options)
    model = ScriptedModel(
        [[get_function_tool_call("child", '{"input":"task"}')], [get_text_message("done")]]
    )
    called: list[str] = []

    def formatter(args: ToolErrorFormatterArgs[Any]) -> str:
        called.append(args.tool_name)
        return APPROVED

    agent = Agent(name="parent", model=model, tools=[child_tool])
    config = RunConfig(tool_error_formatter=formatter, trace_include_sensitive_data=True)
    if policy == "propagate":
        with pytest.raises(UserError, match=SECRET):
            await Runner.run(agent, "start", run_config=config)
    else:
        with caplog.at_level(logging.DEBUG, logger="openai.agents"):
            result = await Runner.run(agent, "start", run_config=config)
        assert (
            model_output(model)
            == {
                "omitted": GENERIC,
                "custom": "per-tool",
                "explicit-default": GENERIC,
            }[policy]
        )
        if policy == "omitted":
            assert_redacted(caplog, result, model)
    assert not called


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "level,policy",
    [
        ("agent", "omitted"),
        ("agent", "custom"),
        ("agent", "explicit-default"),
        ("agent", "propagate"),
        ("server", "custom"),
        ("server", "explicit-default"),
        ("server", "propagate"),
    ],
)
async def test_mcp_tool_retains_factory_failure_policy(
    level: str, policy: str, caplog: pytest.LogCaptureFixture
) -> None:
    from agents.exceptions import AgentsException

    from .mcp.helpers import FakeMCPServer

    class FailingServer(FakeMCPServer):
        async def call_tool(self, tool_name: str, arguments: Any, **kwargs: Any) -> Any:
            raise ValueError(SECRET)

    handler = (
        (lambda ctx, error: "per-tool")
        if policy == "custom"
        else default_tool_error_function
        if policy == "explicit-default"
        else None
    )
    server_options = {"failure_error_function": handler} if level == "server" else {}
    server = FailingServer(**server_options)
    server.add_tool("lookup", {})
    mcp_config: Any = {}
    if level == "server":
        # Server policy must win over an agent-wide MCP policy as well as the run formatter.
        mcp_config["failure_error_function"] = lambda ctx, error: "agent policy"
    elif policy != "omitted":
        mcp_config["failure_error_function"] = handler
    model = make_model("lookup")
    agent = Agent(name="test", model=model, mcp_servers=[server], mcp_config=mcp_config)
    called: list[str] = []

    def formatter(args: ToolErrorFormatterArgs[Any]) -> str:
        called.append(args.tool_name)
        return APPROVED

    config = RunConfig(tool_error_formatter=formatter, trace_include_sensitive_data=True)
    if policy == "propagate":
        with pytest.raises(AgentsException, match=SECRET):
            await Runner.run(agent, "start", run_config=config)
    else:
        with caplog.at_level(logging.DEBUG, logger="openai.agents"):
            result = await Runner.run(agent, "start", run_config=config)
        assert (
            model_output(model)
            == {
                "omitted": GENERIC,
                "custom": "per-tool",
                "explicit-default": GENERIC,
            }[policy]
        )
        if policy == "omitted":
            assert_redacted(caplog, result, model)
    assert not called


@pytest.mark.asyncio
async def test_run_formatter_uses_qualified_tool_identity() -> None:
    from agents import tool_namespace

    @tool
    async def lookup() -> str:
        raise ValueError(SECRET)

    tools = [
        *tool_namespace(name="crm", description="CRM", tools=[lookup]),
        *tool_namespace(name="billing", description="Billing", tools=[lookup]),
    ]
    model = ScriptedModel(
        [
            [
                get_function_tool_call("lookup", "{}", namespace="crm", call_id="crm-call"),
                get_function_tool_call("lookup", "{}", namespace="billing", call_id="billing-call"),
            ],
            [get_text_message("done")],
        ]
    )

    def formatter(args: ToolErrorFormatterArgs[Any]) -> str | None:
        return APPROVED if args.tool_name == "crm.lookup" else None

    await Runner.run(
        Agent(name="test", model=model, tools=tools),
        "start",
        run_config=RunConfig(tool_error_formatter=formatter),
    )
    items = model.calls[-1].input
    assert isinstance(items, list)
    assert {
        item["call_id"]: item["output"]
        for item in items
        if item.get("type") == "function_call_output"
    } == {
        "crm-call": APPROVED,
        "billing-call": GENERIC,
    }


@pytest.mark.asyncio
async def test_run_formatter_collapses_deferred_top_level_namespace() -> None:
    @tool(defer_loading=True)
    async def lookup() -> str:
        raise ValueError(SECRET)

    model = ScriptedModel(
        [
            [get_function_tool_call("lookup", "{}", namespace="lookup")],
            [get_text_message("done")],
        ]
    )

    def formatter(args: ToolErrorFormatterArgs[Any]) -> str | None:
        return APPROVED if args.tool_name == "lookup" else None

    await Runner.run(
        Agent(name="test", model=model, tools=[lookup]),
        "start",
        run_config=RunConfig(tool_error_formatter=formatter),
    )
    assert model_output(model) == APPROVED


@pytest.mark.asyncio
async def test_mcp_cancellation_keeps_existing_failure_policy() -> None:
    from .mcp.helpers import FakeMCPServer

    class CancelledServer(FakeMCPServer):
        async def call_tool(self, tool_name: str, arguments: Any, **kwargs: Any) -> Any:
            raise asyncio.CancelledError(SECRET)

    server = CancelledServer()
    server.add_tool("lookup", {})
    called: list[ToolErrorFormatterArgs[Any]] = []

    def formatter(args: ToolErrorFormatterArgs[Any]) -> str:
        called.append(args)
        return APPROVED

    model = make_model("lookup")
    result = await Runner.run(
        Agent(name="test", model=model, mcp_servers=[server]),
        "start",
        run_config=RunConfig(tool_error_formatter=formatter),
    )
    assert result.final_output == "done"
    assert not called
    assert model_output(model) == GENERIC
