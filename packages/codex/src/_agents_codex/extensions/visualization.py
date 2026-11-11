"""Graphviz drawings of native Agent declarations.

Install the optional graphviz Python package before importing this module.
Rendering also requires Graphviz's dot executable; inspecting source does not.
"""

from __future__ import annotations

from typing import Any

import graphviz  # type: ignore

from .._agent_profiles import _capture_agent_graph
from .._function_tools import FunctionTool
from .._public import preserve_module_identity as _preserve_module_identity
from ..agent import Agent
from ..mcp import _MCP


def draw_graph(
    agent: Agent[Any, Any],
    filename: str | None = None,
    format: str = "png",
    view: bool = False,
) -> graphviz.Digraph:
    """Draw declared handoffs and tools without opening native resources.

    Synchronous handoff factories resolve once through the Session's declaration
    capture rules. Tool callbacks, models and MCP roster discovery do not run.
    Labels contain names only, not instructions, credentials or tool arguments.
    filename renders a file; view opens the rendered graph. Otherwise the return
    value provides DOT source without invoking an external program.
    """
    if not isinstance(agent, Agent):
        raise TypeError("draw_graph requires a native Agent")
    profiles = _capture_agent_graph(agent)
    graph = graphviz.Digraph("Agent workflow", format=format, graph_attr={"rankdir": "LR"})
    graph.node("__start__", label="Start", shape="ellipse")
    graph.node("__end__", label="End", shape="ellipse")
    graph.edge("__start__", profiles[0].id)
    tool_ids: dict[int, str] = {}
    for profile in profiles:
        graph.node(profile.id, label=graphviz.escape(profile.agent.name or "Agent"), shape="box")
        graph.edge(profile.id, "__end__")
        for tool in profile.declaration.tools:
            key = id(tool)
            node = tool_ids.get(key)
            if node is None:
                node = f"tool_{len(tool_ids)}"
                tool_ids[key] = node
                name = (
                    tool.ref.name
                    if isinstance(tool, FunctionTool)
                    else tool.name
                    if isinstance(tool, _MCP)
                    else type(tool).__name__
                )
                graph.node(node, label=graphviz.escape(name), shape="ellipse")
            graph.edge(profile.id, node, style="dashed")
        for target in profile.handoffs.values():
            graph.edge(profile.id, target)
    if filename is not None:
        graph.render(filename=filename, view=view)
    elif view:
        graph.view()
    return graph


_preserve_module_identity(globals(), "agents.extensions.visualization")
