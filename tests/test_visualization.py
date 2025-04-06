import dataclasses
from unittest.mock import Mock, patch

import pytest

from agents import Agent
from agents.extensions.visualization import (
    Edge,
    EdgeType,
    Graph,
    GraphBuilder,
    GraphView,
    GraphvizRenderer,
    MermaidRenderer,
    Node,
    NodeType,
    draw_graph,
    get_all_edges,
    get_all_nodes,
    get_main_graph,
)
from agents.handoffs import Handoff

# Common test graph elements
START_NODE = (
    '"__start__" [label="__start__", shape=ellipse, style=filled, '
    "fillcolor=lightblue, width=0.5, height=0.3];"
)
END_NODE = (
    '"__end__" [label="__end__", shape=ellipse, style=filled, '
    "fillcolor=lightblue, width=0.5, height=0.3];"
)
AGENT_NODE = (
    '"Agent1" [label="Agent1", shape=box, style=filled, '
    "fillcolor=lightyellow, width=1.5, height=0.8];"
)
TOOL1_NODE = (
    '"Tool1" [label="Tool1", shape=ellipse, style=filled, '
    "fillcolor=lightgreen, width=0.5, height=0.3];"
)
TOOL2_NODE = (
    '"Tool2" [label="Tool2", shape=ellipse, style=filled, '
    "fillcolor=lightgreen, width=0.5, height=0.3];"
)
HANDOFF_NODE = (
    '"Handoff1" [label="Handoff1", shape=box, style=filled, '
    "fillcolor=lightyellow, width=1.5, height=0.8];"
)


@pytest.fixture
def mock_agent():
    tool1 = Mock()
    tool1.name = "Tool1"
    tool2 = Mock()
    tool2.name = "Tool2"

    handoff1 = Mock(spec=Handoff)
    handoff1.agent_name = "Handoff1"

    agent = Mock(spec=Agent)
    agent.name = "Agent1"
    agent.tools = [tool1, tool2]
    agent.handoffs = [handoff1]

    return agent


@pytest.fixture
def mock_recursive_agents():
    agent1 = Mock(spec=Agent)
    agent1.name = "Agent1"
    agent1.tools = []
    agent2 = Mock(spec=Agent)
    agent2.name = "Agent2"
    agent2.tools = []
    agent1.handoffs = [agent2]
    agent2.handoffs = [agent1]
    return agent1


# Tests for the new graph abstraction
def test_graph_builder(mock_agent):
    builder = GraphBuilder()
    graph = builder.build_from_agent(mock_agent)

    # Check nodes
    assert "__start__" in graph.nodes
    assert "__end__" in graph.nodes
    assert "Agent1" in graph.nodes
    assert "Tool1" in graph.nodes
    assert "Tool2" in graph.nodes
    assert "Handoff1" in graph.nodes

    # Check node types
    assert graph.nodes["__start__"].type == NodeType.START
    assert graph.nodes["__end__"].type == NodeType.END
    assert graph.nodes["Agent1"].type == NodeType.AGENT
    assert graph.nodes["Tool1"].type == NodeType.TOOL
    assert graph.nodes["Tool2"].type == NodeType.TOOL
    assert graph.nodes["Handoff1"].type == NodeType.HANDOFF

    # Check edges
    start_to_agent = Edge("__start__", "Agent1", EdgeType.HANDOFF)
    agent_to_tool1 = Edge("Agent1", "Tool1", EdgeType.TOOL)
    tool1_to_agent = Edge("Tool1", "Agent1", EdgeType.TOOL)
    agent_to_tool2 = Edge("Agent1", "Tool2", EdgeType.TOOL)
    tool2_to_agent = Edge("Tool2", "Agent1", EdgeType.TOOL)
    agent_to_handoff = Edge("Agent1", "Handoff1", EdgeType.HANDOFF)

    assert any(
        e.source == start_to_agent.source and e.target == start_to_agent.target for e in graph.edges
    )
    assert any(
        e.source == agent_to_tool1.source and e.target == agent_to_tool1.target for e in graph.edges
    )
    assert any(
        e.source == tool1_to_agent.source and e.target == tool1_to_agent.target for e in graph.edges
    )
    assert any(
        e.source == agent_to_tool2.source and e.target == agent_to_tool2.target for e in graph.edges
    )
    assert any(
        e.source == tool2_to_agent.source and e.target == tool2_to_agent.target for e in graph.edges
    )
    assert any(
        e.source == agent_to_handoff.source and e.target == agent_to_handoff.target
        for e in graph.edges
    )


def test_graphviz_renderer(mock_agent):
    builder = GraphBuilder()
    graph = builder.build_from_agent(mock_agent)
    renderer = GraphvizRenderer()
    dot_code = renderer.render(graph)

    assert "digraph G" in dot_code
    assert "graph [splines=true];" in dot_code
    assert 'node [fontname="Arial"];' in dot_code
    assert "edge [penwidth=1.5];" in dot_code
    assert START_NODE in dot_code
    assert END_NODE in dot_code
    assert AGENT_NODE in dot_code
    assert TOOL1_NODE in dot_code
    assert TOOL2_NODE in dot_code
    assert HANDOFF_NODE in dot_code


