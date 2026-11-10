"""Agent local declaration checks use the native opening admission rules."""

import pytest
from pydantic import BaseModel

from agents import Agent, FunctionTool, HttpMCP, NativeProvider, ToolContext, function_tool
from tests.native_fixtures import responses as responses


class Input(BaseModel):
    text: str


class Output(BaseModel):
    text: str


def manual(name: str) -> FunctionTool:
    return FunctionTool(
        name=name,
        description="Synthetic declaration.",
        input_type=Input,
        output_type=Output,
    )


@pytest.mark.parametrize(
    "names, message",
    [
        (("lookup", "lookup"), "Duplicate Python tool: lookup"),
        (("exec_command",), "Python tool uses a reserved name: exec_command"),
        (("exec",), "Python tool uses a reserved name: exec"),
        (("exec_command_status",), None),
        (("lookup", "select"), None),
        (("run",), None),
    ],
)
async def test_validate_matches_actual_opening_admission(responses, names, message):
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-validation")
    agent = Agent(instructions="Local admission.", tools=[manual(name) for name in names])
    if message is None:
        assert agent.validate() is None
        async with agent.session(provider=provider):
            pass
    else:
        with pytest.raises(ValueError, match=message):
            agent.validate()
        with pytest.raises(ValueError, match=message):
            async with agent.session(provider=provider):
                pytest.fail("Native opening admitted the invalid tool declaration.")
    # Failed opening must release a borrowed lease before returning the error.
    await provider.aclose()
    assert not responses.requests


def test_validate_admits_context_tools_and_keeps_mcp_namespace_separate_without_io(monkeypatch):
    from agents import _native

    @function_tool
    def run(context: ToolContext[str], text: str) -> str:
        return context.context + text

    # MCP tool names are checked by the MCP declaration path, not Python tools.
    mcp = HttpMCP(name="catalog", url="https://example.invalid/mcp", tools=["exec_command"])
    agent = Agent(instructions="Only a declaration.", tools=[run, mcp])

    def forbidden(*args, **kwargs):
        pytest.fail("validate performed opening I/O or bound application context.")

    monkeypatch.setattr(NativeProvider, "__init__", forbidden)
    monkeypatch.setattr(NativeProvider, "_acquire_lease", forbidden)
    monkeypatch.setattr(_native, "_read_openai_api_key_from_env", forbidden)
    monkeypatch.setattr(_native, "create_engine", forbidden)
    monkeypatch.setattr(_native, "create_checkpoint_engine", forbidden)
    monkeypatch.setattr(HttpMCP, "_config", forbidden)
    monkeypatch.setattr(FunctionTool, "_capture", forbidden)
    assert agent.validate() is None


def test_validate_raises_native_name_error_without_opening_or_credentials(monkeypatch):
    from agents import _native

    declaration = Agent(instructions="Validate offline.", tools=[manual("exec_command")])

    def forbidden(*args, **kwargs):
        pytest.fail("Rejected declaration accessed a provider or native engine.")

    monkeypatch.setattr(NativeProvider, "__init__", forbidden)
    monkeypatch.setattr(NativeProvider, "_acquire_lease", forbidden)
    monkeypatch.setattr(_native, "_read_openai_api_key_from_env", forbidden)
    monkeypatch.setattr(_native, "create_engine", forbidden)
    with pytest.raises(ValueError, match="Python tool uses a reserved name: exec_command"):
        declaration.validate()


def test_validate_reports_first_failure_before_invalid_output_schema():
    # Native tool-name admission precedes output schema preparation.
    agent = Agent(
        instructions="Check both.", tools=[manual("exec_command")], output_type=dict[str, int]
    )
    with pytest.raises(ValueError, match="Python tool uses a reserved name: exec_command"):
        agent.validate()
    with pytest.raises((TypeError, ValueError)):
        Agent(instructions="Check output.", output_type=dict[str, int]).validate()


@pytest.mark.parametrize("target_problem", ["name", "tools", "output"])
def test_validate_rejects_invalid_handoff_declarations(target_problem):
    target = Agent(
        name=None if target_problem == "name" else "B",
        instructions="Target.",
        tools=[manual("exec_command")] if target_problem == "tools" else [],
        output_type=Output if target_problem == "output" else None,
    )
    with pytest.raises((TypeError, ValueError)):
        Agent(instructions="Root.", handoffs=[target]).validate()


@pytest.mark.parametrize("target_collision", [False, True])
def test_validate_rejects_function_handoff_name_collision(target_collision):
    leaf = Agent(name="C", instructions="Leaf.")
    target = Agent(
        name="B",
        instructions="Target.",
        tools=[manual("transfer_to_c")] if target_collision else [],
        handoffs=[leaf],
    )
    source = Agent(
        name="A",
        instructions="Root.",
        handoffs=[target],
        tools=[] if target_collision else [manual("transfer_to_b")],
    )
    with pytest.raises(ValueError, match="handoff name collides"):
        source.validate()


def test_validate_resolves_cyclic_graph_once_without_binding_context_or_resources(monkeypatch):
    from agents import _native

    resolutions = []

    @function_tool
    def contextual(context: ToolContext[str], text: str) -> str:
        return context.context + text

    def resolve_target():
        resolutions.append("target")
        return target

    source = Agent(name="A", instructions="Root.", handoffs=[resolve_target, resolve_target])
    target = Agent(name="B", instructions="Target.", tools=[contextual], handoffs=[source])

    def forbidden(*args, **kwargs):
        pytest.fail("Graph validation bound context or acquired runtime resources.")

    monkeypatch.setattr(NativeProvider, "__init__", forbidden)
    monkeypatch.setattr(NativeProvider, "_acquire_lease", forbidden)
    monkeypatch.setattr(_native, "_read_openai_api_key_from_env", forbidden)
    monkeypatch.setattr(_native, "create_engine", forbidden)
    monkeypatch.setattr(HttpMCP, "_config", forbidden)
    monkeypatch.setattr(FunctionTool, "_capture", forbidden)
    assert source.validate() is None
    assert resolutions == ["target"]
