from __future__ import annotations

from collections import Counter

import graphviz  # type: ignore

from agents import Agent
from agents.handoffs import Handoff

_NodeKey = tuple[str, int]


class _GraphNodeIds:
    """Assign stable DOT identifiers without conflating nodes that share a label."""

    def __init__(
        self,
        agent: Agent,
        *,
        initially_visited_names: frozenset[str] = frozenset(),
    ) -> None:
        nodes: list[tuple[_NodeKey, str]] = []
        previsited_agents: list[tuple[_NodeKey, str]] = []
        node_keys: set[_NodeKey] = set()
        visited_agents: set[int] = set()

        def add_node(key: _NodeKey, label: str) -> None:
            if key not in node_keys:
                node_keys.add(key)
                nodes.append((key, label))

        def visit(current_agent: Agent) -> None:
            agent_key = self.agent_key(current_agent)
            if id(current_agent) in visited_agents:
                return
            visited_agents.add(id(current_agent))
            if current_agent.name in initially_visited_names:
                previsited_agents.append((agent_key, current_agent.name))
                return
            add_node(agent_key, current_agent.name)

            for tool in current_agent.tools:
                add_node(self.tool_key(tool), tool.name)
            for server in current_agent.mcp_servers:
                add_node(self.mcp_server_key(server), server.name)
            for handoff in current_agent.handoffs:
                if isinstance(handoff, Agent):
                    visit(handoff)
                    continue
                if isinstance(handoff, Handoff):
                    target = _handoff_target_agent(handoff)
                    if target is not None:
                        visit(target)
                    else:
                        add_node(self.handoff_key(handoff), handoff.agent_name)

        visit(agent)

        escaped_labels = [(key, label, _escape_label(label)) for key, label in nodes]
        escaped_previsited_agents = [
            (key, label, _escape_label(label)) for key, label in previsited_agents
        ]
        label_counts = Counter(escaped_label for _, _, escaped_label in escaped_labels)
        raw_labels = {
            escaped_label for _, _, escaped_label in [*escaped_previsited_agents, *escaped_labels]
        }
        used_ids = {"__start__", "__end__"}
        self._ids: dict[_NodeKey, str] = {}
        generated_id = 0

        def generate_id(key: _NodeKey) -> str:
            nonlocal generated_id
            while True:
                node_id = f"__agents_graph_{key[0]}_{generated_id}__"
                generated_id += 1
                if node_id not in raw_labels and node_id not in used_ids:
                    return node_id

        for key, label, escaped_label in escaped_previsited_agents:
            node_id = label if escaped_label not in used_ids else generate_id(key)
            used_ids.add(_escape_label(node_id))
            self._ids[key] = node_id

        for key, label, escaped_label in escaped_labels:
            if label_counts[escaped_label] == 1 and escaped_label not in used_ids:
                node_id = label
            else:
                node_id = generate_id(key)
            used_ids.add(_escape_label(node_id))
            self._ids[key] = node_id

    @staticmethod
    def agent_key(agent: Agent) -> _NodeKey:
        return ("agent", id(agent))

    @staticmethod
    def tool_key(tool: object) -> _NodeKey:
        return ("tool", id(tool))

    @staticmethod
    def mcp_server_key(server: object) -> _NodeKey:
        return ("mcp", id(server))

    @staticmethod
    def handoff_key(handoff: object) -> _NodeKey:
        return ("handoff", id(handoff))

    def agent(self, agent: Agent) -> str:
        return self._ids[self.agent_key(agent)]

    def tool(self, tool: object) -> str:
        return self._ids[self.tool_key(tool)]

    def mcp_server(self, server: object) -> str:
        return self._ids[self.mcp_server_key(server)]

    def handoff(self, handoff: object) -> str:
        return self._ids[self.handoff_key(handoff)]


def _escape_label(name: str) -> str:
    """Escape a name for use inside a Graphviz double-quoted ID or label.

    Backslashes are escaped first, then double quotes and line breaks, so a name
    containing any of these characters does not terminate the DOT string early
    or produce malformed output.
    """
    return (
        name.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r\n", "\\n")
        .replace("\r", "\\n")
        .replace("\n", "\\n")
    )


def _handoff_target_agent(handoff: Handoff) -> Agent | None:
    """Return the live Agent target for a ``handoff()`` object, if available."""
    agent_ref = handoff._agent_ref
    if agent_ref is None:
        return None
    target = agent_ref()
    return target if isinstance(target, Agent) else None


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
    node_ids = _GraphNodeIds(agent)
    parts.append(_get_all_nodes(agent, node_ids=node_ids))
    parts.append(_get_all_edges(agent, node_ids=node_ids))
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
    visited_names = visited if visited is not None else set()
    initially_visited_names = frozenset(visited_names)
    return _get_all_nodes(
        agent,
        parent=parent,
        visited_names=visited_names,
        initially_visited_names=initially_visited_names,
        node_ids=_GraphNodeIds(agent, initially_visited_names=initially_visited_names),
    )