def test_recursive_graph_builder(mock_recursive_agents):
    builder = GraphBuilder()
    graph = builder.build_from_agent(mock_recursive_agents)

    # Check nodes
    assert "Agent1" in graph.nodes
    assert "Agent2" in graph.nodes
    assert graph.nodes["Agent1"].type == NodeType.AGENT
    assert graph.nodes["Agent2"].type == NodeType.AGENT

    # Check edges
    agent1_to_agent2 = Edge("Agent1", "Agent2", EdgeType.HANDOFF)
    agent2_to_agent1 = Edge("Agent2", "Agent1", EdgeType.HANDOFF)

    assert any(
        e.source == agent1_to_agent2.source and e.target == agent1_to_agent2.target
        for e in graph.edges
    )
    assert any(
        e.source == agent2_to_agent1.source and e.target == agent2_to_agent1.target
        for e in graph.edges
    )


def test_graph_validation():
    graph = Graph()

    # Test adding valid nodes and edges
    node1 = Node("1", "Node 1", NodeType.AGENT)
    node2 = Node("2", "Node 2", NodeType.TOOL)
    graph.add_node(node1)
    graph.add_node(node2)

    valid_edge = Edge("1", "2", EdgeType.TOOL)
    graph.add_edge(valid_edge)

    # Test adding edge with non-existent source
    invalid_edge1 = Edge("3", "2", EdgeType.TOOL)
    with pytest.raises(ValueError, match="Source node '3' does not exist in the graph"):
        graph.add_edge(invalid_edge1)

    # Test adding edge with non-existent target
    invalid_edge2 = Edge("1", "3", EdgeType.TOOL)
    with pytest.raises(ValueError, match="Target node '3' does not exist in the graph"):
        graph.add_edge(invalid_edge2)

    # Test helper methods
    assert graph.has_node("1")
    assert graph.has_node("2")
    assert not graph.has_node("3")

    assert graph.get_node("1") == node1
    assert graph.get_node("2") == node2
    assert graph.get_node("3") is None


def test_node_immutability():
    node = Node("1", "Node 1", NodeType.AGENT)
    with pytest.raises(dataclasses.FrozenInstanceError):
        node.id = "2"
    with pytest.raises(dataclasses.FrozenInstanceError):
        node.label = "Node 2"
    with pytest.raises(dataclasses.FrozenInstanceError):
        node.type = NodeType.TOOL


def test_edge_immutability():
    edge = Edge("1", "2", EdgeType.TOOL)
    with pytest.raises(dataclasses.FrozenInstanceError):
        edge.source = "3"
    with pytest.raises(dataclasses.FrozenInstanceError):
        edge.target = "3"
    with pytest.raises(dataclasses.FrozenInstanceError):
        edge.type = EdgeType.HANDOFF


def test_draw_graph_with_invalid_renderer(mock_agent):
    with pytest.raises(ValueError, match="Unsupported renderer: invalid"):
        draw_graph(mock_agent, renderer="invalid")


def test_draw_graph_default_renderer(mock_agent):
    result = draw_graph(mock_agent)
    assert isinstance(result, GraphView)
    assert "digraph G" in result.rendered_graph


def test_draw_graph_with_filename(mock_agent, tmp_path):
    filename = tmp_path / "test_graph"
    result = draw_graph(mock_agent, filename=str(filename))
    assert isinstance(result, GraphView)
    assert "digraph G" in result.rendered_graph
    assert (tmp_path / "test_graph.png").exists()


def test_draw_graph_with_graphviz(mock_agent):
    result = draw_graph(mock_agent, renderer="graphviz")
    assert isinstance(result, GraphView)
    assert "digraph G" in result.rendered_graph
    assert "graph [splines=true];" in result.rendered_graph
    assert 'node [fontname="Arial"];' in result.rendered_graph
    assert "edge [penwidth=1.5];" in result.rendered_graph
    assert START_NODE in result.rendered_graph
    assert END_NODE in result.rendered_graph
    assert AGENT_NODE in result.rendered_graph
    assert TOOL1_NODE in result.rendered_graph
    assert TOOL2_NODE in result.rendered_graph
    assert HANDOFF_NODE in result.rendered_graph


def test_draw_graph_with_mermaid(mock_agent):
    result = draw_graph(mock_agent, renderer="mermaid")
    assert isinstance(result, GraphView)
    assert "graph TD" in result.rendered_graph
    assert "__start__(__start__)" in result.rendered_graph
    assert "style __start__ fill:lightblue" in result.rendered_graph
    assert "Agent1[Agent1]" in result.rendered_graph
    assert "style Agent1 fill:lightyellow" in result.rendered_graph


def test_draw_graph_with_filename_graphviz(mock_agent, tmp_path):
    filename = tmp_path / "test_graph"
    result = draw_graph(mock_agent, filename=str(filename), renderer="graphviz")
    assert isinstance(result, GraphView)
    assert "digraph G" in result.rendered_graph
    assert (tmp_path / "test_graph.png").exists()


