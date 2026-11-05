# pyright: strict
"""Consumer types for declarations, mixed tools, sessions, and results."""

from collections.abc import AsyncIterator, Awaitable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, RootModel
from typing_extensions import assert_type

import agents
from agents import (
    ActionRequested,
    Agent,
    Checklist,
    ChecklistState,
    CheckpointStatus,
    ChildInvocation,
    Completed,
    Environment,
    EnvironmentSource,
    ExecutorContext,
    InputImage,
    InputText,
    LocalStorage,
    ModelRequestContext,
    NativeAgents,
    NativeError,
    NativeGoal,
    NativeGoals,
    NativeOutput,
    NativeProvider,
    NativeRole,
    PromptedOutput,
    ReplaceArguments,
    ResponseIntegrityReason,
    Session,
    Tool,
    ToolContext,
    ToolOutput,
    ToolRef,
    TraceContext,
    Turn,
    UserInput,
    function_tool,
)
from agents.history import RecordedTurn
from agents.observation import EventStream, ToolFinished, ToolStarted
from agents.subagents import Subagent

provider_error = NativeError(
    "provider rejected the request",
    status_code=401,
    code="invalid_api_key",
    request_id="synthetic-request",
    category="authentication_error",
)
assert_type(provider_error.status_code, int | None)
assert_type(provider_error.code, str | None)
assert_type(provider_error.request_id, str | None)
assert_type(provider_error.category, str | None)
assert_type(provider_error.response_integrity_reason, ResponseIntegrityReason | None)

executor = Environment(
    url="wss://executor.example.invalid/session",
    cwd="/workspace",
    tools=["exec_command", "write_stdin", "apply_patch"],
    headers={"Authorization": "Bearer application-owned-token"},
)
executor_agent = Agent(instructions="Use native tools.", tools=[])
assert_type(Agent[None](instructions="Use native tools.", tools=[]), Agent[None, str])


async def no_context_executor() -> None:
    async with executor_agent.session(environment=executor) as session:
        turn = await session.submit("read")
        assert_type(turn, Turn[str])
        assert_type(await turn.get_result(), Completed[str])
        assert_type(await turn.get_result(), Completed[str])
        assert_type(turn.events(), EventStream)
        assert_type(await turn.steer("detail"), None)
        assert_type(await session.submit("detail", if_busy="steer", writable_roots=None), Turn[Any])
        assert_type(await session.submit("detail", if_busy="steer", trace_options=None), Turn[Any])
        assert_type(await turn.cancel(mode="graceful"), None)
        assert_type(await session.cancel_all(), None)
    assert_type(await executor_agent.run("read", environment=executor), Completed[str])


async def mapping_trace_option_types(options: Mapping[str, Any]) -> None:
    async with executor_agent.session(environment=executor, trace_options=options) as session:
        assert_type(await session.submit("read", trace_options=options), Turn[str])
        assert_type(await session.submit("read", trace_options={"disabled": True}), Turn[str])
    assert_type(
        await executor_agent.run("read", environment=executor, trace_options=options),
        Completed[str],
    )


def mapping_trace_option_sync_types(options: Mapping[str, Any]) -> None:
    assert_type(
        executor_agent.run_sync("read", environment=executor, trace_options=options), Completed[str]
    )


native_roles = NativeAgents(
    additional_roles=[
        NativeRole(
            name="research",
            instructions="Read the workspace.",
            description="Researcher",
            model_settings={"reasoning_effort": "high"},
        )
    ],
    max_children=2,
)
child_agent = Agent(model="gpt-5.5", instructions="Delegate.", tools=[native_roles])
assert_type(child_agent, Agent[None, str])


async def native_child_types(session: Session[None, str]) -> None:
    assert_type(await session.update(tools=[executor, native_roles]), None)
    async for child in session.subagents():
        assert_type(child, Subagent)
        turn = child.turn
        assert_type(turn, Turn[str] | None)
        assert_type(await child.start("hello"), Turn[str])
        if turn is not None:
            assert_type(await turn.get_result(), Completed[str])
            assert_type(turn.events(), EventStream)
            assert_type(await turn.steer("detail"), None)
            assert_type(await child.try_cancel(turn), bool)


def observation_families(started: ToolStarted, finished: ToolFinished) -> None:
    assert_type(started.family, Literal["function", "mcp"])
    assert_type(finished.family, Literal["function", "mcp"])
    assert_type(
        ToolStarted(
            session_id="session",
            submission_id="submission",
            turn_id="turn",
            call_id="call",
            name="search",
            namespace="mcp__docs",
            family="mcp",
        ),
        ToolStarted,
    )
    assert_type(
        ToolFinished(
            session_id="session",
            submission_id="submission",
            turn_id="turn",
            call_id="call",
            name="search",
            namespace="mcp__docs",
            status="completed",
            family="mcp",
        ),
        ToolFinished,
    )


