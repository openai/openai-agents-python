import abc
import base64
import warnings
from dataclasses import dataclass
from enum import Enum
from typing import Generic, Literal, Optional, TypeVar

import graphviz
import requests

from agents import Agent
from agents.handoffs import Handoff
from agents.tool import Tool


class NodeType(Enum):
    START = "start"
    END = "end"
    AGENT = "agent"
    TOOL = "tool"
    HANDOFF = "handoff"


class EdgeType(Enum):
    HANDOFF = "handoff"
    TOOL = "tool"


@dataclass(frozen=True)
class Node:
    id: str
    label: str
    type: NodeType


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    type: EdgeType


class Graph:
    def __init__(self):
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []

    def add_node(self, node: Node) -> None:
        self.nodes[node.id] = node

    def add_edge(self, edge: Edge) -> None:
        """Add an edge to the graph.

        Args:
            edge (Edge): The edge to add.

        Raises:
            ValueError: If the source or target node does not exist in the graph.
        """
        if edge.source not in self.nodes:
            raise ValueError(f"Source node '{edge.source}' does not exist in the graph")
        if edge.target not in self.nodes:
            raise ValueError(f"Target node '{edge.target}' does not exist in the graph")
        self.edges.append(edge)

    def has_node(self, node_id: str) -> bool:
        """Check if a node exists in the graph.

        Args:
            node_id (str): The ID of the node to check.

        Returns:
            bool: True if the node exists, False otherwise.
        """
        return node_id in self.nodes

    def get_node(self, node_id: str) -> Optional[Node]:
        """Get a node from the graph.

        Args:
            node_id (str): The ID of the node to get.

        Returns:
            Optional[Node]: The node if it exists, None otherwise.
        """
        return self.nodes.get(node_id)


class GraphBuilder:
    def __init__(self):
        self._visited: set[int] = set()

    def build_from_agent(self, agent: Agent) -> Graph:
        """Build a graph from an agent.

        Args:
            agent (Agent): The agent to build the graph from.

        Returns:
            Graph: The built graph.
        """
        self._visited.clear()
        graph = Graph()

        # Add start and end nodes
        graph.add_node(Node("__start__", "__start__", NodeType.START))
        graph.add_node(Node("__end__", "__end__", NodeType.END))

        self._add_agent_nodes_and_edges(agent, None, graph)
        return graph

    def _add_agent_nodes_and_edges(
        self,
        agent: Agent | None,
        parent: Optional[Agent],
        graph: Graph,
    ) -> None:
        if agent is None:
            return

        # Add agent node
        graph.add_node(Node(agent.name, agent.name, NodeType.AGENT))

        # Connect start node if root agent
        if not parent:
            graph.add_edge(Edge("__start__", agent.name, EdgeType.HANDOFF))

        # Add tool nodes and edges
        for tool in agent.tools:
            graph.add_node(Node(tool.name, tool.name, NodeType.TOOL))
            graph.add_edge(Edge(agent.name, tool.name, EdgeType.TOOL))
            graph.add_edge(Edge(tool.name, agent.name, EdgeType.TOOL))

        # Add current agent's ID to visited set
        self._visited.add(id(agent))

        # Process handoffs
        has_handoffs = False
        for handoff in agent.handoffs:
            has_handoffs = True
            if isinstance(handoff, Handoff):
                graph.add_node(Node(handoff.agent_name, handoff.agent_name, NodeType.HANDOFF))
                graph.add_edge(Edge(agent.name, handoff.agent_name, EdgeType.HANDOFF))
            elif isinstance(handoff, Agent):
                graph.add_node(Node(handoff.name, handoff.name, NodeType.AGENT))
                graph.add_edge(Edge(agent.name, handoff.name, EdgeType.HANDOFF))
                if id(handoff) not in self._visited:
                    self._add_agent_nodes_and_edges(handoff, agent, graph)

        # Connect to end node if no handoffs
        if not has_handoffs and not isinstance(agent, Tool):
            graph.add_edge(Edge(agent.name, "__end__", EdgeType.HANDOFF))


T = TypeVar("T")