def test_draw_graph_with_filename_mermaid(mock_agent, tmp_path):
    filename = tmp_path / "test_graph"
    mock_response = Mock()
    mock_response.content = b"mock image data"
    mock_response.raise_for_status = Mock()

    with patch("requests.get", return_value=mock_response):
        result = draw_graph(mock_agent, filename=str(filename), renderer="mermaid")
        assert isinstance(result, GraphView)
        assert "graph TD" in result.rendered_graph
        assert (tmp_path / "test_graph.png").exists()
        with open(tmp_path / "test_graph.png", "rb") as f:
            assert f.read() == b"mock image data"


def test_draw_graph(mock_agent):
    result = draw_graph(mock_agent)
    assert isinstance(result, GraphView)
    assert "digraph G" in result.rendered_graph


# Legacy function tests
def test_get_main_graph(mock_agent):
    with pytest.warns(DeprecationWarning):
        result = get_main_graph(mock_agent)
    assert "digraph G" in result
    assert "graph [splines=true];" in result
    assert 'node [fontname="Arial"];' in result
    assert "edge [penwidth=1.5];" in result
    assert START_NODE in result
    assert END_NODE in result
    assert AGENT_NODE in result
    assert TOOL1_NODE in result
    assert TOOL2_NODE in result
    assert HANDOFF_NODE in result


def test_get_all_nodes(mock_agent):
    with pytest.warns(DeprecationWarning):
        result = get_all_nodes(mock_agent)
    assert START_NODE in result
    assert END_NODE in result
    assert AGENT_NODE in result
    assert TOOL1_NODE in result
    assert TOOL2_NODE in result
    assert HANDOFF_NODE in result


def test_get_all_edges(mock_agent):
    with pytest.warns(DeprecationWarning):
        result = get_all_edges(mock_agent)
    assert '"__start__" -> "Agent1";' in result
    assert '"Agent1" -> "Tool1" [style=dotted, penwidth=1.5];' in result
    assert '"Tool1" -> "Agent1" [style=dotted, penwidth=1.5];' in result
    assert '"Agent1" -> "Tool2" [style=dotted, penwidth=1.5];' in result
    assert '"Tool2" -> "Agent1" [style=dotted, penwidth=1.5];' in result
    assert '"Agent1" -> "Handoff1";' in result


def test_recursive_handoff_loop(mock_recursive_agents):
    with pytest.warns(DeprecationWarning):
        dot = get_main_graph(mock_recursive_agents)

    assert (
        '"Agent1" [label="Agent1", shape=box, style=filled, '
        "fillcolor=lightyellow, width=1.5, height=0.8];" in dot
    )
    assert (
        '"Agent2" [label="Agent2", shape=box, style=filled, '
        "fillcolor=lightyellow, width=1.5, height=0.8];" in dot
    )
    assert '"Agent1" -> "Agent2";' in dot
    assert '"Agent2" -> "Agent1";' in dot


def test_mermaid_renderer(mock_agent):
    builder = GraphBuilder()
    graph = builder.build_from_agent(mock_agent)
    renderer = MermaidRenderer()
    mermaid_code = renderer.render(graph)

    # Test flowchart header
    assert "graph TD" in mermaid_code

    # Test node rendering
    assert "__start__(__start__)" in mermaid_code
    assert "style __start__ fill:lightblue" in mermaid_code
    assert "__end__(__end__)" in mermaid_code
    assert "style __end__ fill:lightblue" in mermaid_code
    assert "Agent1[Agent1]" in mermaid_code
    assert "style Agent1 fill:lightyellow" in mermaid_code
    assert "Tool1((Tool1))" in mermaid_code
    assert "style Tool1 fill:lightgreen" in mermaid_code
    assert "Tool2((Tool2))" in mermaid_code
    assert "style Tool2 fill:lightgreen" in mermaid_code
    assert "Handoff1[Handoff1]" in mermaid_code
    assert "style Handoff1 fill:lightyellow" in mermaid_code

    # Test edge rendering
    assert "__start__ --> Agent1" in mermaid_code
    assert "Agent1 -.-> Tool1" in mermaid_code
    assert "Tool1 -.-> Agent1" in mermaid_code
    assert "Agent1 -.-> Tool2" in mermaid_code
    assert "Tool2 -.-> Agent1" in mermaid_code
    assert "Agent1 --> Handoff1" in mermaid_code


def test_mermaid_renderer_save(mock_agent, tmp_path):
    renderer = MermaidRenderer()
    graph = GraphBuilder().build_from_agent(mock_agent)
    rendered = renderer.render(graph)
    filename = tmp_path / "test_graph"

    mock_response = Mock()
    mock_response.content = b"mock image data"
    mock_response.raise_for_status = Mock()

    with patch("requests.get", return_value=mock_response):
        renderer.save(rendered, str(filename))
        assert (tmp_path / "test_graph.png").exists()
        with open(tmp_path / "test_graph.png", "rb") as f:
            assert f.read() == b"mock image data"
