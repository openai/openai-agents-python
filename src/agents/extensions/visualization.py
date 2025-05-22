from typing import Optional

import graphviz  # type: ignore

from agents import Agent
from agents.handoffs import Handoff
from agents.tool import Tool


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


def get_all_nodes(agent: Agent, parent: Optional[Agent] = None) -> str:
    """
    Recursively generates the nodes for the given agent and its handoffs in DOT format.

    Args:
        agent (Agent): The agent for which the nodes are to be generated.

    Returns:
        str: The DOT format string representing the nodes.
    """
    parts = []

    # Start and end the graph.
    parts.append(
        '"__start__" [label="__start__", shape=ellipse, style=filled, '
        "fillcolor=lightblue, width=0.5, height=0.3];"
        '"__end__" [label="__end__", shape=ellipse, style=filled, '
        "fillcolor=lightblue, width=0.5, height=0.3];"
    )
    # Ensure the parent agent node is colored.
    if not parent:
        parts.append(
            f'"{agent.name}" [label="{agent.name}", shape=box, style=filled, '
            "fillcolor=lightyellow, width=1.5, height=0.8];"
        )

    for tool in agent.tools:
        parts.append(
            f'"{tool.name}" [label="{tool.name}", shape=ellipse, style=filled, '
            f"fillcolor=lightgreen, width=0.5, height=0.3];"
        )

    for handoff in agent.handoffs:
        if isinstance(handoff, Handoff):
            parts.append(
                f'"{handoff.agent_name}" [label="{handoff.agent_name}", '
                f"shape=box, style=filled, style=rounded, "
                f"fillcolor=lightyellow, width=1.5, height=0.8];"
            )
        if isinstance(handoff, Agent):
            parts.append(
                f'"{handoff.name}" [label="{handoff.name}", '
                f"shape=box, style=filled, style=rounded, "
                f"fillcolor=lightyellow, width=1.5, height=0.8];"
            )
            parts.append(get_all_nodes(handoff))

    return "".join(parts)


def get_all_edges(agent: Agent, parent: Optional[Agent] = None) -> str:
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
        parts.append(f'"__start__" -> "{agent.name}";')

    for tool in agent.tools:
        parts.append(f"""
        "{agent.name}" -> "{tool.name}" [style=dotted, penwidth=1.5];
        "{tool.name}" -> "{agent.name}" [style=dotted, penwidth=1.5];""")

    for handoff in agent.handoffs:
        if isinstance(handoff, Handoff):
            parts.append(f"""
            "{agent.name}" -> "{handoff.agent_name}";""")
        if isinstance(handoff, Agent):
            parts.append(f"""
            "{agent.name}" -> "{handoff.name}";""")
            parts.append(get_all_edges(handoff, agent))

    if not agent.handoffs and not isinstance(agent, Tool):  # type: ignore
        parts.append(f'"{agent.name}" -> "__end__";')

    return "".join(parts)


def draw_graph(agent: Agent, filename: Optional[str] = None) -> graphviz.Source:
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


# --- New functions for generating graph from trace data ---

from agents.tracing.traces import Trace # Add this
from agents.tracing.span_data import AgentSpanData, HandoffSpanData, ToolSpanData # Add these