class GraphRenderer(Generic[T], abc.ABC):
    """Abstract base class for graph renderers."""

    @abc.abstractmethod
    def render(self, graph: Graph) -> T:
        """Render the graph in the specific format.

        Args:
            graph (Graph): The graph to render.

        Returns:
            T: The rendered graph in the format specific to the renderer.
        """
        pass

    @abc.abstractmethod
    def save(self, rendered: T, filename: str) -> None:
        """Save the rendered graph to a file.

        Args:
            rendered (T): The rendered graph returned by render().
            filename (str): The name of the file to save the graph as.
        """
        pass


class GraphvizRenderer(GraphRenderer[str]):
    """Renderer that outputs graphs in Graphviz DOT format."""

    def render(self, graph: Graph) -> str:
        parts = [
            """
    digraph G {
        graph [splines=true];
        node [fontname="Arial"];
        edge [penwidth=1.5];
    """
        ]

        # Add nodes
        for node in graph.nodes.values():
            parts.append(self._render_node(node))

        # Add edges
        for edge in graph.edges:
            parts.append(self._render_edge(edge))

        parts.append("}")
        return "".join(parts)

    def save(self, rendered: str, filename: str) -> None:
        """Save the rendered graph as a PNG file using graphviz.

        Args:
            rendered (str): The DOT format string.
            filename (str): The name of the file to save the graph as.
        """
        graphviz.Source(rendered).render(filename, format="png")

    def _render_node(self, node: Node) -> str:
        style_map = {
            NodeType.START: (
                "shape=ellipse, style=filled, fillcolor=lightblue, width=0.5, height=0.3"
            ),
            NodeType.END: (
                "shape=ellipse, style=filled, fillcolor=lightblue, width=0.5, height=0.3"
            ),
            NodeType.AGENT: (
                "shape=box, style=filled, fillcolor=lightyellow, width=1.5, height=0.8"
            ),
            NodeType.TOOL: (
                "shape=ellipse, style=filled, fillcolor=lightgreen, width=0.5, height=0.3"
            ),
            NodeType.HANDOFF: (
                "shape=box, style=filled, fillcolor=lightyellow, width=1.5, height=0.8"
            ),
        }
        return f'"{node.id}" [label="{node.label}", {style_map[node.type]}];'

    def _render_edge(self, edge: Edge) -> str:
        if edge.type == EdgeType.TOOL:
            return f'"{edge.source}" -> "{edge.target}" [style=dotted, penwidth=1.5];'
        return f'"{edge.source}" -> "{edge.target}";'


class MermaidRenderer(GraphRenderer[str]):
    """Renderer that outputs graphs in Mermaid flowchart syntax."""

    def render(self, graph: Graph) -> str:
        parts = ["graph TD\n"]

        # Add nodes with styles
        for node in graph.nodes.values():
            parts.append(self._render_node(node))

        # Add edges
        for edge in graph.edges:
            parts.append(self._render_edge(edge))

        return "".join(parts)

    def save(self, rendered: str, filename: str) -> None:
        """Save the rendered graph as a PNG file using mermaid.ink API.

        Args:
            rendered (str): The Mermaid syntax string.
            filename (str): The name of the file to save the graph as.
        """
        # Encode the graph to base64
        graphbytes = rendered.encode("utf8")
        base64_bytes = base64.urlsafe_b64encode(graphbytes)
        base64_string = base64_bytes.decode("ascii")

        # Get the image from mermaid.ink
        response = requests.get(f"https://mermaid.ink/img/{base64_string}")
        response.raise_for_status()

        # Save the image directly from response content
        with open(f"{filename}.png", "wb") as f:
            f.write(response.content)

    def _render_node(self, node: Node) -> str:
        # Map node types to Mermaid shapes
        style_map = {
            NodeType.START: ["(", ")", "lightblue"],
            NodeType.END: ["(", ")", "lightblue"],
            NodeType.AGENT: ["[", "]", "lightyellow"],
            NodeType.TOOL: ["((", "))", "lightgreen"],
            NodeType.HANDOFF: ["[", "]", "lightyellow"],
        }

        start, end, color = style_map[node.type]
        node_id = self._sanitize_id(node.id)
        # Use sanitized ID and original label
        return f"{node_id}{start}{node.label}{end}\nstyle {node_id} fill:{color}\n"

    def _render_edge(self, edge: Edge) -> str:
        source = self._sanitize_id(edge.source)
        target = self._sanitize_id(edge.target)
        if edge.type == EdgeType.TOOL:
            return f"{source} -.-> {target}\n"
        return f"{source} --> {target}\n"

    def _sanitize_id(self, id: str) -> str:
        """Sanitize node IDs to work with Mermaid's stricter ID requirements."""
        return id.replace(" ", "_").replace("-", "_")


