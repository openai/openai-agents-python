import pytest

from agents import (
    Agent,
    ModelBehaviorError,
    ModelSettings,
    RunConfig,
    RunHooks,
    Runner,
    RunState,
    ToolDeclaration,
    ToolExecutionConfig,
    UserError,
    function_tool,
)
from agents.sandbox import ExecResult, SandboxAgent
from agents.sandbox.capabilities import Filesystem, Shell
from agents.testing import ScriptedModel, assistant_message, function_call, scripted_sandbox_session

from .mcp.helpers import FakeMCPServer

pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize("streamed", [False, True])]


async def run(streamed, agent, config, prompt="go", **kwargs):
    if streamed:
        result = Runner.run_streamed(agent, prompt, run_config=config, **kwargs)
        async for _ in result.stream_events():
            pass
        return result
    return await Runner.run(agent, prompt, run_config=config, **kwargs)


async def describe(ctx, declaration):
    return declaration.description


def configure(policy=describe, **kwargs):
    return RunConfig(tool_execution=ToolExecutionConfig(prepare_tool_declaration=policy, **kwargs))


def call(name):
    return function_call(name, {}, call_id=name)


@pytest.mark.parametrize("success", [False, True])
async def test_unlock_after_success_and_preserve_original_tool(streamed, success):
    effects = []
    seen = []

    @function_tool
    async def inspect_site():
        """Inspect the site."""
        if not success:
            raise ValueError("Inspection failed")
        effects.append("inspected")

    peer = FakeMCPServer(server_name="Site reports", tool_filter=lambda ctx, t: bool(effects))
    peer.add_tool("publish.report", {})

    async def prepare(ctx, declaration):
        seen.append(declaration)
        return "Ready for " + declaration.name

    model = ScriptedModel(
        [
            [call("inspect_site")],
            [call("mcp_Site_reports__publish_report")],
            [assistant_message("done")],
        ]
    )
    agent = Agent(
        name="host",
        tools=[inspect_site],
        mcp_servers=[peer],
        mcp_config={"include_server_in_tool_names": True},
        model=model,
        model_settings=ModelSettings(tool_choice="required"),
    )
    if success:
        assert (await run(streamed, agent, configure(prepare))).final_output == "done"
        assert peer.tool_calls == ["publish.report"]
        assert any(
            d.mcp_server_name == "Site reports" and d.mcp_tool_name == "publish.report"
            for d in seen
        )
    else:
        with pytest.raises(ModelBehaviorError):
            await run(streamed, agent, configure(prepare))
        assert not peer.tool_calls
        assert all(d.name == "inspect_site" for d in seen)
    assert [t.name for t in model.calls[0].tools] == ["inspect_site"]
    assert model.calls[0].tools[0].description == "Ready for inspect_site"
    assert model.calls[0].tools[0].params_json_schema == inspect_site.params_json_schema
    assert model.calls[1].model_settings.tool_choice is None
    assert inspect_site.description == "Inspect the site."


@pytest.mark.parametrize("choice", [None, "publish", "required"])
async def test_omission_limits_sampling_and_routing(streamed, choice):
    peer = FakeMCPServer()
    peer.add_tool("publish", {})

    async def omit(ctx, declaration):
        return None

    model = ScriptedModel([[call("publish")]])
    agent = Agent(
        name="host",
        mcp_servers=[peer],
        model=model,
        model_settings=ModelSettings(tool_choice=choice),
    )
    with pytest.raises(ModelBehaviorError if choice is None else UserError):
        await run(streamed, agent, configure(omit))
    assert not peer.tool_calls
    if choice is None:
        assert model.calls[0].tools == []
    else:
        assert not model.calls


async def test_cannot_replace_tool_or_edit_schema_through_policy(streamed):
    @function_tool(needs_approval=True)
    def publish():
        pytest.fail("unapproved call")

    async def replace(ctx, declaration):
        assert isinstance(declaration, ToolDeclaration)
        assert not hasattr(declaration, "params_json_schema")
        return publish

    model = ScriptedModel([])
    with pytest.raises(UserError, match="string or None"):
        await run(streamed, Agent(name="host", tools=[publish], model=model), configure(replace))
    assert not model.calls


