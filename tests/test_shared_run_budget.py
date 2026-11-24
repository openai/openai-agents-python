"""Observable SDK admission limits across nested runs and approval resumes."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agents import (
    Agent,
    RunBudget,
    RunBudgetError,
    RunConfig,
    Runner,
    RunState,
    ToolExecutionConfig,
    UserError,
    tool_namespace,
)
from agents.decorators import tool
from agents.models.codex import CodexProvider
from agents.testing import ScriptedModel
from tests.test_responses import get_function_tool_call, get_text_message


async def execute(
    agent, budget, *, streamed=False, input: str | RunState[Any, Agent[Any]] = "begin"
):
    config = RunConfig(budget=budget, tracing_disabled=True)
    if not streamed:
        return await Runner.run(agent, input, run_config=config)
    result = Runner.run_streamed(agent, input, run_config=config)
    async for _ in result.stream_events():
        pass
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_parallel_function_handlers_reserve_one_slot(streamed):
    started = []

    @tool
    async def record(value: str) -> str:
        started.append(value)
        await asyncio.sleep(0)
        return value

    model = ScriptedModel(
        [
            [
                get_function_tool_call("record", '{"value":"one"}', call_id="one"),
                get_function_tool_call("record", '{"value":"two"}', call_id="two"),
            ],
        ]
    )
    with pytest.raises(RunBudgetError) as caught:
        await execute(
            Agent(name="root", model=model, tools=[record]),
            RunBudget(max_tool_starts=1),
            streamed=streamed,
        )
    assert caught.value.limit == "tool_starts"
    assert len(started) <= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_children_compete_for_shared_tool_slot(streamed):
    started = []

    @tool
    async def record() -> str:
        started.append("record")
        return "saved"

    def child(name):
        model = ScriptedModel(
            [[get_function_tool_call("record", call_id=name)], [get_text_message(name)]]
        )
        return Agent(name=name, model=model, tools=[record]).as_tool(
            name, "Run child", failure_error_function=None
        )

    parent = Agent(
        name="root",
        model=ScriptedModel(
            [
                [
                    get_function_tool_call("left", '{"input":"go"}', call_id="left"),
                    get_function_tool_call("right", '{"input":"go"}', call_id="right"),
                ],
                [get_text_message("done")],
            ]
        ),
        tools=[child("left"), child("right")],
    )
    with pytest.raises(RunBudgetError):
        await execute(
            parent, RunBudget(max_tool_starts=3, share_with_children=True), streamed=streamed
        )
    assert len(started) <= 1


@pytest.mark.asyncio
async def test_parent_tool_policy_reaches_child():
    started = []

    @tool
    def denied() -> str:
        started.append("denied")
        return "unsafe"

    nested = Agent(
        name="child", model=ScriptedModel([[get_function_tool_call("denied")]]), tools=[denied]
    ).as_tool("ask", "Ask child", failure_error_function=None)
    parent = Agent(
        name="root",
        model=ScriptedModel([[get_function_tool_call("ask", '{"input":"go"}')]]),
        tools=[nested],
    )
    with pytest.raises(RunBudgetError) as caught:
        await execute(parent, RunBudget(allowed_tools=("ask",)))
    assert caught.value.limit == "tools"
    assert not started


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("restrict_child", [False, True], ids=["inherited", "child-deny"])
async def test_child_can_deny_a_name_allowed_in_the_parent(streamed, restrict_child):
    started = []

    @tool(name_override="lookup")
    def parent_lookup() -> str:
        started.append("parent")
        return "public"

    @tool(name_override="lookup")
    def child_lookup() -> str:
        started.append("child")
        return "private"

    child = Agent(
        name="child",
        model=ScriptedModel(
            [
                [get_function_tool_call("lookup", call_id="child-lookup")],
                [get_text_message("child done")],
            ]
        ),
        tools=[child_lookup],
    )
    delegate = child.as_tool(
        "ask",
        "Ask child",
        run_config={"budget": RunBudget(allowed_tools=())} if restrict_child else None,
        failure_error_function=None,
    )
    parent = Agent(
        name="parent",
        model=ScriptedModel(
            [
                [get_function_tool_call("lookup", call_id="parent-lookup")],
                [get_function_tool_call("ask", '{"input":"go"}', call_id="ask")],
                [get_text_message("done")],
            ]
        ),
        tools=[parent_lookup, delegate],
    )

    budget = RunBudget(allowed_tools=("lookup", "ask"))
    if restrict_child:
        with pytest.raises(RunBudgetError) as caught:
            await execute(parent, budget, streamed=streamed)
        assert caught.value.limit == "tools"
        assert started == ["parent"]
    else:
        await execute(parent, budget, streamed=streamed)
        assert started == ["parent", "child"]


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("dotted_parent", [False, True])
async def test_allowlist_cannot_confuse_bare_and_namespaced_tools(streamed, dotted_parent):
    started = []

    @tool(name_override="crm.lookup")
    def bare() -> str:
        started.append("bare")
        return "bare"

    @tool
    def lookup() -> str:
        started.append("namespaced")
        return "namespaced"

    namespaced = tool_namespace(name="crm", description="CRM", tools=[lookup])[0]
    parent_tool, child_tool = (bare, namespaced) if dotted_parent else (namespaced, bare)
    child_call = (
        get_function_tool_call("lookup", namespace="crm")
        if dotted_parent
        else get_function_tool_call("crm.lookup")
    )
    child_model = ScriptedModel([[child_call], [get_text_message("child done")]])
    child = Agent(name="child", model=child_model, tools=[child_tool])
    parent_model = ScriptedModel(
        [[get_function_tool_call("ask", '{"input":"go"}')], [get_text_message("done")]]
    )
    parent = Agent(
        name="parent",
        model=parent_model,
        tools=[
            parent_tool,
            child.as_tool(
                "ask",
                "Ask child",
                run_config=RunConfig(budget=RunBudget(max_tool_starts=1)),
                failure_error_function=None,
            ),
        ],
    )
    with pytest.raises(UserError, match="RunBudget.allowed_tools"):
        await execute(parent, RunBudget(allowed_tools=("ask", "crm.lookup")), streamed=streamed)
    assert not started and not child_model.calls
    assert len(parent_model.calls) == (0 if dotted_parent else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "namespace, name", [(None, "crm.lookup"), ("sales.crm", "lookup"), ("sales", "crm.lookup")]
)
async def test_allowlist_rejects_dots_in_namespace_components_only_when_restricted(namespace, name):
    started = []

    @tool(name_override=name)
    def lookup() -> str:
        started.append("lookup")
        return "ok"

    model = ScriptedModel(
        [[get_function_tool_call(name, namespace=namespace)], [get_text_message("done")]]
    )
    agent = Agent(
        name="worker",
        model=model,
        tools=tool_namespace(name=namespace, description="CRM", tools=[lookup])
        if namespace
        else [lookup],
    )
    with pytest.raises(UserError, match="RunBudget.allowed_tools"):
        await execute(agent, RunBudget(allowed_tools=("other",)))
    assert not model.calls and not started
    assert (await execute(agent, RunBudget(max_tool_starts=1))).final_output == "done"
    assert started == ["lookup"]


@pytest.mark.asyncio
async def test_namespaced_allowlist_survives_nested_approval_and_policy_check():
    started = []

    @tool(needs_approval=True)
    def lookup() -> str:
        started.append("child")
        return "ok"

    child_model = ScriptedModel(
        [[get_function_tool_call("lookup", namespace="crm")], [get_text_message("child done")]]
    )
    child = Agent(
        name="child",
        model=child_model,
        tools=tool_namespace(name="crm", description="Child CRM", tools=[lookup]),
    )
    parent = Agent(
        name="parent",
        model=ScriptedModel(
            [[get_function_tool_call("ask", '{"input":"go"}')], [get_text_message("done")]]
        ),
        tools=[
            *tool_namespace(name="crm", description="Parent CRM", tools=[lookup]),
            child.as_tool("ask", "Ask child", failure_error_function=None),
        ],
    )
    budget = RunBudget(allowed_tools=("ask", "crm.lookup"))
    paused = (await execute(parent, budget)).to_state()
    state = await RunState.from_json(parent, paused.to_json())
    state.approve(state.get_interruptions()[0])
    with pytest.raises(UserError, match="same RunBudget"):
        await execute(parent, RunBudget(allowed_tools=("ask", "lookup")), input=state)

    @tool(name_override="crm.lookup")
    def ambiguous() -> str:
        return "unreachable"

    original = parent.tools[0]
    parent.tools[0] = ambiguous
    with pytest.raises(UserError, match="RunBudget.allowed_tools"):
        await execute(parent, budget, input=state)
    parent.tools[0] = original
    assert not started and len(child_model.calls) == 1
    assert (await execute(parent, budget, input=state)).final_output == "done"
    assert started == ["child"]


@pytest.mark.asyncio
@pytest.mark.parametrize("portable", [False, True])
async def test_approval_resume_keeps_tool_consumption(portable):
    started = []

    @tool
    def first() -> str:
        started.append("first")
        return "saved"

    @tool(needs_approval=True)
    def second() -> str:
        started.append("second")
        return "saved"

    parent = Agent(
        name="root",
        model=ScriptedModel(
            [
                [
                    get_function_tool_call("first", call_id="first"),
                    get_function_tool_call("second", call_id="second"),
                ],
                [get_text_message("done")],
            ]
        ),
        tools=[first, second],
    )
    budget = RunBudget(max_tool_starts=1)
    state = (await execute(parent, budget)).to_state()
    assert started == ["first"]
    if portable:
        state = await RunState.from_json(parent, state.to_json())
    state.approve(state.get_interruptions()[0])
    with pytest.raises(RunBudgetError):
        await execute(parent, budget, input=state)
    assert started == ["first"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["max_child_runs", "max_delegation_depth"])
async def test_zero_delegation_limit_prevents_child_model(field):
    child_model = ScriptedModel([[get_text_message("child")]])
    nested = Agent(name="child", model=child_model).as_tool(
        "ask", "Ask child", failure_error_function=None
    )
    parent = Agent(
        name="root",
        model=ScriptedModel([[get_function_tool_call("ask", '{"input":"go"}')]]),
        tools=[nested],
    )
    with pytest.raises(RunBudgetError):
        await execute(
            parent,
            RunBudget(max_child_runs=0)
            if field == "max_child_runs"
            else RunBudget(max_delegation_depth=0),
        )
    assert not child_model.calls


@pytest.mark.asyncio
async def test_child_request_budget_cannot_expand_parent():
    child_model = ScriptedModel([[get_text_message("child")]])
    nested = Agent(name="child", model=child_model).as_tool(
        "ask",
        "Ask child",
        run_config=RunConfig(budget=RunBudget(max_outer_requests=100)),
        failure_error_function=None,
    )
    parent = Agent(
        name="root",
        model=ScriptedModel([[get_function_tool_call("ask", '{"input":"go"}')]]),
        tools=[nested],
    )
    with pytest.raises(RunBudgetError) as caught:
        await execute(parent, RunBudget(max_outer_requests=1, share_with_children=True))
    assert caught.value.limit == "outer_requests"
    assert not child_model.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("portable", [False, True])
@pytest.mark.parametrize("mode", ["default", "configured", "streamed", "grandchild"])
async def test_shared_budget_rebinds_nested_approval_resume(portable, mode):
    started = []

    @tool(needs_approval=True)
    def record() -> str:
        started.append("record")
        return "saved"

    child_model = ScriptedModel(
        [[get_function_tool_call("record", call_id="inner")], [get_text_message("child")]]
    )
    child = Agent(name="child", model=child_model, tools=[record])
    if mode == "grandchild":
        child = Agent(
            name="middle",
            model=ScriptedModel(
                [
                    [get_function_tool_call("delegate", '{"input":"go"}', call_id="middle")],
                    [get_text_message("middle done")],
                ]
            ),
            tools=[child.as_tool("delegate", "Delegate", failure_error_function=None)],
        )
    child_config = RunConfig(tracing_disabled=True) if mode != "default" else None
    if mode == "grandchild":
        child_config = RunConfig(budget=RunBudget(max_tool_starts=2), tracing_disabled=True)
    nested = child.as_tool(
        "ask",
        "Ask child",
        run_config=child_config,
        on_stream=(lambda _: None) if mode == "streamed" else None,
        failure_error_function=None,
    )
    parent = Agent(
        name="root",
        model=ScriptedModel(
            [
                [get_function_tool_call("ask", '{"input":"go"}', call_id="outer")],
                [get_text_message("done")],
            ]
        ),
        tools=[nested],
    )
    starts = 3 if mode == "grandchild" else 2
    budget = RunBudget(
        max_tool_starts=starts, max_outer_requests=2 * starts, share_with_children=True
    )
    state = (await execute(parent, budget)).to_state()
    if portable:
        state = await RunState.from_json(parent, state.to_json())
    state.approve(state.get_interruptions()[0])
    result = await execute(parent, budget, input=state)
    assert result.final_output == "done"
    assert started == ["record"]
    assert result.context_wrapper._budget is not None
    assert result.context_wrapper._budget.tool_starts == starts
    assert result.context_wrapper._budget.requests == 2 * starts


@pytest.mark.asyncio
async def test_native_hidden_usage_budget_rejected_before_execution():
    with pytest.raises(UserError, match="native sampling"):
        await Runner.run(
            Agent(name="native", model="example"),
            "go",
            run_config=RunConfig(
                model_provider=CodexProvider(),
                budget=RunBudget(max_tokens=1),
                tracing_disabled=True,
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_cancelled_parent_joins_owned_child_handler(streamed):
    # Controlled suspension observes the real child tool's cleanup before parent return.
    started = asyncio.Event()
    cleaned = asyncio.Event()

    @tool
    async def wait_forever() -> str:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()
        return "unreachable"

    child = Agent(
        name="child",
        model=ScriptedModel([[get_function_tool_call("wait_forever")]]),
        tools=[wait_forever],
    )
    nested = child.as_tool("ask", "Ask child", failure_error_function=None)
    parent = Agent(
        name="root",
        model=ScriptedModel([[get_function_tool_call("ask", '{"input":"go"}')]]),
        tools=[nested],
    )
    budget = RunBudget(max_tool_starts=3, share_with_children=True)
    if streamed:
        result = Runner.run_streamed(
            parent, "begin", run_config=RunConfig(budget=budget, tracing_disabled=True)
        )

        async def drain():
            async for _ in result.stream_events():
                pass

        task = asyncio.create_task(drain())
        await asyncio.wait_for(started.wait(), timeout=2)
        result.cancel()
        await asyncio.wait_for(task, timeout=2)
    else:
        task = asyncio.create_task(execute(parent, budget))
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
    assert cleaned.is_set()


@pytest.mark.asyncio
async def test_rejected_approval_does_not_reserve_start():
    started = []

    @tool(needs_approval=True)
    def first() -> str:
        started.append("first")
        return "first"

    @tool
    def second() -> str:
        started.append("second")
        return "second"

    parent = Agent(
        name="root",
        model=ScriptedModel(
            [
                [get_function_tool_call("first", call_id="first")],
                [get_function_tool_call("second", call_id="second")],
                [get_text_message("done")],
            ]
        ),
        tools=[first, second],
    )
    budget = RunBudget(max_tool_starts=1)
    state = (await execute(parent, budget)).to_state()
    state.reject(state.get_interruptions()[0])
    assert (await execute(parent, budget, input=state)).final_output == "done"
    assert started == ["second"]


@pytest.mark.asyncio
async def test_child_local_tool_limit_remains_in_force():
    started = []

    @tool
    def record() -> str:
        started.append("record")
        return "saved"

    child = Agent(
        name="child", model=ScriptedModel([[get_function_tool_call("record")]]), tools=[record]
    )
    nested = child.as_tool(
        "ask",
        "Ask child",
        run_config=RunConfig(budget=RunBudget(max_tool_starts=0)),
        failure_error_function=None,
    )
    parent = Agent(
        name="root",
        model=ScriptedModel([[get_function_tool_call("ask", '{"input":"go"}')]]),
        tools=[nested],
    )
    with pytest.raises(RunBudgetError) as caught:
        await execute(parent, RunBudget(max_tool_starts=10, share_with_children=True))
    assert caught.value.limit == "tool_starts"
    assert not started


@pytest.mark.asyncio
async def test_child_usage_counts_against_parent_token_limit():
    from agents import Usage
    from agents.testing import ModelStep

    child_model = ScriptedModel(
        [ModelStep(output=[get_text_message("child")], usage=Usage(total_tokens=5))]
    )
    nested = Agent(name="child", model=child_model).as_tool(
        "ask", "Ask child", failure_error_function=None
    )
    parent_model = ScriptedModel(
        [
            ModelStep(
                output=[get_function_tool_call("ask", '{"input":"go"}')],
                usage=Usage(total_tokens=1),
            ),
            [get_text_message("done")],
        ]
    )
    with pytest.raises(RunBudgetError) as caught:
        await execute(
            Agent(name="root", model=parent_model, tools=[nested]),
            RunBudget(max_tokens=6, share_with_children=True),
        )
    assert caught.value.limit == "tokens"
    assert len(parent_model.calls) == 1
    assert len(child_model.calls) == 1


@pytest.mark.asyncio
async def test_grandchild_cannot_escape_ancestor_depth_through_local_budget():
    leaf_model = ScriptedModel([[get_text_message("leaf")]])
    leaf = Agent(name="leaf", model=leaf_model).as_tool(
        "leaf", "Run leaf", failure_error_function=None
    )
    child = Agent(
        name="child",
        model=ScriptedModel([[get_function_tool_call("leaf", '{"input":"go"}')]]),
        tools=[leaf],
    ).as_tool(
        "child",
        "Run child",
        run_config=RunConfig(budget=RunBudget(max_outer_requests=100)),
        failure_error_function=None,
    )
    root = Agent(
        name="root",
        model=ScriptedModel([[get_function_tool_call("child", '{"input":"go"}')]]),
        tools=[child],
    )
    with pytest.raises(RunBudgetError) as caught:
        await execute(root, RunBudget(max_delegation_depth=1))
    assert caught.value.limit == "delegation_depth"
    assert not leaf_model.calls


@pytest.mark.asyncio
async def test_shared_outer_limit_counts_completed_child_before_parent_followup():
    from agents import Usage
    from agents.testing import ModelStep

    child_model = ScriptedModel(
        [ModelStep(output=[get_text_message("child")], usage=Usage(total_tokens=5))]
    )
    nested = Agent(name="child", model=child_model).as_tool(
        "ask", "Ask child", failure_error_function=None
    )
    parent_model = ScriptedModel(
        [
            ModelStep(
                output=[get_function_tool_call("ask", '{"input":"go"}')],
                usage=Usage(total_tokens=1),
            ),
            [get_text_message("done")],
        ]
    )
    with pytest.raises(RunBudgetError) as caught:
        await execute(
            Agent(name="root", model=parent_model, tools=[nested]),
            RunBudget(max_outer_requests=2, share_with_children=True),
        )
    assert caught.value.limit == "outer_requests"
    assert len(parent_model.calls) == len(child_model.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_selected_name_obeys_outer_limit_on_adapter_without_model_attribute(streamed):
    from agents import ModelProvider, Usage
    from agents.testing import ModelStep

    model = ScriptedModel(
        [
            ModelStep(output=[get_function_tool_call("record")], usage=Usage(total_tokens=1)),
            [get_text_message("done")],
        ]
    )

    class Provider(ModelProvider):
        def get_model(self, model_name):
            assert model_name == "selected"
            return model

    @tool
    def record() -> str:
        return "saved"

    agent = Agent(name="root", model="static", tools=[record])
    config = RunConfig(
        model_provider=Provider(),
        model_selector=lambda _: "selected",
        budget=RunBudget(max_outer_requests=1),
        tracing_disabled=True,
    )
    with pytest.raises(RunBudgetError):
        if streamed:
            result = Runner.run_streamed(agent, "go", run_config=config)
            async for _ in result.stream_events():
                pass
        else:
            await Runner.run(agent, "go", run_config=config)
    assert len(model.calls) == 1
    assert agent.model == "static"


@pytest.mark.asyncio
async def test_shared_child_selection_obeys_ancestor_outer_limit():
    from agents import ModelProvider, Usage
    from agents.testing import ModelStep

    child_model = ScriptedModel(
        [ModelStep(output=[get_text_message("child")], usage=Usage(total_tokens=1))]
    )

    class Provider(ModelProvider):
        def get_model(self, model_name):
            assert model_name == "selected-child"
            return child_model

    child = Agent(name="child", model="static-child").as_tool(
        "ask",
        "Ask child",
        run_config=RunConfig(
            model_provider=Provider(),
            model_selector=lambda _: "selected-child",
            budget=RunBudget(max_outer_requests=1),
        ),
        failure_error_function=None,
    )
    root = Agent(
        name="root",
        model=ScriptedModel(
            [
                ModelStep(
                    output=[get_function_tool_call("ask", '{"input":"go"}')],
                    usage=Usage(total_tokens=1),
                ),
                [get_text_message("done")],
            ]
        ),
        tools=[child],
    )
    with pytest.raises(RunBudgetError):
        await execute(
            root,
            RunBudget(max_outer_requests=2, share_with_children=True),
        )
    assert len(child_model.calls) == 1
    assert isinstance(root.model, ScriptedModel)
    assert len(root.model.calls) == 1


@pytest.mark.asyncio
async def test_child_rejects_unlisted_hosted_tool_before_model_dispatch():
    from agents import WebSearchTool

    child_model = ScriptedModel([[get_text_message("should not run")]])
    nested = Agent(name="child", model=child_model, tools=[WebSearchTool()]).as_tool(
        "ask", "Ask child", failure_error_function=None
    )
    root = Agent(
        name="root",
        model=ScriptedModel([[get_function_tool_call("ask", '{"input":"go"}')]]),
        tools=[nested],
    )
    with pytest.raises(UserError, match="only SDK FunctionTool"):
        await execute(root, RunBudget(allowed_tools=("ask",)))
    assert not child_model.calls


@pytest.mark.asyncio
async def test_codex_backed_sdk_child_bounds_outer_requests_and_sdk_handlers(tmp_path, monkeypatch):
    # This double preserves the native wire boundary without requiring a compiled extension.
    import json
    import sys
    from collections import deque
    from types import ModuleType

    from tests.models.test_codex_provider import FakeThread

    first = FakeThread(call={"callId": "record", "tool": "record", "arguments": {}})
    queue = deque([first])
    module = ModuleType("agents.models._codex_native")

    class Manager:
        def __init__(self, *args):
            pass

        async def start_thread(self, tools, instructions, settings):
            thread = queue.popleft()
            monkeypatch.setattr(
                thread, "model", json.loads(settings)["model"] or "native-default", raising=False
            )
            return thread

        async def close(self):
            pass

    monkeypatch.setattr(module, "Manager", Manager, raising=False)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    started = []

    @tool
    def record() -> str:
        started.append("record")
        return "saved"

    child = Agent(name="native-child", tools=[record]).as_tool(
        "ask",
        "Ask child",
        run_config=RunConfig(
            model_provider=CodexProvider(cwd=tmp_path),
            budget=RunBudget(max_outer_requests=1, max_tool_starts=1),
        ),
        failure_error_function=None,
    )
    root = Agent(
        name="root",
        model=ScriptedModel([[get_function_tool_call("ask", '{"input":"go"}')]]),
        tools=[child],
    )
    with pytest.raises(RunBudgetError) as caught:
        await execute(root, RunBudget(max_tool_starts=1))
    assert caught.value.limit == "outer_requests"
    assert started == ["record"]
    assert first.closed and not queue


@pytest.mark.asyncio
@pytest.mark.parametrize("sdk_workspace", [False, True])
@pytest.mark.parametrize("streamed", [False, True])
async def test_shared_codex_child_rejects_unenforceable_native_delegation(
    tmp_path, sdk_workspace, streamed
):
    provider = CodexProvider(cwd=tmp_path)
    child = Agent(name="native-child").as_tool(
        "ask",
        "Ask child",
        run_config=RunConfig(
            model_provider=provider,
            tool_execution=ToolExecutionConfig(workspace_execution="sdk")
            if sdk_workspace
            else None,
        ),
        failure_error_function=None,
    )
    root = Agent(
        name="root",
        model=ScriptedModel([[get_function_tool_call("ask", '{"input":"go"}')]]),
        tools=[child],
    )
    with pytest.raises(UserError, match="controlled native delegation"):
        await execute(
            root,
            RunBudget(max_tool_starts=2, share_with_children=True),
            streamed=streamed,
        )
    assert provider._manager is None


@pytest.mark.asyncio
async def test_tool_start_limit_rejects_uncounted_custom_handler():
    from agents import CustomTool

    async def execute_custom(ctx, value):
        pytest.fail("The unsupported handler must not run.")

    model = ScriptedModel([[get_text_message("should not run")]])
    root = Agent(
        name="root",
        model=model,
        tools=[CustomTool(name="raw", description="Raw", on_invoke_tool=execute_custom)],
    )
    with pytest.raises(UserError, match="max_tool_starts supports SDK FunctionTool"):
        await execute(root, RunBudget(max_tool_starts=1))
    assert not model.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["extra_body", "extra_args", "deferred"])
async def test_allowlist_rejects_provider_override_and_deferred_escape(surface):
    from agents import ModelSettings

    @tool
    def record() -> str:
        return "saved"

    if surface == "deferred":
        record.defer_loading = True
        settings = ModelSettings()
    elif surface == "extra_body":
        settings = ModelSettings(extra_body={"tools": [{"type": "web_search"}]})
    else:
        settings = ModelSettings(extra_args={"tools": [{"type": "web_search"}]})
    model = ScriptedModel([[get_text_message("should not run")]])
    root = Agent(name="root", model=model, tools=[record], model_settings=settings)
    with pytest.raises(UserError, match="RunBudget.allowed_tools"):
        await execute(root, RunBudget(allowed_tools=("record",)))
    assert not model.calls


@pytest.mark.asyncio
async def test_upstream_six_key_checkpoint_restores_without_extension_options():
    @tool(needs_approval=True)
    def record() -> str:
        return "saved"

    model = ScriptedModel([[get_function_tool_call("record")], [get_text_message("done")]])
    root = Agent(name="upstream checkpoint", model=model, tools=[record])
    budget = RunBudget(max_model_calls=2)
    saved = (await execute(root, budget)).to_state().to_json()
    # These exact names and defaults come from the merged upstream checkpoint contract.
    assert saved["context"]["budget"] == {
        "max_requests": 2,
        "max_tokens": None,
        "missing_usage": "stop",
        "requests": 1,
        "tokens": 0,
        "unavailable": False,
    }
    assert budget.include_failed_attempt_usage is False
    restored = await RunState.from_json(root, saved)
    restored.approve(restored.get_interruptions()[0])
    result = await execute(root, budget, input=restored)
    assert result.final_output == "done" and len(model.calls) == 2
    assert result.context_wrapper._budget is not None
    assert result.context_wrapper._budget.requests == 2
    assert result.to_state().to_json()["context"]["budget"]["max_requests"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize(
    "field,ceiling", [("max_model_calls", "model_calls"), ("max_outer_requests", "outer_requests")]
)
async def test_python_outer_option_preserves_upstream_model_call_contract(streamed, field, ceiling):
    model = ScriptedModel(
        [
            [get_function_tool_call("unknown")],
            [get_function_tool_call("unknown")],
            [get_text_message("must not run")],
        ]
    )
    config = RunConfig(
        budget=RunBudget(max_model_calls=2)
        if field == "max_model_calls"
        else RunBudget(max_outer_requests=2),
        tracing_disabled=True,
        tool_not_found_behavior="return_error_to_model",
    )
    root = Agent(name="limited", model=model)
    with pytest.raises(RunBudgetError) as caught:
        if streamed:
            result = Runner.run_streamed(root, "start", run_config=config, max_turns=None)
            async for _ in result.stream_events():
                pass
        else:
            await Runner.run(root, "start", run_config=config, max_turns=None)
    assert caught.value.limit == ceiling
    assert len(model.calls) == 2
