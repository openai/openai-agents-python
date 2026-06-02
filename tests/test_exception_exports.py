"""Verify that all public exception classes are re-exported from the top-level agents package."""

import agents
from agents import exceptions as exceptions_module


def test_mcp_tool_cancellation_error_is_exported_at_top_level() -> None:
    # MCPToolCancellationError is a public exception users may need to catch from MCP tool
    # invocations. It must be importable from `agents` like its sibling exception types.
    from agents import MCPToolCancellationError

    assert MCPToolCancellationError is exceptions_module.MCPToolCancellationError
    assert "MCPToolCancellationError" in agents.__all__


def test_all_public_exception_classes_are_reexported() -> None:
    # Every concrete exception class subclassing AgentsException in agents.exceptions
    # should be re-exported from the top-level `agents` package, so users have a
    # single import path for catching SDK errors.
    public_exception_names = [
        name
        for name, obj in vars(exceptions_module).items()
        if isinstance(obj, type)
        and issubclass(obj, exceptions_module.AgentsException)
        and not name.startswith("_")
    ]

    for name in public_exception_names:
        assert hasattr(agents, name), f"agents.{name} is not re-exported from agents package"
        assert name in agents.__all__, f"{name} is missing from agents.__all__"