def _get_all_nodes(
    agent: Agent,
    *,
    node_ids: _GraphNodeIds,
    parent: Agent | None = None,
    visited_names: set[str] | None = None,
    initially_visited_names: frozenset[str] = frozenset(),
    visited_agents: set[int] | None = None,
) -> str:
    if visited_names is None:
        visited_names = set()
    if visited_agents is None:
        visited_agents = set()
    if id(agent) in visited_agents or agent.name in initially_visited_names:
        return ""
    visited_agents.add(id(agent))
    visited_names.add(agent.name)

    parts = []

    # Start and end the graph
    if parent is None:
        parts.append(
            '"__start__" [label="__start__", shape=ellipse, style=filled, '
            "fillcolor=lightblue, width=0.5, height=0.3];"
            '"__end__" [label="__end__", shape=ellipse, style=filled, '
            "fillcolor=lightblue, width=0.5, height=0.3];"
        )
        # Ensure parent agent node is colored
        node_id = _escape_label(node_ids.agent(agent))
        name = _escape_label(agent.name)
        parts.append(
            f'"{node_id}" [label="{name}", '
            "shape=box, style=filled, "
            "fillcolor=lightyellow, width=1.5, height=0.8];"
        )

    for tool in agent.tools:
        node_id = _escape_label(node_ids.tool(tool))
        name = _escape_label(tool.name)
        parts.append(
            f'"{node_id}" [label="{name}", '
            "shape=ellipse, style=filled, "
            "fillcolor=lightgreen, width=0.5, height=0.3];"
        )

    for mcp_server in agent.mcp_servers:
        node_id = _escape_label(node_ids.mcp_server(mcp_server))
        name = _escape_label(mcp_server.name)
        parts.append(
            f'"{node_id}" [label="{name}", '
            "shape=box, style=filled, "
            "fillcolor=lightgrey, width=1, height=0.5];"
        )

    for handoff in agent.handoffs:
        if isinstance(handoff, Agent):
            if id(handoff) not in visited_agents and handoff.name not in initially_visited_names:
                node_id = _escape_label(node_ids.agent(handoff))
                name = _escape_label(handoff.name)
                parts.append(
                    f'"{node_id}" [label="{name}", '
                    f'shape=box, style="filled,rounded", '
                    f"fillcolor=lightyellow, width=1.5, height=0.8];"
                )
            parts.append(
                _get_all_nodes(
                    handoff,
                    parent=agent,
                    visited_names=visited_names,
                    initially_visited_names=initially_visited_names,
                    visited_agents=visited_agents,
                    node_ids=node_ids,
                )
            )
            continue

        if isinstance(handoff, Handoff):
            target = _handoff_target_agent(handoff)
            if target is not None:
                if id(target) not in visited_agents and target.name not in initially_visited_names:
                    node_id = _escape_label(node_ids.agent(target))
                    name = _escape_label(target.name)
                    parts.append(
                        f'"{node_id}" [label="{name}", '
                        f'shape=box, style="filled,rounded", '
                        f"fillcolor=lightyellow, width=1.5, height=0.8];"
                    )
                parts.append(
                    _get_all_nodes(
                        target,
                        parent=agent,
                        visited_names=visited_names,
                        initially_visited_names=initially_visited_names,
                        visited_agents=visited_agents,
                        node_ids=node_ids,
                    )
                )
            else:
                node_id = _escape_label(node_ids.handoff(handoff))
                name = _escape_label(handoff.agent_name)
                parts.append(
                    f'"{node_id}" [label="{name}", '
                    f'shape=box, style="filled,rounded", '
                    f"fillcolor=lightyellow, width=1.5, height=0.8];"
                )

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
    visited_names = visited if visited is not None else set()
    initially_visited_names = frozenset(visited_names)
    return _get_all_edges(
        agent,
        parent=parent,
        visited_names=visited_names,
        initially_visited_names=initially_visited_names,
        node_ids=_GraphNodeIds(agent, initially_visited_names=initially_visited_names),
    )


def _get_all_edges(
    agent: Agent,
    *,
    node_ids: _GraphNodeIds,
    parent: Agent | None = None,
    visited_names: set[str] | None = None,
    initially_visited_names: frozenset[str] = frozenset(),
    visited_agents: set[int] | None = None,
) -> str:
    if visited_names is None:
        visited_names = set()
    if visited_agents is None:
        visited_agents = set()
    if id(agent) in visited_agents or agent.name in initially_visited_names:
        return ""
    visited_agents.add(id(agent))
    visited_names.add(agent.name)

    parts = []

    agent_id = _escape_label(node_ids.agent(agent))

    if parent is None:
        parts.append(f'"__start__" -> "{agent_id}";')

    for tool in agent.tools:
        tool_id = _escape_label(node_ids.tool(tool))
        parts.append(f"""
        "{agent_id}" -> "{tool_id}" [style=dotted, penwidth=1.5];
        "{tool_id}" -> "{agent_id}" [style=dotted, penwidth=1.5];""")

    for mcp_server in agent.mcp_servers:
        server_id = _escape_label(node_ids.mcp_server(mcp_server))
        parts.append(f"""
        "{agent_id}" -> "{server_id}" [style=dashed, penwidth=1.5];
        "{server_id}" -> "{agent_id}" [style=dashed, penwidth=1.5];""")

    for handoff in agent.handoffs:
        if isinstance(handoff, Agent):
            parts.append(f"""
            "{agent_id}" -> "{_escape_label(node_ids.agent(handoff))}";""")
            parts.append(
                _get_all_edges(
                    handoff,
                    parent=agent,
                    visited_names=visited_names,
                    initially_visited_names=initially_visited_names,
                    visited_agents=visited_agents,
                    node_ids=node_ids,
                )
            )
            continue

        if isinstance(handoff, Handoff):
            target = _handoff_target_agent(handoff)
            if target is not None:
                parts.append(f"""
            "{agent_id}" -> "{_escape_label(node_ids.agent(target))}";""")
                parts.append(
                    _get_all_edges(
                        target,
                        parent=agent,
                        visited_names=visited_names,
                        initially_visited_names=initially_visited_names,
                        visited_agents=visited_agents,
                        node_ids=node_ids,
                    )
                )
            else:
                parts.append(f"""
            "{agent_id}" -> "{_escape_label(node_ids.handoff(handoff))}";""")

    if not agent.handoffs:
        parts.append(f'"{agent_id}" -> "__end__";')

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