@pytest.mark.parametrize("route", ["local", "mcp"])
async def test_queued_tool_rechecks_original_predicate(streamed, route):
    effects = []

    @function_tool
    async def revoke():
        effects.append("revoked")

    @function_tool(is_enabled=lambda ctx, agent: not effects)
    async def publish():
        effects.append("published")

    peer = FakeMCPServer(tool_filter=lambda ctx, t: not effects)
    peer.add_tool("publish", {})
    model = ScriptedModel([[call("revoke"), call("publish")]])
    agent = Agent(
        name="host",
        tools=[revoke, publish] if route == "local" else [revoke],
        mcp_servers=[peer] if route == "mcp" else [],
        model=model,
    )
    with pytest.raises((UserError, ModelBehaviorError), match="eligible"):
        await run(streamed, agent, configure(max_function_tool_concurrency=1))
    assert effects == ["revoked"]
    assert not peer.tool_calls


@pytest.mark.parametrize("route", ["local", "mcp"])
@pytest.mark.parametrize("change", ["memory", "none", "missing", "revoked", "replacement"])
async def test_approval_binding_across_pause(streamed, route, change):
    effects = []
    eligible = True

    @function_tool(needs_approval=True)
    def publish():
        effects.append("local")

    def server():
        peer = FakeMCPServer(require_approval="always")
        peer.add_tool("publish", {})
        return peer

    async def prepare(ctx, declaration):
        return "Publish" if eligible else None

    peer = server()
    config = configure(prepare)
    model = ScriptedModel([[call("publish")], [assistant_message("done")]])
    agent = Agent(
        name="host",
        tools=[publish] if route == "local" else [],
        mcp_servers=[peer] if route == "mcp" else [],
        model=model,
    )
    first = await run(streamed, agent, config)
    state = first.to_state()
    state.approve(first.interruptions[0])
    if change not in ("replacement", "memory"):
        state = await RunState.from_json(agent, state.to_json())
    if change in ("none", "memory"):
        result = await run(streamed, agent, config, state)
        assert result.final_output == "done"
        assert effects + peer.tool_calls == (["local"] if route == "local" else ["publish"])
        return
    if change == "replacement":
        if route == "local":
            agent.tools = [
                function_tool(lambda: effects.append("replacement"), name_override="publish")
            ]
        else:
            agent.mcp_servers = [server()]
    if change == "revoked":
        eligible = False
    with pytest.raises((UserError, ModelBehaviorError)):
        await run(streamed, agent, RunConfig() if change == "missing" else config, state)
    assert not effects and not peer.tool_calls
    assert all(not s.tool_calls for s in agent.mcp_servers)


async def test_recheck_after_mcp_metadata(streamed):
    async def metadata(ctx):
        peer.tool_filter = {"blocked_tool_names": ["publish"]}

    peer = FakeMCPServer(tool_meta_resolver=metadata)
    peer.add_tool("publish", {})
    agent = Agent(name="host", mcp_servers=[peer], model=ScriptedModel([[call("publish")]]))
    with pytest.raises(UserError, match="eligible"):
        await run(streamed, agent, configure())
    assert not peer.tool_calls


async def test_recheck_after_tool_start(streamed):
    @function_tool
    def publish():
        pytest.fail("revoked")

    class Hooks(RunHooks):
        async def on_tool_start(self, context, agent, tool):
            tool.is_enabled = False

    agent = Agent(name="host", tools=[publish], model=ScriptedModel([[call("publish")]]))
    with pytest.raises(ModelBehaviorError, match="eligible"):
        await run(streamed, agent, configure(), hooks=Hooks())


