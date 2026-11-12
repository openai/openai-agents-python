"""Custom-storage admission through the public Session opening boundary."""

import pytest
from pydantic import BaseModel

from agents import (
    Agent,
    CapabilityError,
    Checklist,
    CustomStorage,
    FunctionTool,
    HttpMCP,
    NativeAgents,
    NativeProvider,
    WebSearch,
    function_tool,
)
from tests.native_fixtures import responses as responses
from tests.native_storage_fixtures import MemorySessionStore


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("checklist", "CustomStorage preview does not support"),
        ("code", "CustomStorage preview does not support"),
        ("mcp", "CustomStorage preview does not support"),
        ("manual", "CustomStorage preview does not support"),
        ("standalone-search", "CustomStorage preview does not support"),
        ("split-first", "exactly one NativeAgents"),
        ("provider-first", "Buffered Responses do not support native child agents"),
        ("instructions-first", "missing.md"),
    ],
)
async def test_custom_storage_opening_rejection_order(responses, tmp_path, kind, expected):
    acquisitions = []
    effects = []

    class UnopenedStore(MemorySessionStore):
        def acquire(self, thread_id):
            acquisitions.append(thread_id)
            return super().acquire(thread_id)

    @function_tool
    def record() -> str:
        """Record an application effect."""
        effects.append("called")
        return "called"

    declarations = {
        "checklist": [Checklist()],
        "code": [],
        "mcp": [HttpMCP(name="remote", url=responses.url, tools=["lookup"])],
        "manual": [
            FunctionTool(
                name="manual", description="Manual result", input_type=BaseModel, output_type=str
            )
        ],
        "standalone-search": [WebSearch()],
        "split-first": [NativeAgents(), NativeAgents()],
        "provider-first": [NativeAgents()],
        "instructions-first": [Checklist()],
    }[kind]
    provider = NativeProvider(
        base_url=responses.url,
        api_key="synthetic-opening-order",
        response_mode="buffered" if kind == "provider-first" else "streaming",
    )
    agent = Agent(
        instructions="Answer.",
        model="gpt-5.5",
        provider=provider,
        tools=[*declarations, record],
        programmatic_tool_calling=kind == "code",
    )
    options = (
        {"instruction_files": [tmp_path / "missing.md"]} if kind == "instructions-first" else {}
    )
    error = (
        CapabilityError
        if kind == "provider-first"
        else FileNotFoundError
        if kind == "instructions-first"
        else ValueError
    )
    async with provider:
        with pytest.raises(error, match=expected):
            async with agent.session(storage=CustomStorage(UnopenedStore()), **options):
                pytest.fail("Unsupported custom storage opened")
    assert acquisitions == []
    assert effects == []
    assert responses.requests == []
