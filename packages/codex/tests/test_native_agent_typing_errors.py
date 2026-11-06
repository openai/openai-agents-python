# pyright: reportArgumentType=true, reportCallIssue=true, reportAssignmentType=true, reportUnnecessaryTypeIgnoreComment=true
"""Invalid native consumers must retain their expected diagnostics."""

from typing import TYPE_CHECKING, Any

from agents import (
    Agent,
    Environment,
    FunctionTool,
    HttpMCP,
    InputImage,
    InteractionRequired,
    MCPResultContext,
    ModelSettings,
    NativeAgents,
    NativeRole,
    StdioMCP,
    Tool,
    ToolContext,
    Turn,
    function_tool,
)
from agents.telemetry import TraceOptions

from .test_native_agent_typing import (
    Answer,
    Application,
    Specialist,
    StructuredSpecialist,
    agent,
    lookup,
    plain,
    typed_executor,
    typed_executor_agent,
)

if TYPE_CHECKING:

    def invalid_manual_resume(error: InteractionRequired) -> None:
        if error.state is not None:
            Agent(instructions="Continue.").session(state=error.state.to_json())  # pyright: ignore[reportArgumentType]

    NativeAgents(additional_roles=["research"])  # pyright: ignore[reportArgumentType]
    NativeRole(name="research", instructions="Read.", description="Researcher", model=123)  # pyright: ignore[reportArgumentType]
    NativeRole(
        name="research",
        instructions="Read.",
        description="Researcher",
        model_settings={"reasoning_summary": "auto"},  # pyright: ignore[reportArgumentType]
    )
    Environment(url="wss://executor.invalid", cwd="/workspace", tools=["shell"])  # pyright: ignore[reportArgumentType]
    Environment(
        url="wss://executor.invalid",
        cwd="/workspace",
        tools=["exec_command"],
        sandbox_mode="read-only",  # pyright: ignore[reportCallIssue]
    )
    Environment(
        url="wss://executor.invalid",
        cwd="/workspace",
        tools=["exec_command"],
        tls_verify=False,  # pyright: ignore[reportCallIssue]
    )
    # Use a RootModel class for a statically typed union output.
    Agent(instructions="Choose.", output_type=int | str)  # pyright: ignore[reportCallIssue, reportArgumentType]
    HttpMCP(
        name="docs",
        url="https://example.invalid/mcp",
        tools=["write"],
        approval_mode="always",  # pyright: ignore[reportArgumentType]
    )
    HttpMCP(name="docs", url="https://example.invalid/mcp", tools=["write"], approval_mode=None)  # pyright: ignore[reportArgumentType]

    @function_tool
    async def unrelated_context(context: ToolContext[str]) -> str:
        return context.context

    InputImage(type="input_text", image_url="data:image/png;base64,typing-only")  # pyright: ignore[reportArgumentType]
    StdioMCP(name="records", command="server", tools=["read"], location="elsewhere")  # pyright: ignore[reportArgumentType]
    HttpMCP(name="records", url="https://example.invalid", tools=["read"], command="server")  # pyright: ignore[reportCallIssue]
    agent.session()  # pyright: ignore[reportCallIssue]
    agent.session(context="wrong context")  # pyright: ignore[reportArgumentType]
    agent.run_sync("input")  # pyright: ignore[reportCallIssue]
    agent.run_sync("input", context="wrong context")  # pyright: ignore[reportArgumentType]
    agent.run_sync("input")  # pyright: ignore[reportCallIssue]
    agent.run_sync("input", context="wrong")  # pyright: ignore[reportArgumentType]

    # The context-free tool must not erase the contextual tool's requirement.
    mixed = Agent(instructions="Use the tool.", tools=[plain, lookup])
    mixed.session(context="wrong context")  # pyright: ignore[reportArgumentType]

    async def mcp_policy(
        ctx: MCPResultContext[Application], value: dict[str, Any]
    ) -> dict[str, Any]:
        return value

    protected = HttpMCP(
        name="vault",
        url="https://example.invalid/mcp",
        tools=["read"],
        transform_result=mcp_policy,
    )
    policy_agent = Agent(instructions="Read.", tools=[protected])
    policy_agent.session()  # pyright: ignore[reportCallIssue]
    policy_agent.session(context="wrong")  # pyright: ignore[reportArgumentType]
    Agent[str](instructions="Read.", tools=[protected])  # pyright: ignore[reportArgumentType]

    mcp = HttpMCP(name="docs", url="https://example.invalid/mcp", tools=["read"])
    native_mixed: Agent[Application, str] = Agent(instructions="Read.", tools=[mcp, plain, lookup])
    native_mixed.session()  # pyright: ignore[reportCallIssue]
    native_mixed.session(context="wrong")  # pyright: ignore[reportArgumentType]
    native_mixed.run_sync("read", context="wrong")  # pyright: ignore[reportArgumentType]
    Agent[Application, Answer](
        instructions="Read.",
        tools=[mcp, lookup],
        output_type=str,  # pyright: ignore[reportArgumentType]
    )

    settings: ModelSettings = {"reasoning_efort": "low"}  # pyright: ignore[reportAssignmentType]
    tool_choice: ModelSettings = {"tool_choice": {"type": "web_search"}}  # pyright: ignore[reportAssignmentType]
    typed: Agent[Application, Answer] = Agent(instructions="Answer.")  # pyright: ignore[reportCallIssue]
    typed_explicit: Agent[Application, Answer] = Agent[Application, Answer](  # pyright: ignore[reportCallIssue]
        instructions="Answer."
    )

    # Allocation must still use the selected initializer's parameter types.
    Agent()  # pyright: ignore[reportCallIssue]
    Agent(instructions=123)  # pyright: ignore[reportArgumentType]
    Agent(instructions="Answer.", bogus=True)  # pyright: ignore[reportCallIssue]
    Specialist(123)  # pyright: ignore[reportArgumentType]
    StructuredSpecialist(123)  # pyright: ignore[reportArgumentType]

    async def invalid_run() -> None:
        Agent[None](instructions="Use tools.", tools=[typed_executor])  # pyright: ignore[reportArgumentType]
        Agent[None](instructions="Read.").session(environment=typed_executor)  # pyright: ignore[reportArgumentType]
        Agent[str](instructions="Read.").session(context="tenant", environment=typed_executor)  # pyright: ignore[reportArgumentType]
        typed_executor_agent.session()  # pyright: ignore[reportCallIssue]
        await typed_executor_agent.run("input", context=42)  # pyright: ignore[reportArgumentType]
        await agent.run("input")  # pyright: ignore[reportCallIssue]
        await agent.run("input", context="wrong")  # pyright: ignore[reportArgumentType]
        await agent.run("input")  # pyright: ignore[reportCallIssue]
        await agent.run("input", context="wrong context")  # pyright: ignore[reportArgumentType]
        async with agent.session(context=Application("tenant")) as session:
            await session.submit("input", if_busy="steer", trace_options=TraceOptions())  # pyright: ignore[reportCallIssue, reportArgumentType]
            await session.submit("input", if_busy="steer", trace_options={"disabled": True})  # pyright: ignore[reportCallIssue, reportArgumentType]
            turn = await session.submit("read")
            session.events(turn)  # pyright: ignore[reportCallIssue]
            turn.get_result(1)  # pyright: ignore[reportCallIssue]
            await session.update(model=123)  # pyright: ignore[reportCallIssue, reportArgumentType]
            await session.update(model_settings={"temperature": 0.2})  # pyright: ignore[reportCallIssue, reportArgumentType]
            await session.update(model_settings={"reasoning_effort": None})  # pyright: ignore[reportCallIssue, reportArgumentType]
            await session.update(instructions=123)  # pyright: ignore[reportCallIssue, reportArgumentType]
            await session.submit("input", tools=[unrelated_context])  # pyright: ignore[reportArgumentType]
            await session.submit("input", tools=[plain], instructions="Changed.")  # pyright: ignore[reportCallIssue]
            await session.submit("input", instructions="Changed.", model="gpt-5.5")  # pyright: ignore[reportCallIssue]
            await session.submit("input", tools=[plain], model_settings={"reasoning_effort": "low"})  # pyright: ignore[reportCallIssue]
            await session.update(tools=[unrelated_context])  # pyright: ignore[reportCallIssue, reportArgumentType]
            await session.update(tools=[plain], instructions="Changed.")  # pyright: ignore[reportCallIssue, reportArgumentType]
            await session.update(instructions="Changed.", model="gpt-5.5")  # pyright: ignore[reportCallIssue, reportArgumentType]
            await session.update(tools=[plain], model_settings={"reasoning_effort": "low"})  # pyright: ignore[reportCallIssue, reportArgumentType]
            await session.compact("summary")  # pyright: ignore[reportCallIssue]
            await session.compact(summary=42)  # pyright: ignore[reportArgumentType]