async def test_selected_description_does_not_replace_approval_or_argument_edit(streamed):
    calls = []

    @function_tool(needs_approval=True)
    def publish(value: str):
        calls.append(value)

    async def edit(call):
        assert call.tool is publish
        calls.append("edit")
        return {"value": "approved"}

    async def policy(ctx, declaration):
        return "Publish the reviewed value."

    model = ScriptedModel(
        [
            [function_call("publish", {"value": "draft"}, call_id="one")],
            [assistant_message("done")],
        ]
    )
    agent = Agent(name="host", tools=[publish], model=model)
    config = configure(policy, prepare_tool_input=edit)
    result = await run(streamed, agent, config)
    assert calls == ["edit"]
    assert model.calls[0].tools[0].description == "Publish the reviewed value."
    state = result.to_state()
    state.approve(result.interruptions[0])
    restored = await RunState.from_json(agent, state.to_json())
    await run(streamed, agent, config, restored)
    assert calls == ["edit", "approved"]


async def test_pending_policy_cannot_be_saved_under_an_older_schema(streamed):
    @function_tool(needs_approval=True)
    def publish():
        pytest.fail("not approved")

    agent = Agent(name="host", tools=[publish], model=ScriptedModel([[call("publish")]]))
    first = await run(streamed, agent, configure())
    data = first.to_state().to_json()
    data["$schemaVersion"] = "1.22"
    with pytest.raises(UserError, match="schema 1.23"):
        await RunState.from_json(agent, data)


async def test_omitting_collision_winner_does_not_expose_loser(streamed):
    @function_tool
    def publish():
        pytest.fail("withheld")

    @function_tool
    def inspect_site():
        return "ready"

    peer = FakeMCPServer()
    peer.add_tool("publish", {})

    async def policy(ctx, declaration):
        return None if declaration.name == "publish" else declaration.description

    model = ScriptedModel([[call("publish")]])
    agent = Agent(name="host", tools=[publish, inspect_site], mcp_servers=[peer], model=model)
    with pytest.raises(ModelBehaviorError):
        await run(streamed, agent, configure(policy))
    assert [tool.name for tool in model.calls[0].tools] == ["inspect_site"]
    assert not peer.tool_calls


@pytest.mark.parametrize("field", ["approval", "schema"])
async def test_authority_edits_during_preparation_fail_before_sampling(streamed, field):
    @function_tool(needs_approval=True)
    def publish():
        pytest.fail("unauthorized")

    async def policy(ctx, declaration):
        if field == "schema":
            publish.params_json_schema.clear()
        else:
            publish.needs_approval = False
        return "Publish"

    model = ScriptedModel([[call("publish")]])
    with pytest.raises(UserError, match="changed"):
        await run(streamed, Agent(name="host", tools=[publish], model=model), configure(policy))
    assert not model.calls


@pytest.mark.parametrize("approved", [False, True])
async def test_missing_policy_preserves_managed_approval(streamed, approved):
    from agents.memory.resumable_session import ResumableSession

    from .memory.test_resumable_session import MemoryStore

    store = MemoryStore()
    session = await ResumableSession.create(store)
    peer = FakeMCPServer(require_approval="always")
    peer.add_tool("publish", {})
    model = ScriptedModel([[call("publish")], [assistant_message("done")]])
    agent = Agent(name="host", mcp_servers=[peer], model=model)
    await run(streamed, agent, configure(), session=session)
    saved = store.records[session.session_id]
    restored = await ResumableSession.resume(store, session.session_id)
    state = await restored.load_state(agent)
    pending = state.get_interruptions()[0]
    if approved:
        state.approve(pending)
        with pytest.raises(UserError, match="prepare_tool_declaration"):
            await run(streamed, agent, RunConfig(), state)
        assert store.records[session.session_id] == saved
        assert not peer.tool_calls and len(model.calls) == 1
        restored = await ResumableSession.resume(store, session.session_id)
        state = await restored.load_state(agent)
        state.approve(state.get_interruptions()[0])
    else:
        state.reject(pending)
    result = await run(streamed, agent, configure() if approved else RunConfig(), state)
    assert result.final_output == "done"
    assert peer.tool_calls == (["publish"] if approved else [])


@pytest.mark.parametrize("choice", ["required", "web_search_preview"])
async def test_prompt_managed_choice_passes_through(streamed, choice):
    model = ScriptedModel([[assistant_message("done")]])
    agent = Agent(
        name="host",
        model=model,
        prompt={"id": "pmpt_example"},
        model_settings=ModelSettings(tool_choice=choice),
    )
    await run(streamed, agent, configure())
    assert model.calls[0].model_settings.tool_choice == choice


