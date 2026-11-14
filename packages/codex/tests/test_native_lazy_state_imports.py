"""Ordinary completion avoids checkpoints; later public state use stays canonical."""

import asyncio
import importlib
import json
import os
import sys
from hashlib import sha256
from pathlib import Path

from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses

_PROGRAM = r"""
import asyncio
import importlib
import inspect
import json
import pickle
import sys
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from threading import Barrier
from typing import Any, get_args, get_type_hints

import agents
from agents import Agent, NativeProvider, ToolContext, function_tool

url, expected = sys.argv[1], json.loads(sys.argv[2])


@function_tool
def ordinary(context: ToolContext[None]) -> str:
    assert context.context is None
    return "ordinary tool result"


async def main():
    assert "_agents_codex.state" not in sys.modules
    async with NativeProvider(base_url=url, api_key="synthetic-lazy-state") as provider:
        result = await Agent(
            instructions="Use the tool.", model="gpt-5.5", tools=[ordinary], provider=provider
        ).run("ordinary input")
        assert result.final_output == "ordinary complete"
    assert "_agents_codex.state" not in sys.modules
    for family in (
        "decision_handoff",
        "realtime_agents",
        "realtime_settings",
        "realtime_guardrails",
        "_tool_catalog",
        "manual",
        "mcp",
        "hosted_mcp",
        "native_agents",
        "_events",
        "subagents",
    ):
        assert f"_agents_codex.{family}" not in sys.modules, family
        assert f"agents.{family}" not in sys.modules, family

    tool_facade = importlib.import_module("agents.tools")
    optional_names = {"WebSearch", "FileSearch", "ToolSearch", "NativeGoals"}
    assert optional_names <= set(dir(tool_facade))
    assert optional_names <= set(tool_facade.__all__)
    assert "_agents_codex._tool_catalog" not in sys.modules
    assert "_agents_codex.manual" not in sys.modules

    union = agents.Tool
    catalog = importlib.import_module("_agents_codex._tool_catalog")
    functions = importlib.import_module("agents._function_tools")
    manual = importlib.import_module("agents.manual")
    mcp = importlib.import_module("agents.mcp")
    native_agents = importlib.import_module("agents.native_agents")
    hosted_mcp = importlib.import_module("agents.hosted_mcp")
    assert union is catalog.Tool is functions.Tool
    context_type = functions.ToolContextT
    assert get_args(union) == (
        functions._PythonTool[context_type, context_type],
        functions._PythonTool[context_type, object],
        functions._PythonTool[object, context_type],
        functions.FunctionTool[Any, Any],
        mcp.HttpMCP[object],
        mcp.StdioMCP[object],
        mcp.HttpMCP[context_type],
        mcp.StdioMCP[context_type],
        tool_facade.Checklist,
        tool_facade.NativeGoals,
        tool_facade.WebSearch,
        tool_facade.FileSearch,
        tool_facade.CodeInterpreter,
        tool_facade.HostedShell,
        tool_facade.HostedProgrammaticToolCalling,
        tool_facade.ImageGeneration,
        hosted_mcp.HostedMCP[context_type],
        tool_facade.ToolSearch,
        native_agents.NativeAgents[context_type],
    )
    for name in ("InputT", "ResultT"):
        variable = getattr(manual, name)
        assert variable is getattr(functions, name)
        assert variable.__module__ == "agents.manual"
        assert pickle.loads(pickle.dumps(variable)) is variable
    for name in optional_names:
        value = getattr(tool_facade, name)
        assert value is getattr(catalog, name)
        assert value.__module__ == "agents.tools"
        assert pickle.loads(pickle.dumps(value)) is value
    namespace = {}
    exec("from agents.tools import *", namespace)
    assert all(namespace[name] is getattr(catalog, name) for name in optional_names)
    assert native_agents.ChildInvocation in get_args(
        get_type_hints(functions.ToolContext)["submission"]
    )
    assert "is_enabled" in get_type_hints(functions.FunctionTool)

    wire = importlib.import_module("_agents_codex._tool_response_types")
    events = importlib.import_module("agents._events")
    for name in ("FunctionCallResponse", "FunctionCallStatus", "Failure", "TokenUsage"):
        value = getattr(wire, name)
        assert getattr(events, name) is value
        assert value.__module__ == "agents._events"
        assert pickle.loads(pickle.dumps(value)) is value
    observations = importlib.import_module("agents.observation")
    sessions = importlib.import_module("agents.session")
    assert "item" in get_type_hints(observations.ModelOutputItemCompleted)
    assert "item" in get_type_hints(observations.ExecutionItemCompleted)
    assert "item" in get_type_hints(sessions.ProducedItem)

    for name, digest in expected["python_modules"].items():
        module = importlib.import_module(name)
        assert sha256(Path(module.__file__).read_bytes()).hexdigest() == digest
    native = importlib.import_module("agents._native")
    assert Path(native.__file__).resolve() == Path(expected["native"]).resolve()

    names = (
        "SessionState",
        "LocalStorage",
        "ApplicationToolOutput",
        "ChecklistState",
        "CheckpointStatus",
        "NativeGoal",
    )
    barrier = Barrier(8)

    def first_access(index):
        barrier.wait(timeout=10)
        offset = index % len(names)
        classes = {name: getattr(agents, name) for name in names[offset:] + names[:offset]}
        record = classes["ApplicationToolOutput"](
            submission_id="submission",
            turn_id="turn",
            call_id="call",
            name="tool",
            application_data={"receipt": index},
        )
        assert (
            classes["ApplicationToolOutput"].model_validate_json(record.model_dump_json()) == record
        )
        return classes

    with ThreadPoolExecutor(max_workers=8) as workers:
        futures = [workers.submit(first_access, index) for index in range(8)]
        groups = [future.result(timeout=20) for future in futures]
    canonical = importlib.import_module("_agents_codex.state")
    facade = importlib.import_module("agents.session")
    for group in groups:
        for name in names:
            assert group[name] is getattr(canonical, name) is getattr(agents, name)
    for name in (
        "SessionState",
        "LocalStorage",
        "ChecklistState",
        "CheckpointStatus",
        "NativeGoal",
    ):
        assert getattr(facade, name) is getattr(canonical, name)

    def contains(annotation, wanted):
        return annotation is wanted or any(contains(arg, wanted) for arg in get_args(annotation))

    session = importlib.import_module("_agents_codex.session")
    surfaces = [(session.Completed.application_tool_outputs.fget, canonical.ApplicationToolOutput)]
    for name, wanted in (
        ("checklist", canonical.ChecklistState),
        ("goal", canonical.NativeGoal),
        ("set_paused_goal", canonical.NativeGoal),
        ("save_state", canonical.SessionState),
        ("fork", canonical.SessionState),
        ("checkpoint_status", canonical.CheckpointStatus),
        ("suspend", canonical.SessionState),
    ):
        surfaces.append((getattr(session.Session, name), wanted))
    for operation, wanted in surfaces:
        assert contains(get_type_hints(operation)["return"], wanted)
        assert contains(inspect.get_annotations(operation, eval_str=True)["return"], wanted)

    for public_class in (session.InteractionRequired,):
        hints = get_type_hints(public_class)
        assert any(contains(annotation, canonical.SessionState) for annotation in hints.values())
        annotations = inspect.get_annotations(public_class, eval_str=True)
        assert any(
            contains(annotation, canonical.SessionState) for annotation in annotations.values()
        )

    completed_namespace = vars(importlib.import_module(session.Completed.__module__))
    state_annotation = inspect.get_annotations(session.Completed)["state"]
    assert eval(state_annotation, completed_namespace) == canonical.SessionState | None

    from agents import SessionState, ToolOutput

    @function_tool
    def with_data() -> ToolOutput:
        output = ToolOutput.text("public tool value")
        output.application_data = {"receipt": "retained application data"}
        return output

    async with NativeProvider(base_url=url, api_key="synthetic-lazy-state") as provider:
        agent = Agent(
            instructions="Use the tool.", model="gpt-5.5", tools=[with_data], provider=provider
        )
        async with agent.session() as opened:
            completed = await (await opened.submit("capture input")).get_result()
            assert completed.final_output == "data complete"
            (record,) = completed.application_tool_outputs
            assert isinstance(record, canonical.ApplicationToolOutput)
            assert record.application_data == {"receipt": "retained application data"}
            saved = SessionState.from_json((await opened.save_state()).to_json())
            assert type(saved) is canonical.SessionState
            assert saved.application_tool_outputs[0] == record
        async with agent.session(state=saved) as restored:
            assert restored.id == saved.source_session_id
            assert (
                await (await restored.submit("continue")).get_result()
            ).final_output == "restored complete"
    print(
        json.dumps(
            {
                "ordinary_state_absent": True,
                "ordinary_catalog_absent": True,
                "canonical_tool_union": True,
                "canonical_type_accessors": len(groups),
                "public_hint_surfaces": len(surfaces),
                "public_class_hints": 2,
                "checkpoint_restored": True,
            }
        )
    )


asyncio.run(main())
"""