@dataclass
class Application:
    tenant: str


@dataclass
class AuditContext:
    account: str


def composition_types(environment: EnvironmentSource[AuditContext]) -> None:
    child = Agent[AuditContext](instructions="Answer.", tools=[])

    @function_tool
    async def audit(outer: ToolContext[Application], invoice_id: str) -> str:
        async def handle(request: agents.Interaction) -> agents.ActionReply[Any]:
            assert_type(outer.context, Application)
            assert_type(request.call, agents.ToolCallInfo | None)
            if isinstance(request, ActionRequested):
                assert_type(request.call, agents.ToolCallInfo)
                assert_type(request.subagent_id, str | None)
                return request.reply(outer.context.tenant)
            elif isinstance(request, agents.ApprovalRequest):
                assert_type(request.call, agents.ToolCallInfo | None)
                assert_type(request.subagent_id, str | None)
                return request.reply("allow_once")
            elif isinstance(request, agents.VerificationRequest):
                assert_type(request.call, agents.ToolCallInfo | None)
                assert_type(request.subagent_id, str | None)
                return request.reply(
                    agents.VerificationProof(credential_id="credential", signature="c2lnbmF0dXJl")
                )
            else:
                assert_type(request, agents.ElicitationRequest)
                assert_type(request.call, agents.ToolCallInfo | None)
                assert_type(request.subagent_id, str | None)
                return request.reply({"answer": outer.context.tenant})

        result = await child.run(
            f"Audit {invoice_id}",
            context=AuditContext(outer.context.tenant),
            environment=environment,
            on_interaction=handle,
        )
        assert_type(result, Completed[str])
        return result.final_output

    parent = Agent[Application](instructions="Review.", tools=[audit])
    assert_type(parent, Agent[Application, str])


async def executor_policy(context: ExecutorContext[Application], value: Any) -> Any:
    assert_type(context.context, Application)
    assert_type(context.submission, Turn[Any] | ChildInvocation)
    assert_type(context.call_id, str | None)
    assert_type(context.cell_id, str | None)
    assert_type(context.session, Session[Any, Any])
    assert_type(context.agent, Agent[Any, Any])
    assert_type(context.agent_role, str | None)
    assert_type(context.model, str | None)
    return ReplaceArguments(value)


typed_executor = Environment(
    url="wss://executor.example.invalid/session",
    cwd="/workspace",
    tools=["exec_command"],
    transform_arguments=executor_policy,
    transform_result=executor_policy,
)
assert_type(typed_executor, Environment[Application])
typed_executor_agent = Agent[Application](
    instructions="Use tools.", tools=[], programmatic_tool_calling=True
)
assert_type(typed_executor_agent, Agent[Application, str])


async def typed_environment_context() -> None:
    async with typed_executor_agent.session(
        context=Application("tenant"), environment=typed_executor
    ) as session:
        assert_type(
            await session.update(
                tools=[typed_executor, ToolRef("exec", "functions"), ToolRef("wait", "functions")]
            ),
            None,
        )
        turn = await session.submit("read")
        assert_type(turn, Turn[str])
        assert_type(await turn.get_result(), Completed[str])
    assert_type(
        await typed_executor_agent.run(
            "read", context=Application("tenant"), environment=typed_executor
        ),
        Completed[str],
    )
    assert_type(
        typed_executor_agent.run_sync(
            "read", context=Application("tenant"), environment=typed_executor
        ),
        Completed[str],
    )


@asynccontextmanager
async def typed_executor_lease() -> AsyncIterator[Environment[Application]]:
    yield typed_executor


async def typed_standard_environment_manager() -> None:
    async with typed_executor_agent.session(
        context=Application("tenant"), environment=typed_executor_lease()
    ) as session:
        assert_type(await (await session.submit("read")).get_result(), Completed[str])
    assert_type(
        await typed_executor_agent.run(
            "read",
            context=Application("tenant"),
            environment=typed_executor_lease(),
        ),
        Completed[str],
    )
    assert_type(
        typed_executor_agent.run_sync(
            "read", context=Application("tenant"), environment=typed_executor_lease()
        ),
        Completed[str],
    )


class Answer(BaseModel):
    answer: str


class Choice(RootModel[int | str]):
    pass


@function_tool
async def lookup(context: ToolContext[Application], key: str) -> str:
    return context.context.tenant + key