if TYPE_CHECKING:
    manual = FunctionTool(
        name="manual", description="Typed result", input_type=Answer, output_type=Answer
    )
    action = manual.match(object())
    if action is not None:
        invalid_response = action.respond("wrong result")  # pyright: ignore[reportArgumentType]
        missing: str = action.arguments  # pyright: ignore[reportAssignmentType]
    FunctionTool(name="invalid", description="Object input", input_type=str, output_type=str)  # pyright: ignore[reportArgumentType]


if TYPE_CHECKING:

    def missing_step_index(turn: Turn[Any]) -> None:
        return None

    def misnamed_step_index(turn: Turn[Any], *, index: int) -> None:
        return None

    def invalid_step_turn(turn: str, *, step_index: int) -> None:
        return None

    def invalid_step_result(turn: Turn[Any], *, step_index: int) -> int:
        return 42

    def incompatible_step_context(turn: Turn[Any], *, step_index: int) -> list[Tool[str]]:
        return []

    async def invalid_before_step_types() -> None:
        async with agent.session(context=Application("tenant")) as session:
            await session.submit("input", before_step=missing_step_index)  # pyright: ignore[reportArgumentType]
            await session.submit("input", before_step=misnamed_step_index)  # pyright: ignore[reportArgumentType]
            await session.submit("input", before_step=invalid_step_turn)  # pyright: ignore[reportArgumentType]
            await session.submit("input", before_step=invalid_step_result)  # pyright: ignore[reportArgumentType]
            await session.submit("input", before_step=incompatible_step_context)  # pyright: ignore[reportArgumentType]