async def test_approval_requires_same_bound_policy_owner(streamed):
    class Owner:
        async def decide(self, ctx, declaration):
            return declaration.description

    owner = Owner()
    effects = []

    @function_tool(needs_approval=True)
    def publish():
        effects.append("published")

    model = ScriptedModel([[call("publish")], [assistant_message("done")]])
    agent = Agent(name="host", model=model, tools=[publish])
    first = await run(streamed, agent, configure(owner.decide))
    state = first.to_state()
    state.approve(first.interruptions[0])
    with pytest.raises(UserError, match="policy changed"):
        await run(streamed, agent, configure(Owner().decide), state)
    assert not effects
    await run(streamed, agent, configure(owner.decide), state)
    assert effects == ["published"]


@pytest.mark.parametrize("batch", [False, True])
async def test_preconverted_tools_require_agent_discovery(streamed, batch):
    from agents import RunContextWrapper
    from agents.mcp import MCPUtil

    peer = FakeMCPServer()
    peer.add_tool("publish", {})
    model = ScriptedModel([[call("publish")], [assistant_message("done")]])
    agent = Agent(name="host", model=model)
    agent.tools = (
        await MCPUtil.get_all_function_tools([peer], False, RunContextWrapper(None), agent)
        if batch
        else [MCPUtil.to_function_tool(peer.tools[0], peer, False)]
    )
    with pytest.raises(UserError, match="agent.mcp_servers"):
        await run(streamed, agent, configure())
    assert not peer.tool_calls and not model.calls
    assert (await run(streamed, agent, RunConfig())).final_output == "done"
    assert peer.tool_calls == ["publish"]


async def test_named_choice_tracks_the_qualified_target(streamed):
    from agents import tool_namespace

    @function_tool
    def report():
        return "ready"

    async def keep_local(ctx, declaration):
        return None if declaration.namespace else declaration.description

    tools = [report, *tool_namespace(name="archive", description="Archive", tools=[report])]
    model = ScriptedModel([[call("report")]])
    agent = Agent(
        name="host",
        tools=tools,
        model=model,
        tool_use_behavior="stop_on_first_tool",
        model_settings=ModelSettings(tool_choice="archive.report"),
    )
    with pytest.raises(UserError, match="tool_choice"):
        await run(streamed, agent, configure(keep_local))
    assert not model.calls
    agent.model_settings.tool_choice = "report"
    assert (await run(streamed, agent, configure(keep_local))).final_output == "ready"


async def test_named_choice_can_still_select_hosted_search(streamed):
    from agents import WebSearchTool

    @function_tool
    def web_search_preview():
        pytest.fail("withheld local function")

    async def omit(ctx, declaration):
        return None

    search = WebSearchTool()
    model = ScriptedModel([[assistant_message("done")]])
    agent = Agent(
        name="host",
        tools=[web_search_preview, search],
        model=model,
        model_settings=ModelSettings(tool_choice="web_search_preview"),
    )
    await run(streamed, agent, configure(omit))
    assert model.calls[0].tools == [search]
    assert model.calls[0].model_settings.tool_choice == "web_search_preview"