@function_tool(name="named_lookup")
async def named_lookup(context: ToolContext[Application], key: str) -> str:
    return context.context.tenant + key


@function_tool
async def plain(key: str) -> str:
    return key


@function_tool
def sync_lookup(context: ToolContext[Application], key: str) -> str:
    return context.context.tenant + key


sync_agent = Agent(instructions="Use the tool.", tools=[sync_lookup], output_type=Answer)
assert_type(sync_agent, Agent[Application, Answer])


application_tool: Tool[Application] = lookup
named_application_tool: Tool[Application] = named_lookup
plain_tool: Tool[object] = plain
assert_type(lookup.callback_timeout, float | None)
agent = Agent(instructions="Use the tool.", tools=[plain, lookup], output_type=Answer)
assert_type(agent, Agent[Application, Answer])
assert_type(Agent(instructions="Use the tool.", tools=[lookup, plain]), Agent[Application, str])
roster: list[Tool[Application]] = [lookup, plain]
assert_type(lookup.ref, ToolRef)
assert_type(Agent(instructions="Use these tools.", tools=roster), Agent[Application, str])


async def opening_tools_types() -> None:
    reusable = Agent[Application, Answer](instructions="Answer.", output_type=Answer)
    async with reusable.session(context=Application(tenant="a"), tools=roster) as opened:
        assert_type(opened, Session[Application, Answer])
    async with reusable.session(context=Application(tenant="b"), tools=[plain, lookup]) as opened:
        assert_type(opened, Session[Application, Answer])
    async with Agent(instructions="Answer.").session(tools=None) as empty:
        assert_type(empty, Session[None, str])


assert_type(ToolOutput.image("data:image/png;base64,aW1hZ2U="), ToolOutput)
native_items: list[dict[str, Any]] = [{"type": "inputText", "text": "retained"}]
assert_type(ToolOutput(native_items, False), ToolOutput)
native_result = ToolOutput(content_items=native_items)
assert_type(native_result, ToolOutput)
native_result.content_items.append({"type": "inputText", "text": "more"})
native_result.content_items = native_items
native_result.success = False
assert_type(Agent(instructions="Answer."), Agent[None, str])
assert_type(Agent(instructions="Answer.", tools=[plain]), Agent[object, str])
assert_type(Agent(instructions="Answer.", tools=[plain], output_type=Answer), Agent[object, Answer])
assert_type(Agent(instructions="Answer.", output_type=Answer), Agent[None, Answer])
assert_type(Agent(instructions="Count.", output_type=int), Agent[None, int])
assert_type(Agent(instructions="List counts.", output_type=list[int]), Agent[None, list[int]])
assert_type(Agent(instructions="Choose.", output_type=Choice), Agent[None, Choice])
assert_type(Agent(instructions="Return null.", output_type=type(None)), Agent[None, None])
assert_type(Agent(instructions="Report.", output_type=NativeOutput(Answer)), Agent[None, Answer])
assert_type(Agent(instructions="Report.", output_type=PromptedOutput(Answer)), Agent[None, Answer])


async def project_context(request: ModelRequestContext[Application]) -> Sequence[dict[str, Any]]:
    assert_type(request.context, Application)
    assert_type(request.items, tuple[dict[str, Any], ...])
    assert_type(request.purpose, Literal["turn", "compaction", "output_repair"])
    return request.items


assert_type(
    Agent(instructions="Answer.", transform_context=project_context), Agent[Application, str]
)
assert_type(
    Agent(instructions="Answer.", output_type=Answer, transform_context=project_context),
    Agent[Application, Answer],
)

assert_type(Agent(instructions="Answer.").with_output_type(NativeOutput(int)), Agent[None, int])
assert_type(
    Agent(instructions="Answer.").with_output_type(PromptedOutput(Answer)), Agent[None, Answer]
)


async def per_root_outputs(session: Session[None, Answer]) -> None:
    inherited = await session.submit("inherit")
    assert_type(inherited, Turn[Answer])
    plain = await session.submit("text", output_type=str)
    assert_type(plain, Turn[str])
    native = await session.submit("count", output_type=NativeOutput(int))
    assert_type(native, Turn[int])
    prompted = await session.submit("report", output_type=PromptedOutput(Answer))
    assert_type(prompted, Turn[Answer])
    assert_type(await plain.get_result(), Completed[str])
    assert_type(await native.get_result(), Completed[int])
    assert_type(session.active_turn, Turn[Any] | None)
    assert_type(session.restored_turn, Turn[Any] | None)
    assert_type(await session.checkpoint_status(kind="manual_wait"), CheckpointStatus)


