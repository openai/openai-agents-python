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
        _is_codex_provider = True

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
