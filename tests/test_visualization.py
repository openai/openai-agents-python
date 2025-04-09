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

    # Find nodes by name
    agent_node = next(node for node in graph.nodes.values() if node.label == "Agent1")
    tool1_node = next(node for node in graph.nodes.values() if node.label == "Tool1")
    tool2_node = next(node for node in graph.nodes.values() if node.label == "Tool2")
    handoff_node = next(node for node in graph.nodes.values() if node.label == "Handoff1")

    # Check node types
    assert graph.nodes["__start__"].type == NodeType.START
    assert graph.nodes["__end__"].type == NodeType.END
    assert agent_node.type == NodeType.AGENT
    assert tool1_node.type == NodeType.TOOL
    assert tool2_node.type == NodeType.TOOL
    assert handoff_node.type == NodeType.HANDOFF

    # Check edges
    start_node = graph.nodes["__start__"]

    start_to_agent = Edge(start_node, agent_node, EdgeType.HANDOFF)
    agent_to_tool1 = Edge(agent_node, tool1_node, EdgeType.TOOL)
    tool1_to_agent = Edge(tool1_node, agent_node, EdgeType.TOOL)
    agent_to_tool2 = Edge(agent_node, tool2_node, EdgeType.TOOL)
    tool2_to_agent = Edge(tool2_node, agent_node, EdgeType.TOOL)
    agent_to_handoff = Edge(agent_node, handoff_node, EdgeType.HANDOFF)

    assert any(
        e.source.id == start_to_agent.source.id and e.target.id == start_to_agent.target.id
        for e in graph.edges
    )
    assert any(
        e.source.id == agent_to_tool1.source.id and e.target.id == agent_to_tool1.target.id
        for e in graph.edges
    )
    assert any(
        e.source.id == tool1_to_agent.source.id and e.target.id == tool1_to_agent.target.id
        for e in graph.edges
    )
    assert any(
        e.source.id == agent_to_tool2.source.id and e.target.id == agent_to_tool2.target.id
        for e in graph.edges
    )
    assert any(
        e.source.id == tool2_to_agent.source.id and e.target.id == tool2_to_agent.target.id
        for e in graph.edges
    )
    assert any(
        e.source.id == agent_to_handoff.source.id and e.target.id == agent_to_handoff.target.id
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

    # Find nodes by name in rendered output
    agent_node = next(node for node in graph.nodes.values() if node.label == "Agent1")
    tool1_node = next(node for node in graph.nodes.values() if node.label == "Tool1")
    tool2_node = next(node for node in graph.nodes.values() if node.label == "Tool2")
    handoff_node = next(node for node in graph.nodes.values() if node.label == "Handoff1")

    # Check node definitions in dot code
    agent_style = (
        f'"{agent_node.id}" [label="Agent1", shape=box, style=filled, '
        "fillcolor=lightyellow, width=1.5, height=0.8];"
    )
    assert agent_style in dot_code
    tool1_style = (
        f'"{tool1_node.id}" [label="Tool1", shape=ellipse, style=filled, '
        "fillcolor=lightgreen, width=0.5, height=0.3];"
    )
    assert tool1_style in dot_code
    tool2_style = (
        f'"{tool2_node.id}" [label="Tool2", shape=ellipse, style=filled, '
        "fillcolor=lightgreen, width=0.5, height=0.3];"
    )
    assert tool2_style in dot_code
    handoff_style = (
        f'"{handoff_node.id}" [label="Handoff1", shape=box, style=filled, '
        "fillcolor=lightyellow, width=1.5, height=0.8];"
    )
    assert handoff_style in dot_code


def test_recursive_graph_builder(mock_recursive_agents):
    builder = GraphBuilder()
    graph = builder.build_from_agent(mock_recursive_agents)

    # Find nodes by name
    agent1_node = next(node for node in graph.nodes.values() if node.label == "Agent1")
    agent2_node = next(node for node in graph.nodes.values() if node.label == "Agent2")

    # Check node types
    assert agent1_node.type == NodeType.AGENT
    assert agent2_node.type == NodeType.AGENT

    # Check edges
    agent1_to_agent2 = Edge(agent1_node, agent2_node, EdgeType.HANDOFF)
    agent2_to_agent1 = Edge(agent2_node, agent1_node, EdgeType.HANDOFF)

    assert any(
        e.source.id == agent1_to_agent2.source.id and e.target.id == agent1_to_agent2.target.id
        for e in graph.edges
    )
    assert any(
        e.source.id == agent2_to_agent1.source.id and e.target.id == agent2_to_agent1.target.id
        for e in graph.edges
    )


def test_graph_validation():
    graph = Graph()

    # Test adding valid nodes and edges
    node1 = Node("1", "Node 1", NodeType.AGENT)
    node2 = Node("2", "Node 2", NodeType.TOOL)
    graph.add_node(node1)
    graph.add_node(node2)

    valid_edge = Edge(node1, node2, EdgeType.TOOL)
    graph.add_edge(valid_edge)

    # Test adding edge with non-existent source
    node3 = Node("3", "Node 3", NodeType.TOOL)
    invalid_edge1 = Edge(node3, node2, EdgeType.TOOL)
    with pytest.raises(ValueError, match="Source node '3' does not exist in the graph"):
        graph.add_edge(invalid_edge1)

    # Test adding edge with non-existent target
    invalid_edge2 = Edge(node1, node3, EdgeType.TOOL)
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

    # Get the graph to find node IDs
    builder = GraphBuilder()
    graph = builder.build_from_agent(mock_agent)
    agent_node = next(node for node in graph.nodes.values() if node.label == "Agent1")
    tool1_node = next(node for node in graph.nodes.values() if node.label == "Tool1")
    tool2_node = next(node for node in graph.nodes.values() if node.label == "Tool2")
    handoff_node = next(node for node in graph.nodes.values() if node.label == "Handoff1")

    # Check node definitions in dot code
    agent_style = (
        f'"{agent_node.id}" [label="Agent1", shape=box, style=filled, '
        "fillcolor=lightyellow, width=1.5, height=0.8];"
    )
    assert agent_style in result.rendered_graph
    tool1_style = (
        f'"{tool1_node.id}" [label="Tool1", shape=ellipse, style=filled, '
        "fillcolor=lightgreen, width=0.5, height=0.3];"
    )
    assert tool1_style in result.rendered_graph
    tool2_style = (
        f'"{tool2_node.id}" [label="Tool2", shape=ellipse, style=filled, '
        "fillcolor=lightgreen, width=0.5, height=0.3];"
    )
    assert tool2_style in result.rendered_graph
    handoff_style = (
        f'"{handoff_node.id}" [label="Handoff1", shape=box, style=filled, '
        "fillcolor=lightyellow, width=1.5, height=0.8];"
    )
    assert handoff_style in result.rendered_graph


def test_draw_graph_with_mermaid(mock_agent):
    result = draw_graph(mock_agent, renderer="mermaid")
    assert isinstance(result, GraphView)
    assert "graph TD" in result.rendered_graph

    # Get the graph to find node IDs
    builder = GraphBuilder()
    graph = builder.build_from_agent(mock_agent)
    agent_node = next(node for node in graph.nodes.values() if node.label == "Agent1")

    assert f"{agent_node.id}[Agent1]" in result.rendered_graph
    assert f"style {agent_node.id} fill:lightyellow" in result.rendered_graph


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

    # Get the graph to find node IDs
    builder = GraphBuilder()
    graph = builder.build_from_agent(mock_agent)
    agent_node = next(node for node in graph.nodes.values() if node.label == "Agent1")
    tool1_node = next(node for node in graph.nodes.values() if node.label == "Tool1")
    tool2_node = next(node for node in graph.nodes.values() if node.label == "Tool2")
    handoff_node = next(node for node in graph.nodes.values() if node.label == "Handoff1")

    # Check node definitions in dot code
    agent_style = (
        f'"{agent_node.id}" [label="Agent1", shape=box, style=filled, '
        "fillcolor=lightyellow, width=1.5, height=0.8];"
    )
    assert agent_style in result
    tool1_style = (
        f'"{tool1_node.id}" [label="Tool1", shape=ellipse, style=filled, '
        "fillcolor=lightgreen, width=0.5, height=0.3];"
    )
    assert tool1_style in result
    tool2_style = (
        f'"{tool2_node.id}" [label="Tool2", shape=ellipse, style=filled, '
        "fillcolor=lightgreen, width=0.5, height=0.3];"
    )
    assert tool2_style in result
    handoff_style = (
        f'"{handoff_node.id}" [label="Handoff1", shape=box, style=filled, '
        "fillcolor=lightyellow, width=1.5, height=0.8];"
    )
    assert handoff_style in result


def test_get_all_nodes(mock_agent):
    with pytest.warns(DeprecationWarning):
        result = get_all_nodes(mock_agent)

    # Get the graph to find node IDs
    builder = GraphBuilder()
    graph = builder.build_from_agent(mock_agent)
    agent_node = next(node for node in graph.nodes.values() if node.label == "Agent1")
    tool1_node = next(node for node in graph.nodes.values() if node.label == "Tool1")
    tool2_node = next(node for node in graph.nodes.values() if node.label == "Tool2")
    handoff_node = next(node for node in graph.nodes.values() if node.label == "Handoff1")

    # Check node definitions in dot code
    agent_style = (
        f'"{agent_node.id}" [label="Agent1", shape=box, style=filled, '
        "fillcolor=lightyellow, width=1.5, height=0.8];"
    )
    assert agent_style in result
    tool1_style = (
        f'"{tool1_node.id}" [label="Tool1", shape=ellipse, style=filled, '
        "fillcolor=lightgreen, width=0.5, height=0.3];"
    )
    assert tool1_style in result
    tool2_style = (
        f'"{tool2_node.id}" [label="Tool2", shape=ellipse, style=filled, '
        "fillcolor=lightgreen, width=0.5, height=0.3];"
    )
    assert tool2_style in result
    handoff_style = (
        f'"{handoff_node.id}" [label="Handoff1", shape=box, style=filled, '
        "fillcolor=lightyellow, width=1.5, height=0.8];"
    )
    assert handoff_style in result


def test_get_all_edges(mock_agent):
    with pytest.warns(DeprecationWarning):
        result = get_all_edges(mock_agent)

    # Get the graph to find node IDs
    builder = GraphBuilder()
    graph = builder.build_from_agent(mock_agent)
    start_node = graph.nodes["__start__"]
    agent_node = next(node for node in graph.nodes.values() if node.label == "Agent1")
    tool1_node = next(node for node in graph.nodes.values() if node.label == "Tool1")
    tool2_node = next(node for node in graph.nodes.values() if node.label == "Tool2")
    handoff_node = next(node for node in graph.nodes.values() if node.label == "Handoff1")

    # Check edge definitions
    assert f'"{start_node.id}" -> "{agent_node.id}";' in result
    assert f'"{agent_node.id}" -> "{tool1_node.id}" [style=dotted, penwidth=1.5];' in result
    assert f'"{tool1_node.id}" -> "{agent_node.id}" [style=dotted, penwidth=1.5];' in result
    assert f'"{agent_node.id}" -> "{tool2_node.id}" [style=dotted, penwidth=1.5];' in result
    assert f'"{tool2_node.id}" -> "{agent_node.id}" [style=dotted, penwidth=1.5];' in result
    assert f'"{agent_node.id}" -> "{handoff_node.id}";' in result


def test_recursive_handoff_loop(mock_recursive_agents):
    with pytest.warns(DeprecationWarning):
        dot = get_main_graph(mock_recursive_agents)

    # Get the graph to find node IDs
    builder = GraphBuilder()
    graph = builder.build_from_agent(mock_recursive_agents)
    agent1_node = next(node for node in graph.nodes.values() if node.label == "Agent1")
    agent2_node = next(node for node in graph.nodes.values() if node.label == "Agent2")

    # Check node and edge definitions
    agent1_style = (
        f'"{agent1_node.id}" [label="Agent1", shape=box, style=filled, '
        "fillcolor=lightyellow, width=1.5, height=0.8];"
    )
    assert agent1_style in dot
    agent2_style = (
        f'"{agent2_node.id}" [label="Agent2", shape=box, style=filled, '
        "fillcolor=lightyellow, width=1.5, height=0.8];"
    )
    assert agent2_style in dot
    assert f'"{agent1_node.id}" -> "{agent2_node.id}";' in dot
    assert f'"{agent2_node.id}" -> "{agent1_node.id}";' in dot


def test_mermaid_renderer(mock_agent):
    builder = GraphBuilder()
    graph = builder.build_from_agent(mock_agent)
    renderer = MermaidRenderer()
    mermaid_code = renderer.render(graph)

    # Test flowchart header
    assert "graph TD" in mermaid_code

    # Find nodes by name
    agent_node = next(node for node in graph.nodes.values() if node.label == "Agent1")
    tool1_node = next(node for node in graph.nodes.values() if node.label == "Tool1")
    tool2_node = next(node for node in graph.nodes.values() if node.label == "Tool2")
    handoff_node = next(node for node in graph.nodes.values() if node.label == "Handoff1")

    # Test node rendering
    assert f"{agent_node.id}[Agent1]" in mermaid_code
    assert f"style {agent_node.id} fill:lightyellow" in mermaid_code
    assert f"{tool1_node.id}((Tool1))" in mermaid_code
    assert f"style {tool1_node.id} fill:lightgreen" in mermaid_code
    assert f"{tool2_node.id}((Tool2))" in mermaid_code
    assert f"style {tool2_node.id} fill:lightgreen" in mermaid_code
    assert f"{handoff_node.id}[Handoff1]" in mermaid_code
    assert f"style {handoff_node.id} fill:lightyellow" in mermaid_code