assert_type(Agent[Application](instructions="Answer."), Agent[Application, str])
assert_type(Agent[Application](instructions="Answer.", tools=[plain]), Agent[Application, str])
assert_type(Agent[object](instructions="Answer.", tools=[plain]), Agent[object, str])
assert_type(
    Agent[Application, Answer](instructions="Answer.", output_type=Answer),
    Agent[Application, Answer],
)


class Specialist(Agent[None, str]):
    def __init__(self, label: str = "Specialize.") -> None:
        super().__init__(instructions=label)


class StructuredSpecialist(Agent[Application, Answer]):
    def __init__(self, label: str) -> None:
        super().__init__(instructions=label, tools=[plain, lookup], output_type=Answer)


assert_type(Specialist(), Specialist)
assert_type(Specialist("Specialize."), Specialist)
assert_type(StructuredSpecialist("Answer."), StructuredSpecialist)


async def run() -> None:
    count = await Agent(instructions="Count.", output_type=int).run("Count once.")
    assert_type(count, Completed[int])
    assert_type(count.final_output, int)
    async with Agent(instructions="List counts.", output_type=list[int]).session() as numbers:
        assert_type(numbers, Session[None, list[int]])
        counts = await (await numbers.submit("Count twice.")).get_result()
        assert_type(counts.final_output, list[int])
    choice = await Agent(instructions="Choose.", output_type=Choice).run("Choose once.")
    assert_type(choice.final_output, Choice)
    assert_type(choice.final_output.root, int | str)
    assert_type(await agent.run("input", context=Application("tenant")), Completed[Answer])
    async with Agent(instructions="Answer.", tools=[plain]).session() as plain_session:
        assert_type(plain_session, Session[object, str])
    async with Agent[Application](instructions="Answer.").session(
        context=Application("tenant")
    ) as explicit_session:
        assert_type(explicit_session, Session[Application, str])
    async with Agent[object](instructions="Answer.", tools=[plain]).session(
        context=object()
    ) as object_session:
        assert_type(object_session, Session[object, str])
    async with agent.session(context=Application("tenant")) as session:
        assert_type(session, Session[Application, Answer])
        assert_type(await session.submit("read", tools=[plain, lookup]), Turn[Answer])
        assert_type(await session.submit("read", tools=[lookup.ref]), Turn[Answer])
        assert_type(await session.compact(), None)
        assert_type(await session.compact(summary="Summary of settled work."), None)
        assert_type(await session.update(instructions="Changed instructions."), None)
        assert_type(await session.update(instructions=""), None)
        assert_type(await session.update(model=None), None)
        assert_type(await session.update(), None)
        assert_type(await session.update(tools=[]), None)
        assert_type(await session.update(tools=[plain, lookup]), None)
        assert_type(
            await session.update(model="gpt-5.5", model_settings={"reasoning_effort": "low"}),
            None,
        )
        assert_type(await session.submit("read", tools=[plain]), Turn[Answer])
        assert_type(await anext(session.subagents()), Subagent)
        assert_type(await anext(session.turns()), RecordedTurn)
        item = await anext(session.items())
        assert_type(item["id"], str)
        assert_type(item["turn_id"], str)
        if item["type"] == "function_call_output":
            assert_type(item["call_id"], str)
        handle = await session.submit("input")
        assert_type(handle, Turn[Answer])
        result = await handle.get_result()
        assert_type(result, Completed[Answer])
        assert_type(result.final_output, Answer)
        assert_type(result.turn, Turn[Answer])
    assert_type(await agent.run("input", context=Application("tenant")), Completed[Answer])
    assert_type(
        await StructuredSpecialist("Answer.").run("input", context=Application("tenant")),
        Completed[Answer],
    )
    input: UserInput = {
        "role": "user",
        "content": [
            InputText(type="input_text", text="Describe"),
            InputImage(type="input_image", image_url="data:image/png;base64,synthetic-typing-only"),
        ],
    }
    assert_type(await agent.run(input, context=Application("tenant")), Completed[Answer])


def sync() -> None:
    assert_type(agent.run_sync("input", context=Application("tenant")), Completed[Answer])
    assert_type(agent.run_sync("input", context=Application("tenant")), Completed[Answer])
    assert_type(Agent(instructions="Answer.", tools=[plain]).run_sync("input"), Completed[str])
    assert_type(
        agent.run_sync({"role": "user", "content": "input"}, context=Application("tenant")),
        Completed[Answer],
    )


