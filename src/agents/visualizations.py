import graphviz

from src.agents.agent import Agent


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
        
        "__start__" [shape=ellipse, style=filled, fillcolor=lightblue];
        "__end__" [shape=ellipse, style=filled, fillcolor=lightblue];
    """
    ]
    parts.append(get_all_nodes(agent))
    parts.append(get_all_edges(agent))
    parts.append("}")
    return "".join(parts)


def get_all_nodes(agent: Agent, parent: Agent = None) -> str:
    """
    Recursively generates the nodes for the given agent and its handoffs in DOT format.

    Args:
        agent (Agent): The agent for which the nodes are to be generated.

    Returns:
        str: The DOT format string representing the nodes.
    """
    parts = []

    # Ensure parent agent node is colored
    if not parent:
        parts.append(f"""
        "{agent.name}" [label="{agent.name}", shape=box, style=filled, fillcolor=lightyellow, width=1.5, height=0.8];""")

    # Smaller tools (ellipse, green)
    for tool in agent.tools:
        parts.append(f"""
        "{tool.name}" [label="{tool.name}", shape=ellipse, style=filled, fillcolor=lightgreen, width=0.5, height=0.3];""")

    # Bigger handoffs (rounded box, yellow)
    for handoff in agent.handoffs:
        parts.append(f"""
        "{handoff.name}" [label="{handoff.name}", shape=box, style=filled, style=rounded, fillcolor=lightyellow, width=1.5, height=0.8];""")
        parts.append(get_all_nodes(handoff))

    return "".join(parts)


def get_all_edges(agent: Agent, parent: Agent = None) -> str:
    """
    Recursively generates the edges for the given agent and its handoffs in DOT format.

    Args:
        agent (Agent): The agent for which the edges are to be generated.
        parent (Agent, optional): The parent agent. Defaults to None.

    Returns:
        str: The DOT format string representing the edges.
    """
    parts = []

    if not parent:
        parts.append(f"""
        "__start__" -> "{agent.name}";""")

    for tool in agent.tools:
        parts.append(f"""
        "{agent.name}" -> "{tool.name}" [style=dotted, penwidth=1.5];
        "{tool.name}" -> "{agent.name}" [style=dotted, penwidth=1.5];""")

    if not agent.handoffs:
        parts.append(f"""
        "{agent.name}" -> "__end__";""")

    for handoff in agent.handoffs:
        parts.append(f"""
        "{agent.name}" -> "{handoff.name}";""")
        parts.append(get_all_edges(handoff, agent))

    return "".join(parts)


def draw_graph(agent: Agent, filename: str = None) -> graphviz.Source:
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
        graph.render(filename, format="png")

    return graph