class GraphView:
    def __init__(
        self, rendered_graph: str, renderer: GraphRenderer, filename: Optional[str] = None
    ):
        self.rendered_graph = rendered_graph
        self.renderer = renderer
        self.filename = filename

    def view(self) -> None:
        """Opens the rendered graph in a separate window."""
        import os
        import tempfile
        import webbrowser

        if self.filename:
            webbrowser.open(f"file://{os.path.abspath(self.filename)}.png")
        else:
            temp_dir = tempfile.gettempdir()
            temp_path = os.path.join(temp_dir, next(tempfile._get_candidate_names()))
            self.renderer.save(self.rendered_graph, temp_path)
            webbrowser.open(f"file://{os.path.abspath(temp_path)}.png")


def draw_graph(
    agent: Agent,
    filename: str | None = None,
    renderer: Literal["graphviz", "mermaid"] = "graphviz",
) -> GraphView:
    """
    Draws the graph for the given agent using the specified renderer.

    Args:
        agent (Agent): The agent for which the graph is to be drawn.
        filename (str | None): The name of the file to save the graph as PNG. Defaults to None.
        renderer (Literal["graphviz", "mermaid"]): The renderer to use. Defaults to "graphviz".

    Returns:
        GraphView: A view object that can be used to display the graph.

    Raises:
        ValueError: If the specified renderer is not supported.
    """
    builder = GraphBuilder()
    graph = builder.build_from_agent(agent)

    if renderer == "graphviz":
        renderer_instance = GraphvizRenderer()
    elif renderer == "mermaid":
        renderer_instance = MermaidRenderer()
    else:
        raise ValueError(f"Unsupported renderer: {renderer}")

    rendered = renderer_instance.render(graph)

    if filename:
        filename = filename.rsplit(".", 1)[0]
        renderer_instance.save(rendered, filename)

    return GraphView(rendered, renderer_instance, filename)


def get_main_graph(agent: Agent) -> str:
    """
    Generates the main graph structure in DOT format for the given agent.

    Args:
        agent (Agent): The agent for which the graph is to be generated.

    Returns:
        str: The DOT format string representing the graph.

    Deprecated:
        This function is deprecated. Use GraphBuilder and GraphvizRenderer instead.
    """
    warnings.warn(
        "get_main_graph is deprecated. Use GraphBuilder and GraphvizRenderer instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    builder = GraphBuilder()
    renderer = GraphvizRenderer()
    graph = builder.build_from_agent(agent)
    return renderer.render(graph)


def get_all_nodes(
    agent: Agent, parent: Optional[Agent] = None, visited: Optional[set[int]] = None
) -> str:
    """
    Recursively generates the nodes for the given agent and its handoffs in DOT format.

    Deprecated:
        This function is deprecated. Use GraphBuilder and GraphvizRenderer instead.
    """
    warnings.warn(
        "get_all_nodes is deprecated. Use GraphBuilder and GraphvizRenderer instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    builder = GraphBuilder()
    renderer = GraphvizRenderer()
    graph = builder.build_from_agent(agent)
    return "\n".join(renderer._render_node(node) for node in graph.nodes.values())


def get_all_edges(
    agent: Agent, parent: Optional[Agent] = None, visited: Optional[set[int]] = None
) -> str:
    """
    Recursively generates the edges for the given agent and its handoffs in DOT format.

    Deprecated:
        This function is deprecated. Use GraphBuilder and GraphvizRenderer instead.
    """
    warnings.warn(
        "get_all_edges is deprecated. Use GraphBuilder and GraphvizRenderer instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    builder = GraphBuilder()
    renderer = GraphvizRenderer()
    graph = builder.build_from_agent(agent)
    return "\n".join(renderer._render_edge(edge) for edge in graph.edges)