async def one_shot_typed_context() -> None:
    context = Application("tenant")
    async with NativeProvider() as provider:
        assert_type(provider, NativeProvider)
        assert_type(agent.validate(), None)
        async with agent.session(
            context=context,
            provider=provider,
            model="gpt-5.2",
            model_settings={"reasoning_effort": "high", "service_tier": None},
        ) as session:
            assert_type(session, Session[Application, Answer])
            assert_type(await session.update(model="gpt-5.2", model_settings={}), None)
            assert_type(await session.update(instructions="Answer."), None)
        assert_type(
            await agent.run("answer", context=context, provider=provider, model_settings={}),
            Completed[Answer],
        )
        assert_type(
            await agent.run(
                "answer",
                context=context,
                provider=provider,
                model="gpt-5.2",
                model_settings={"service_tier": None},
            ),
            Completed[Answer],
        )


def one_shot_sync_typed_context() -> None:
    context = Application("tenant")
    provider = NativeProvider()
    assert_type(
        agent.run_sync(
            "answer",
            context=context,
            provider=provider,
            model="gpt-5.2",
            model_settings={"service_tier": None},
        ),
        Completed[Answer],
    )
    assert_type(
        agent.run_sync(
            "answer",
            context=context,
            provider=provider,
            model="gpt-5.2",
            model_settings={"reasoning_effort": "high"},
        ),
        Completed[Answer],
    )


assert_type(
    Agent[Application, Answer](instructions="Use tools.", tools=[lookup], output_type=Answer),
    Agent[Application, Answer],
)


async def environment_source_checklist_types(
    source: "EnvironmentSource[None]", agent: Agent[None, str]
) -> None:
    async with agent.session(environment=source) as session:
        assert_type(await session.checklist(), ChecklistState | None)
    assert_type(await agent.run("Inspect", environment=source), Completed[str])
    Agent[None, str](instructions="Inspect.", tools=[Checklist()])


async def local_native_goal_types(storage: LocalStorage) -> None:
    agent = Agent[Application, Answer](
        instructions="Manage the requested native goal.",
        tools=[NativeGoals(), lookup],
        output_type=Answer,
    )
    async with agent.session(context=Application("tenant"), storage=storage) as session:
        assert_type(session, Session[Application, Answer])
        goal = await session.goal()
        assert_type(goal, NativeGoal | None)
        if goal is not None:
            assert_type(goal["threadId"], str)
            assert_type(goal["tokensUsed"], int)
            assert_type(goal.get("tokenBudget"), int | None)
            assert_type(
                goal["status"],
                Literal["active", "paused", "blocked", "usageLimited", "budgetLimited", "complete"],
            )
        assert_type(await (await session.submit("Pause the goal.")).get_result(), Completed[Answer])


def select_step(current: Turn[Any], *, step_index: int) -> Sequence[Tool[Application]] | None:
    assert_type(current, Turn[Any])
    assert_type(step_index, int)
    return [lookup] if step_index == 0 else None


async def select_step_async(current: Turn[Any], *, step_index: int) -> list[ToolRef]:
    return [lookup.ref]


def select_step_awaitable(current: Turn[Any], *, step_index: int) -> Awaitable[list[ToolRef]]:
    return select_step_async(current, step_index=step_index)


async def before_step_types(session: Session[Application, Answer]) -> None:
    assert_type(await session.submit("select", before_step=select_step), Turn[Answer])
    assert_type(
        await session.submit("select", output_type=int, before_step=select_step_async), Turn[int]
    )
    assert_type(
        await agent.run("select", context=Application("a"), before_step=select_step_awaitable),
        Completed[Answer],
    )

    assert_type(
        agent.run_sync("select", context=Application("a"), before_step=select_step),
        Completed[Answer],
    )


async def trace_context_types(agent: Agent[None, str], ctx: ToolContext[None]) -> None:
    parent = TraceContext(traceparent="00-11111111111111111111111111111111-2222222222222222-01")
    headers: dict[str, str | None] = {"traceparent": parent.traceparent, "tracestate": None}
    assert_type(ctx.trace_context, TraceContext | None)
    assert_type(parent.to_headers(), dict[str, str])
    async with agent.session(
        instruction_files=(), reconciled_calls=(), parent_trace=parent
    ) as session:
        assert_type(session, Session[None, str])
        assert_type(
            await session.submit("Read", writable_roots=[], parent_trace=headers), Turn[str]
        )
        assert_type(await session.submit("Read", output_type=int, parent_trace=parent), Turn[int])
        assert_type(
            await session.submit("Steer", if_busy="steer", writable_roots=None, parent_trace=None),
            Turn[Any],
        )
    assert_type(await agent.run("Read", parent_trace=headers), Completed[str])
    assert_type(agent.run_sync("Read", parent_trace=parent), Completed[str])
