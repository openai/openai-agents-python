from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

import pytest

from agents import ModelSettings, RunConfig, Runner
from agents.sandbox import Manifest, MemoryGenerateConfig, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Filesystem, Memory
from agents.sandbox.capabilities.tools import SandboxApplyPatchTool
from agents.sandbox.errors import WorkspaceReadNotFoundError
from agents.testing import ScriptedModel, scripted_sandbox_session
from agents.tool import FunctionTool
from tests.sandbox._filesystem_test_session import FilesystemTestSandboxClient
from tests.test_responses import get_final_output_message, get_function_tool_call


class _FunctionSandboxModel(ScriptedModel):
    # This double preserves the provider capability boundary without starting a subprocess.
    _sandbox_model_capabilities: ClassVar[dict[str, object]] = {
        "responses_compaction": False,
        "hosted_apply_patch": False,
        "tool_namespace": "agents_sdk",
        "use_provider_defaults": True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("approve", [True, False])
async def test_function_sandbox_patch_uses_runner_approval_and_session_files(approve: bool) -> None:
    client = FilesystemTestSandboxClient()
    session = await client.create(manifest=Manifest())
    model = _FunctionSandboxModel(
        steps=[
            [
                get_function_tool_call(
                    "apply_patch",
                    json.dumps(
                        {
                            "patch": (
                                "*** Begin Patch\n*** Add File: note.txt\n+hello\n*** End Patch\n"
                            )
                        }
                    ),
                    namespace="agents_sdk",
                )
            ],
            [get_final_output_message("done")],
        ]
    )

    def configure_tools(tools) -> None:
        tools.apply_patch.needs_approval = True

    agent = SandboxAgent(
        name="sandbox",
        model=model,
        capabilities=[Filesystem(configure_tools=configure_tools)],
    )
    config = RunConfig(sandbox=SandboxRunConfig(session=session), tracing_disabled=True)
    try:
        async with session:
            first = await Runner.run(agent, "create note", run_config=config)
            assert len(first.interruptions) == 1
            with pytest.raises(WorkspaceReadNotFoundError):
                await session.read(Path("note.txt"))
            state = first.to_state()
            if approve:
                state.approve(first.interruptions[0])
            else:
                state.reject(first.interruptions[0])
            result = await Runner.run(agent, state, run_config=config)
            assert result.final_output == "done"
            if approve:
                with await session.read(Path("note.txt")) as handle:
                    assert handle.read() == b"hello"
            else:
                with pytest.raises(WorkspaceReadNotFoundError):
                    await session.read(Path("note.txt"))
            assert all(isinstance(tool, FunctionTool) for tool in model.calls[0].tools)
            assert all(getattr(tool, "_sdk_sandbox", False) for tool in model.calls[0].tools)
            assert model.calls[0].model_settings.extra_args is None
    finally:
        await client.delete(session)


@pytest.mark.asyncio
async def test_function_sandbox_disables_responses_compaction_without_mutating_agent() -> None:
    model = _FunctionSandboxModel(steps=[[get_final_output_message("done")]])
    session = scripted_sandbox_session()
    agent = SandboxAgent(name="sandbox", model=model)
    await Runner.run(
        agent,
        "hello",
        run_config=RunConfig(
            sandbox=SandboxRunConfig(session=session),
            tracing_disabled=True,
        ),
    )
    assert model.calls[0].model_settings.extra_args is None
    assert agent.model_settings.extra_args is None
    assert agent.capabilities[-1].type == "compaction"


def test_responses_sandbox_patch_keeps_custom_transport() -> None:
    capability = Filesystem()
    capability.bind(scripted_sandbox_session())
    assert isinstance(capability.tools()[-1], SandboxApplyPatchTool)


def test_memory_omitted_models_follow_provider_but_explicit_default_literals_stay_explicit() -> (
    None
):
    model = _FunctionSandboxModel()
    memory = Memory()
    memory._model_instance = model
    resolved = memory.generation_config()
    assert resolved.phase_one_model is model
    assert resolved.phase_two_model is model
    assert resolved.phase_one_model_settings is None
    assert resolved.phase_two_model_settings is None
    assert memory.generate is not None
    assert memory.generate.phase_one_model == "gpt-5.4-mini"

    settings = ModelSettings(temperature=0.5)
    memory.generate = MemoryGenerateConfig(
        phase_one_model="gpt-5.4-mini",
        phase_one_model_settings=settings,
        phase_two_model="gpt-5.5",
        phase_two_model_settings=ModelSettings(),
    )
    resolved = memory.generation_config()
    assert resolved.phase_one_model == "gpt-5.4-mini"
    assert resolved.phase_two_model == "gpt-5.5"
    assert resolved.phase_one_model_settings is settings
    assert resolved.phase_two_model_settings == ModelSettings()


def test_memory_responses_defaults_stay_unchanged() -> None:
    memory = Memory()
    memory._model_instance = ScriptedModel()
    assert memory.generation_config() is memory.generate


def test_memory_literal_defaults_remain_public_and_explicit_choices_survive_validation() -> None:
    from dataclasses import fields
    from inspect import signature

    model = _FunctionSandboxModel()
    name = "phase_one_model"
    default = next(field.default for field in fields(MemoryGenerateConfig) if field.name == name)
    assert type(default) is str
    assert type(signature(MemoryGenerateConfig).parameters[name].default) is str
    assert type(MemoryGenerateConfig().phase_one_model) is str

    for memory in (
        Memory(generate=MemoryGenerateConfig(256, default)),
        Memory.model_validate({"generate": {name: default}}),
    ):
        memory._model_instance = model
        assert memory.generation_config().phase_one_model == default
        assert memory.generation_config().phase_two_model is model

    memory = Memory.model_validate({"generate": {}})
    memory._model_instance = model
    assert memory.generation_config().phase_one_model is model
    assert memory.generate is not None
    memory.generate.phase_one_model = default
    assert memory.generation_config().phase_one_model == default


@pytest.mark.asyncio
async def test_sandbox_capabilities_follow_codex_provider_string_resolution() -> None:
    from agents.models.interface import Model, ModelProvider

    model = _FunctionSandboxModel(steps=[[get_final_output_message("done")]])

    class Provider(ModelProvider):
        def get_model(self, model_name: str | None) -> Model:
            assert model_name == "selected-model"
            return model

    await Runner.run(
        SandboxAgent(name="sandbox", model="selected-model"),
        "hello",
        run_config=RunConfig(
            model_provider=Provider(),
            tracing_disabled=True,
            sandbox=SandboxRunConfig(session=scripted_sandbox_session()),
        ),
    )
    assert all(getattr(tool, "_sdk_sandbox", False) for tool in model.calls[0].tools)
    assert model.calls[0].model_settings.extra_args is None


@pytest.mark.asyncio
async def test_sandbox_keeps_custom_provider_resolution_once_per_model_call() -> None:
    from agents.models.interface import Model, ModelProvider

    model = ScriptedModel(steps=[[get_final_output_message("done")]])

    class Provider(ModelProvider):
        calls = 0

        def get_model(self, model_name: str | None) -> Model:
            self.calls += 1
            return model

    provider = Provider()
    await Runner.run(
        SandboxAgent(name="sandbox", model=_FunctionSandboxModel()),
        "hello",
        run_config=RunConfig(
            model="selected-model",
            model_provider=provider,
            tracing_disabled=True,
            sandbox=SandboxRunConfig(session=scripted_sandbox_session()),
        ),
    )
    assert provider.calls == 1
    assert model.calls[0].model_settings.extra_args is not None
    assert "context_management" in model.calls[0].model_settings.extra_args


@pytest.mark.asyncio
async def test_memory_generation_uses_bound_provider_for_both_omitted_phases() -> None:
    client = FilesystemTestSandboxClient()
    session = await client.create(manifest=Manifest())
    model = _FunctionSandboxModel(
        steps=[
            [get_final_output_message("done")],
            [
                get_final_output_message(
                    json.dumps(
                        {
                            "rollout_slug": "task",
                            "rollout_summary": "summary",
                            "raw_memory": "memory",
                        }
                    )
                )
            ],
            [get_final_output_message("consolidated")],
        ]
    )
    memory = Memory(read=None)
    agent = SandboxAgent(name="worker", model=model, capabilities=[memory])
    try:
        await Runner.run(
            agent,
            "hello",
            run_config=RunConfig(
                sandbox=SandboxRunConfig(session=session),
                tracing_disabled=True,
            ),
        )
        assert len(model.calls) == 1
        await session.aclose()
        assert len(model.calls) == 3
        assert model.calls[1].output_schema is not None
        assert model.calls[1].model_settings.reasoning is None
        assert model.calls[2].model_settings.reasoning is None
        assert memory._model_instance is None
    finally:
        await client.delete(session)


@pytest.mark.asyncio
async def test_sandbox_preserves_custom_local_shell_execution_and_approval() -> None:
    from openai.types.responses import ResponseFunctionShellToolCall

    from agents.sandbox.capabilities import Capability
    from agents.tool import ShellTool

    executed = []

    def execute(request):
        executed.extend(request.data.action.commands)
        return "custom workspace result"

    shell = ShellTool(executor=execute, needs_approval=True)

    class WorkspaceShellCapability(Capability):
        type: str = "workspace-shell"

        def tools(self):
            return [shell]

    model = _FunctionSandboxModel(
        steps=[
            [
                ResponseFunctionShellToolCall(
                    id="item-shell",
                    call_id="call-shell",
                    type="shell_call",
                    status="in_progress",
                    action={
                        "commands": ["printf workspace"],
                        "timeout_ms": None,
                        "max_output_length": None,
                    },
                )
            ],
            [get_final_output_message("done")],
        ]
    )
    agent = SandboxAgent(
        name="custom-shell", model=model, capabilities=[WorkspaceShellCapability()]
    )
    config = RunConfig(
        tracing_disabled=True, sandbox=SandboxRunConfig(session=scripted_sandbox_session())
    )
    first = await Runner.run(agent, "inspect workspace", run_config=config)
    assert executed == []
    assert len(first.interruptions) == 1
    state = first.to_state()
    state.approve(first.interruptions[0])
    result = await Runner.run(agent, state, run_config=config)
    assert result.final_output == "done"
    assert executed == ["printf workspace"]
    assert isinstance(model.calls[0].tools[0], ShellTool)
    assert getattr(model.calls[0].tools[0], "_sdk_sandbox", False) is True
    assert not getattr(shell, "_sdk_sandbox", False)


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("run_override", [False, True])
async def test_mapped_provider_sandbox_uses_one_resolved_model_with_original_name(
    streamed: bool, run_override: bool
) -> None:
    from agents.models.interface import Model, ModelProvider
    from agents.models.multi_provider import MultiProvider, MultiProviderMap
    from agents.sandbox.capabilities import Capability

    model = _FunctionSandboxModel(steps=[[get_final_output_message("mapped sandbox")]])
    names: list[str | None] = []
    capability_names: list[str | None] = []

    class NamedCapability(Capability):
        type: str = "named"

        def sampling_params(self, sampling_params):
            capability_names.append(sampling_params.get("model"))
            return {}

    class Provider(ModelProvider):
        def get_model(self, model_name: str | None) -> Model:
            names.append(model_name)
            return model

    providers = MultiProviderMap()
    providers.add_provider("codex", Provider())
    selected = "codex/selected-model"
    agent = SandboxAgent(
        name="sandbox",
        model="ignored" if run_override else selected,
        capabilities=[Filesystem(), NamedCapability()],
    )
    config = RunConfig(
        model=selected if run_override else None,
        model_provider=MultiProvider(provider_map=providers),
        sandbox=SandboxRunConfig(session=scripted_sandbox_session()),
        tracing_disabled=True,
    )
    if streamed:
        result = Runner.run_streamed(agent, "hello", run_config=config)
        async for _event in result.stream_events():
            pass
    else:
        result = await Runner.run(agent, "hello", run_config=config)
    assert result.final_output == "mapped sandbox"
    assert names == ["selected-model"]
    assert capability_names == [selected]
    assert model.calls[0].tools
    assert model.calls[0].model_settings.extra_args is None
    assert all(getattr(tool, "_sdk_sandbox", False) for tool in model.calls[0].tools)
    assert agent.model == ("ignored" if run_override else selected)
    assert config.model == (selected if run_override else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("initial_codex", [False, True])
async def test_cached_sandbox_preparation_tracks_model_selection_changes(
    streamed: bool, initial_codex: bool
) -> None:
    from agents import function_tool
    from agents.models.interface import Model, ModelProvider

    first_type = _FunctionSandboxModel if initial_codex else ScriptedModel
    second_type = ScriptedModel if initial_codex else _FunctionSandboxModel
    first = first_type(steps=[[get_function_tool_call("switch_model", "{}")]])
    second = second_type(steps=[[get_final_output_message("switched")]])
    names: list[str | None] = []

    class Provider(ModelProvider):
        def get_model(self, model_name: str | None) -> Model:
            names.append(model_name)
            return first if model_name == "first" else second

    @function_tool
    def switch_model() -> str:
        agent.model = "second"
        return "Use the second model."

    agent = SandboxAgent(name="sandbox", model="first", tools=[switch_model])
    config = RunConfig(
        model_provider=Provider(),
        sandbox=SandboxRunConfig(session=scripted_sandbox_session()),
        tracing_disabled=True,
    )
    if streamed:
        result = Runner.run_streamed(agent, "switch models", run_config=config)
        async for _event in result.stream_events():
            pass
    else:
        result = await Runner.run(agent, "switch models", run_config=config)
    assert result.final_output == "switched"
    assert names == ["first", "second"]
    for recorded, is_codex in [
        (first.calls[0], initial_codex),
        (second.calls[0], not initial_codex),
    ]:
        sandbox_tools = [tool for tool in recorded.tools if tool.name != "switch_model"]
        assert all(getattr(tool, "_sdk_sandbox", False) == is_codex for tool in sandbox_tools)
        assert (recorded.model_settings.extra_args is None) == is_codex


@pytest.mark.asyncio
async def test_cached_memory_uses_current_resolved_model_without_rebuilding_capabilities() -> None:
    from agents.models.interface import Model, ModelProvider
    from agents.run_context import RunContextWrapper
    from agents.sandbox.runtime import SandboxRuntime

    first_model = _FunctionSandboxModel()
    second_model = _FunctionSandboxModel()
    models = iter([first_model, second_model])

    class Provider(ModelProvider):
        def get_model(self, model_name: str | None) -> Model:
            assert model_name == "same-selection"
            return next(models)

    agent = SandboxAgent(name="sandbox", model="same-selection", capabilities=[Memory(read=None)])
    runtime = SandboxRuntime(
        starting_agent=agent,
        run_config=RunConfig(
            model_provider=Provider(),
            sandbox=SandboxRunConfig(session=scripted_sandbox_session()),
            tracing_disabled=True,
        ),
        run_state=None,
    )
    context = RunContextWrapper(context=None)
    try:
        first = await runtime.prepare_agent(
            current_agent=agent,
            current_input="first",
            context_wrapper=context,
            is_resumed_state=False,
        )
        memory = next(
            cap for cap in first.bindings.execution_agent.capabilities if isinstance(cap, Memory)
        )
        assert memory.generation_config().phase_one_model is first_model
        second = await runtime.prepare_agent(
            current_agent=agent,
            current_input="second",
            context_wrapper=context,
            is_resumed_state=False,
        )
        assert second.bindings.execution_agent is first.bindings.execution_agent
        assert second.bindings.resolved_model is second_model
        assert memory.generation_config().phase_one_model is second_model
        assert memory.generation_config().phase_two_model is second_model
    finally:
        await runtime.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_sandbox_does_not_resolve_provider_when_blocking_guardrail_stops_run(
    streamed: bool,
) -> None:
    from agents import GuardrailFunctionOutput, InputGuardrailTripwireTriggered, input_guardrail
    from agents.models.interface import Model, ModelProvider

    class Provider(ModelProvider):
        def get_model(self, model_name: str | None) -> Model:
            raise AssertionError("The blocked run must not resolve a model.")

    @input_guardrail(run_in_parallel=False)
    def blocked(context, agent, input):
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    agent = SandboxAgent(name="sandbox", model="selected-model", input_guardrails=[blocked])
    config = RunConfig(
        model_provider=Provider(),
        sandbox=SandboxRunConfig(session=scripted_sandbox_session()),
        tracing_disabled=True,
    )
    with pytest.raises(InputGuardrailTripwireTriggered):
        if streamed:
            result = Runner.run_streamed(agent, "blocked", run_config=config)
            async for _event in result.stream_events():
                pass
        else:
            await Runner.run(agent, "blocked", run_config=config)