async def test_first_run_defers_state_until_public_checkpoint_use(responses) -> None:
    responses.respond(function_call("ordinary", {}, "ordinary-call"))
    responses.respond(assistant_output("ordinary complete"))
    responses.respond(function_call("with_data", {}, "application-call"))
    responses.respond(assistant_output("data complete"))
    responses.respond(assistant_output("restored complete"))
    expected = {
        "python_modules": {
            name: sha256(Path(importlib.import_module(name).__file__).read_bytes()).hexdigest()
            for name in (
                "_agents_codex.session",
                "_agents_codex._opening_compatibility",
                "_agents_codex.tools",
                "_agents_codex._function_tools",
                "_agents_codex._tool_catalog",
            )
        },
        "native": importlib.import_module("agents._native").__file__,
    }
    environment = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT")
        if key in os.environ
    }
    environment.update(
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONNOUSERSITE="1",
        OPENAI_AGENTS_TEST_IN_CODEX_SANDBOX="1",
        OPENAI_AGENTS_DISABLE_TRACING="1",
        OTEL_SDK_DISABLED="true",
        NO_PROXY="localhost,127.0.0.1,::1",
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-B",
        "-c",
        _PROGRAM,
        responses.url,
        json.dumps(expected),
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with timeout(60):
            stdout, stderr = await process.communicate()
        assert process.returncode == 0, (stdout.decode(), stderr.decode())
        assert json.loads(stdout) == {
            "ordinary_state_absent": True,
            "ordinary_catalog_absent": True,
            "canonical_tool_union": True,
            "canonical_type_accessors": 8,
            "public_hint_surfaces": 8,
            "public_class_hints": 2,
            "checkpoint_restored": True,
        }
        assert len(responses.requests) == 5
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