@pytest.mark.parametrize("resume", [None, "allow", "restore", "replace", "move", "executor"])
async def test_prepared_sandbox_capability(streamed, resume):
    replace_executor = False

    def enabled(ctx, owner):
        assert owner is agent
        return True

    def shell_tools(tools):
        tools.exec_command.needs_approval = resume is not None
        if replace_executor:
            tools.exec_command.on_invoke_tool = lambda *_: pytest.fail("Replacement ran")
        tools.exec_command.is_enabled = enabled

    session = scripted_sandbox_session(
        [{"method": "exec", "result": ExecResult(stdout=b"ok", stderr=b"", exit_code=0)}]
    )
    model = ScriptedModel(
        [
            [function_call("exec_command", {"cmd": "pwd"}, call_id="one")],
            [assistant_message("done")],
        ]
    )
    agent = SandboxAgent(
        name="host", model=model, capabilities=[Shell(configure_tools=shell_tools)]
    )
    config = RunConfig(sandbox={"session": session}, tool_execution=configure().tool_execution)
    result = await run(streamed, agent, config)
    if resume:
        assert not session.calls
        state = result.to_state()
        state.approve(result.interruptions[0])
        if resume == "restore":
            state = await RunState.from_json(agent, state.to_json())
        elif resume != "allow":
            if resume == "replace":
                agent.capabilities = [Shell(configure_tools=shell_tools)]
            if resume == "move":
                agent.capabilities = [Filesystem(), *agent.capabilities]
                state = await RunState.from_json(agent, state.to_json())
            replace_executor = resume == "executor"
            with pytest.raises(
                UserError, match="no longer configured|replacement tool|capability source"
            ):
                await run(streamed, agent, config, state)
            assert not session.calls
            return
        result = await run(streamed, agent, config, state)
    assert result.final_output == "done"
    session.assert_complete()


async def test_prepared_sandbox_checks_live_public_tools(streamed):
    def enabled(ctx, owner):
        assert owner is agent
        return True

    @function_tool(is_enabled=enabled)
    def publish():
        pytest.fail("Revoked tool ran")

    class Hooks(RunHooks):
        async def on_tool_start(self, context, owner, tool):
            assert owner is agent
            agent.tools = []

    agent = SandboxAgent(
        name="host", tools=[publish], capabilities=[], model=ScriptedModel([[call("publish")]])
    )
    config = RunConfig(
        sandbox={"session": scripted_sandbox_session()}, tool_execution=configure().tool_execution
    )
    with pytest.raises(UserError, match="no longer configured"):
        await run(streamed, agent, config, hooks=Hooks())


@pytest.mark.parametrize("direct", [False, True])
async def test_prepared_sandbox_approval_cannot_change_source(streamed, direct):
    effects = []

    @function_tool(needs_approval=True)
    def exec_command(cmd: str):
        effects.append(cmd)

    def require_approval(tools):
        tools.exec_command.needs_approval = True

    shell = Shell(configure_tools=require_approval)
    session = scripted_sandbox_session(
        [{"method": "exec", "result": ExecResult(stdout=b"ok", stderr=b"", exit_code=0)}]
    )
    model = ScriptedModel(
        [
            [function_call("exec_command", {"cmd": "pwd"}, call_id="one")],
            [assistant_message("done")],
        ]
    )
    agent = SandboxAgent(
        name="host",
        model=model,
        capabilities=[] if direct else [shell],
        tools=[exec_command] if direct else [],
    )
    config = RunConfig(sandbox={"session": session}, tool_execution=configure().tool_execution)
    result = await run(streamed, agent, config)
    state = result.to_state()
    state.approve(result.interruptions[0])
    data = state.to_json()
    agent.tools = [] if direct else [exec_command]
    agent.capabilities = [shell] if direct else []
    restored = await RunState.from_json(agent, data)
    with pytest.raises(UserError, match="source"):
        await run(streamed, agent, config, restored)
    assert not session.calls and not effects


async def test_prepared_view_image_requires_capability_source(streamed):
    def require_approval(tools):
        tools.view_image.needs_approval = True

    session = scripted_sandbox_session([{"method": "read", "result": b""}])
    agent = SandboxAgent(
        name="host",
        model=ScriptedModel([[function_call("view_image", {"path": "image.png"}, call_id="one")]]),
        capabilities=[Filesystem(configure_tools=require_approval)],
    )
    config = RunConfig(sandbox={"session": session}, tool_execution=configure().tool_execution)
    result = await run(streamed, agent, config)
    state = result.to_state()
    state.approve(result.interruptions[0])
    data = state.to_json()
    record = data["context"]["tool_invocations"]["one"]
    record["capability_source"] = None
    with pytest.raises(UserError, match="capability source"):
        await RunState.from_json(agent, data)
    del record["capability_source"]
    restored = await RunState.from_json(agent, data)
    with pytest.raises(UserError, match="capability source"):
        await run(streamed, agent, config, restored)
    assert not session.calls
