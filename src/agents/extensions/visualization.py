from __future__ import annotations

import graphviz  # type: ignore

from agents import Agent
from agents.handoffs import Handoff


def _escape_label(name: str) -> str:
    """Escape a name for use inside a Graphviz double-quoted ID or label.

    Backslashes are escaped first, then double quotes, so a name containing
    either character does not terminate the DOT string early or produce
    malformed output.
    """
    return name.replace("\\", "\\\\").replace('"', '\\"')


def get_main_graph(agent: Agent) -> str:
    """
    Generates the main graph structure in DOT format for the given agent.

    Args:
        agent (Agent): The agent for which the graph is to be generated.

    Returns:
        str: The DOT format string representing the graph.
    """
    parts = [
        """
    digraph G {
        graph [splines=true];
        node [fontname="Arial"];
        edge [penwidth=1.5];
    """
    ]
    parts.append(get_all_nodes(agent))
    parts.append(get_all_edges(agent))
    parts.append("}")
    return "".join(parts)


def get_all_nodes(
    agent: Agent, parent: Agent | None = None, visited: set[str] | None = None
) -> str:
    """
    Recursively generates the nodes for the given agent and its handoffs in DOT format.

    Args:
        agent (Agent): The agent for which the nodes are to be generated.

    Returns:
        str: The DOT format string representing the nodes.
    """
    if visited is None:
        visited = set()
    if agent.name in visited:
        return ""
    visited.add(agent.name)

    parts = []

    # Start and end the graph
    if not parent:
        parts.append(
            '"__start__" [label="__start__", shape=ellipse, style=filled, '
            "fillcolor=lightblue, width=0.5, height=0.3];"
            '"__end__" [label="__end__", shape=ellipse, style=filled, '
            "fillcolor=lightblue, width=0.5, height=0.3];"
        )
        # Ensure parent agent node is colored
        name = _escape_label(agent.name)
        parts.append(
            f'"{name}" [label="{name}", '
            "shape=box, style=filled, "
            "fillcolor=lightyellow, width=1.5, height=0.8];"
        )

    for tool in agent.tools:
        name = _escape_label(tool.name)
        parts.append(
            f'"{name}" [label="{name}", '
            "shape=ellipse, style=filled, "
            "fillcolor=lightgreen, width=0.5, height=0.3];"
        )

    for mcp_server in agent.mcp_servers:
        name = _escape_label(mcp_server.name)
        parts.append(
            f'"{name}" [label="{name}", '
            "shape=box, style=filled, "
            "fillcolor=lightgrey, width=1, height=0.5];"
        )

    for handoff in agent.handoffs:
        if isinstance(handoff, Handoff):
            name = _escape_label(handoff.agent_name)
            parts.append(
                f'"{name}" [label="{name}", '
                f"shape=box, style=filled, style=rounded, "
                f"fillcolor=lightyellow, width=1.5, height=0.8];"
            )
        if isinstance(handoff, Agent):
            if handoff.name not in visited:
                name = _escape_label(handoff.name)
                parts.append(
                    f'"{name}" [label="{name}", '
                    f"shape=box, style=filled, style=rounded, "
                    f"fillcolor=lightyellow, width=1.5, height=0.8];"
                )
            parts.append(get_all_nodes(handoff, agent, visited))

    return "".join(parts)


def get_all_edges(
    agent: Agent, parent: Agent | None = None, visited: set[str] | None = None
) -> str:
    """
    Recursively generates the edges for the given agent and its handoffs in DOT format.

    Args:
        agent (Agent): The agent for which the edges are to be generated.
        parent (Agent, optional): The parent agent. Defaults to None.

    Returns:
        str: The DOT format string representing the edges.
    """
    if visited is None:
        visited = set()
    if agent.name in visited:
        return ""
    visited.add(agent.name)

    parts = []

    agent_name = _escape_label(agent.name)

    if not parent:
        parts.append(f'"__start__" -> "{agent_name}";')

    for tool in agent.tools:
        tool_name = _escape_label(tool.name)
        parts.append(f"""
        "{agent_name}" -> "{tool_name}" [style=dotted, penwidth=1.5];
        "{tool_name}" -> "{agent_name}" [style=dotted, penwidth=1.5];""")

    for mcp_server in agent.mcp_servers:
        server_name = _escape_label(mcp_server.name)
        parts.append(f"""
        "{agent_name}" -> "{server_name}" [style=dashed, penwidth=1.5];
        "{server_name}" -> "{agent_name}" [style=dashed, penwidth=1.5];""")

    for handoff in agent.handoffs:
        if isinstance(handoff, Handoff):
            parts.append(f"""
            "{agent_name}" -> "{_escape_label(handoff.agent_name)}";""")
        if isinstance(handoff, Agent):
            parts.append(f"""
            "{agent_name}" -> "{_escape_label(handoff.name)}";""")
            parts.append(get_all_edges(handoff, agent, visited))

    if not agent.handoffs:
        parts.append(f'"{agent_name}" -> "__end__";')

    return "".join(parts)


def draw_graph(agent: Agent, filename: str | None = None) -> graphviz.Source:
    """
    Draws the graph for the given agent and optionally saves it as a PNG file.

    Args:
        agent (Agent): The agent for which the graph is to be drawn.
        filename (str): The name of the file to save the graph as a PNG.

    Returns:
        graphviz.Source: The graphviz Source object representing the graph.
    """
    dot_code = get_main_graph(agent)
    graph = graphviz.Source(dot_code)

    if filename:
        graph.render(filename, format="png", cleanup=True)

    return graph