def generate_graph_from_trace(trace_data: Trace) -> str:
    dot = graphviz.Digraph(comment=f"Trace: {trace_data.name} ({trace_data.trace_id})")
    dot.attr(rankdir='TB') # Top-to-bottom graph

    # Keep track of nodes to avoid duplicates, and edges
    nodes = set()
    edges = set()

    # Add a start node
    dot.node("__start__", "__start__", shape="ellipse", style="filled", fillcolor="lightblue")
    last_agent_node_name = "__start__" # Represents the last *agent* context node
    # This is used to link sequential agent runs or tool calls from an agent.

    # Sort spans by start time to represent the flow chronologically
    sorted_spans = sorted(trace_data.spans, key=lambda s: s.start_time)

    for span in sorted_spans:
        if isinstance(span.span_data, AgentSpanData):
            agent_name = span.span_data.name
            agent_node_id = f"agent_{agent_name}_{span.span_id[:8]}" # Unique node for each agent execution instance

            if agent_node_id not in nodes:
                dot.node(agent_node_id, agent_name, shape="box", style="filled", fillcolor="lightyellow")
                nodes.add(agent_node_id)
            
            # If there was a previous agent context, and this is a different agent instance,
            # and it's not a direct handoff (handoffs draw their own edges).
            # This logic is a bit tricky: handoffs should be the primary way to link agents.
            # If an agent span follows another without a handoff span, it might be a sub-agent
            # or a complex sequence. For now, we rely on handoff spans for explicit agent-to-agent edges.
            # The `last_agent_node_name` will be updated by handoff spans too.

            # If the previous node was __start__ or a different agent (and no handoff linked them), draw an edge.
            # This simplistic view assumes a new agent span starts after the previous one logically concludes or hands off.
            if last_agent_node_name and last_agent_node_name != agent_node_id and \
               (last_agent_node_name == "__start__" or not any(e[0] == last_agent_node_name and e[1] == agent_node_id for e in edges)):
                # Avoid drawing edge if a handoff already connected the conceptual agents
                # This needs to be smarter by checking if the conceptual `last_agent_name` (not node_id)
                # was the source of a handoff to the current `agent_name`.
                # For now, if last_agent_node_name is __start__, always draw.
                # Otherwise, this edge might be redundant if a handoff span exists.
                # Let's refine: only draw from __start__ here. Handoffs will draw their specific edges.
                # If an agent follows __start__ directly, it's the first agent.
                if last_agent_node_name == "__start__":
                     dot.edge(last_agent_node_name, agent_node_id, label="starts")
                     edges.add((last_agent_node_name, agent_node_id))

            last_agent_node_name = agent_node_id # Current agent becomes the last agent context

        elif isinstance(span.span_data, HandoffSpanData):
            from_agent_name = span.span_data.from_agent # Conceptual agent name
            to_agent_name = span.span_data.to_agent     # Conceptual agent name

            # We need to find the specific *instance* (node_id) of from_agent that initiated this handoff.
            # This requires looking up the most recent AgentSpanData for from_agent_name.
            # For simplicity, we'll use `last_agent_node_name` if its name matches `from_agent_name`.
            # This is an approximation. A more robust way would be to map agent names to their latest span IDs.
            
            from_agent_node_id = last_agent_node_name # Assume handoff is from the last active agent span
            # A better way: search backwards for an agent span matching from_agent_name if last_agent_node_name isn't it.
            # For now, this simplification means handoffs should ideally occur from the immediately preceding agent span.

            # The target agent will have its own AgentSpanData later in the sorted list.
            # We create a conceptual node for the target agent if not seen,
            # but the edge should point to its actual instance node when that appears.
            # This is tricky. Let's use the names for handoff edges for now,
            # and assume AgentSpanData instances will create their concrete nodes.

            # Let's use the agent names for handoff edges, assuming their nodes will be created by AgentSpanData.
            # This makes the graph more about conceptual agent flow via handoffs.
            # The actual agent execution nodes (agent_node_id) will show instances.
            
            # Simplified: Edge between agent names for handoff.
            # More accurate: Edge from specific source agent *instance* node to target agent *instance* node.
            # For now, let's use the conceptual agent names for handoff edges for clarity.
            # The target agent's instance node will be created by its own AgentSpanData.

            # Ensure conceptual nodes for agents involved in handoff exist
            if from_agent_name not in nodes:
                dot.node(from_agent_name, from_agent_name, shape="box", style="filled", fillcolor="lightgoldenrodyellow")
                nodes.add(from_agent_name)
            if to_agent_name not in nodes:
                dot.node(to_agent_name, to_agent_name, shape="box", style="filled", fillcolor="lightyellow")
                nodes.add(to_agent_name)

            if (from_agent_name, to_agent_name) not in edges:
                 dot.edge(from_agent_name, to_agent_name, label=f"handoff\n({span.span_data.tool_name})")
                 edges.add((from_agent_name, to_agent_name))
            
            # The context for the next operation is now the agent that received the handoff.
            # We use the conceptual name here. The next AgentSpanData for to_agent_name will pick this up.
            last_agent_node_name = to_agent_name # Update conceptual last agent

        elif isinstance(span.span_data, ToolSpanData):
            tool_name = span.span_data.name
            # Tool is called by an agent. Assume it's the `last_agent_node_name` (which is an instance ID).
            agent_calling_tool_node_id = last_agent_node_name
            
            # Create a unique node for each tool call, including part of span_id for uniqueness
            tool_instance_node_id = f"tool_{tool_name}_{span.span_id[:8]}" 
            if tool_instance_node_id not in nodes:
                dot.node(tool_instance_node_id, tool_name, shape="ellipse", style="filled", fillcolor="lightgreen")
                nodes.add(tool_instance_node_id)
            
            if agent_calling_tool_node_id and agent_calling_tool_node_id != "__start__":
                if (agent_calling_tool_node_id, tool_instance_node_id) not in edges:
                    dot.edge(agent_calling_tool_node_id, tool_instance_node_id, label="calls_tool")
                    edges.add((agent_calling_tool_node_id, tool_instance_node_id))
                # Edge from tool back to agent represents the result.
                # The agent (agent_calling_tool_node_id) is expected to continue or process the result.
                if (tool_instance_node_id, agent_calling_tool_node_id) not in edges:
                    dot.edge(tool_instance_node_id, agent_calling_tool_node_id, label="result")
                    edges.add((tool_instance_node_id, agent_calling_tool_node_id))
            # The agent context doesn't change due to a tool call; it resumes.
            # So, last_agent_node_name remains the same.

    # Add an end node, connecting the very last active node (agent or tool instance) to it.
    # This needs to find the chronologically last node.
    # For simplicity, connect `last_agent_node_name` (which is the last agent instance or conceptual agent from handoff) to end.
    dot.node("__end__", "__end__", shape="ellipse", style="filled", fillcolor="lightblue")
    if last_agent_node_name and last_agent_node_name != "__start__":
         if (last_agent_node_name, "__end__") not in edges:
            dot.edge(last_agent_node_name, "__end__")
            edges.add((last_agent_node_name, "__end__"))
    elif not nodes or last_agent_node_name == "__start__": # Only start node was effectively active
        dot.edge("__start__", "__end__")

    return dot.source


def draw_trace_graph(trace_data: Trace, filename: Optional[str] = None) -> graphviz.Source:
    """
    Draws the graph for the given trace data and optionally saves it as a PNG file.

    Args:
        trace_data (Trace): The trace data object.
        filename (str): The name of the file to save the graph as a PNG.

    Returns:
        graphviz.Source: The graphviz Source object representing the graph.
    """
    dot_code = generate_graph_from_trace(trace_data)
    graph = graphviz.Source(dot_code)

    if filename:
        # Ensure the filename has no extension, graphviz adds .png
        render_filename = filename.split('.')[0]
        graph.render(render_filename, format="png", cleanup=True)
        print(f"Trace graph saved to {render_filename}.png")

    return graph
