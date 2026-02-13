import sys
from unittest.mock import Mock

import graphviz  # type: ignore
import pytest

from agents import Agent
from agents.extensions.visualization import (
    draw_graph,
    get_all_edges,
    get_all_nodes,
    get_main_graph,
)
from agents.handoffs import Handoff

if sys.version_info >= (3, 10):
    from .mcp.helpers import FakeMCPServer


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
    agent.mcp_servers = []

    if sys.version_info >= (3, 10):
        agent.mcp_servers = [FakeMCPServer(server_name="MCPServer1")]

    return agent


def test_get_main_graph(mock_agent):
    result = get_main_graph(mock_agent)
    print(result)
    assert "digraph G" in result
    assert "graph [splines=true];" in result
    assert 'node [fontname="Arial"];' in result
    assert "edge [penwidth=1.5];" in result
    assert (
        '"__start__" [label="__start__", shape=ellipse, style=filled, '
        "fillcolor=lightblue, width=0.5, height=0.3];" in result
    )
    assert (
        '"__end__" [label="__end__", shape=ellipse, style=filled, '
        "fillcolor=lightblue, width=0.5, height=0.3];" in result
    )
    assert (
        '"Agent1" [label="Agent1", shape=box, style=filled, '
        "fillcolor=lightyellow, width=1.5, height=0.8];" in result
    )
    assert (
        '"Tool1" [label="Tool1", shape=ellipse, style=filled, '
        "fillcolor=lightgreen, width=0.5, height=0.3];" in result
    )
    assert (
        '"Tool2" [label="Tool2", shape=ellipse, style=filled, '
        "fillcolor=lightgreen, width=0.5, height=0.3];" in result
    )
    assert (
        '"Handoff1" [label="Handoff1", shape=box, style=filled, style=rounded, '
        "fillcolor=lightyellow, width=1.5, height=0.8];" in result
    )
    _assert_mcp_nodes(result)


def test_get_all_nodes(mock_agent):
    result = get_all_nodes(mock_agent)
    assert (
        '"__start__" [label="__start__", shape=ellipse, style=filled, '
        "fillcolor=lightblue, width=0.5, height=0.3];" in result
    )
    assert (
        '"__end__" [label="__end__", shape=ellipse, style=filled, '
        "fillcolor=lightblue, width=0.5, height=0.3];" in result
    )
    assert (
        '"Agent1" [label="Agent1", shape=box, style=filled, '
        "fillcolor=lightyellow, width=1.5, height=0.8];" in result
    )
    assert (
        '"Tool1" [label="Tool1", shape=ellipse, style=filled, '
        "fillcolor=lightgreen, width=0.5, height=0.3];" in result
    )
    assert (
        '"Tool2" [label="Tool2", shape=ellipse, style=filled, '
        "fillcolor=lightgreen, width=0.5, height=0.3];" in result
    )
    assert (
        '"Handoff1" [label="Handoff1", shape=box, style=filled, style=rounded, '
        "fillcolor=lightyellow, width=1.5, height=0.8];" in result
    )
    _assert_mcp_nodes(result)


def test_get_all_edges(mock_agent):
    result = get_all_edges(mock_agent)
    assert '"__start__" -> "Agent1";' in result
    assert '"Agent1" -> "__end__";'
    assert '"Agent1" -> "Tool1" [style=dotted, penwidth=1.5];' in result
    assert '"Tool1" -> "Agent1" [style=dotted, penwidth=1.5];' in result
    assert '"Agent1" -> "Tool2" [style=dotted, penwidth=1.5];' in result
    assert '"Tool2" -> "Agent1" [style=dotted, penwidth=1.5];' in result
    assert '"Agent1" -> "Handoff1";' in result
    _assert_mcp_edges(result)


def test_draw_graph(mock_agent):
    graph = draw_graph(mock_agent)
    assert isinstance(graph, graphviz.Source)
    assert "digraph G" in graph.source
    assert "graph [splines=true];" in graph.source
    assert 'node [fontname="Arial"];' in graph.source
    assert "edge [penwidth=1.5];" in graph.source
    assert (
        '"__start__" [label="__start__", shape=ellipse, style=filled, '
        "fillcolor=lightblue, width=0.5, height=0.3];" in graph.source
    )
    assert (
        '"__end__" [label="__end__", shape=ellipse, style=filled, '
        "fillcolor=lightblue, width=0.5, height=0.3];" in graph.source
    )
    assert (
        '"Agent1" [label="Agent1", shape=box, style=filled, '
        "fillcolor=lightyellow, width=1.5, height=0.8];" in graph.source
    )
    assert (
        '"Tool1" [label="Tool1", shape=ellipse, style=filled, '
        "fillcolor=lightgreen, width=0.5, height=0.3];" in graph.source
    )
    assert (
        '"Tool2" [label="Tool2", shape=ellipse, style=filled, '
        "fillcolor=lightgreen, width=0.5, height=0.3];" in graph.source
    )
    assert (
        '"Handoff1" [label="Handoff1", shape=box, style=filled, style=rounded, '
        "fillcolor=lightyellow, width=1.5, height=0.8];" in graph.source
    )
    _assert_mcp_nodes(graph.source)


def _assert_mcp_nodes(source: str):
    if sys.version_info < (3, 10):
        assert "MCPServer1" not in source
        return
    assert (
        '"MCPServer1" [label="MCPServer1", shape=box, style=filled, '
        "fillcolor=lightgrey, width=1, height=0.5];" in source
    )


def _assert_mcp_edges(source: str):
    if sys.version_info < (3, 10):
        assert "MCPServer1" not in source
        return
    assert '"Agent1" -> "MCPServer1" [style=dashed, penwidth=1.5];' in source
    assert '"MCPServer1" -> "Agent1" [style=dashed, penwidth=1.5];' in source


def test_cycle_detection():
    agent_a = Agent(name="A")
    agent_b = Agent(name="B")
    agent_a.handoffs.append(agent_b)
    agent_b.handoffs.append(agent_a)

    nodes = get_all_nodes(agent_a)
    edges = get_all_edges(agent_a)

    assert nodes.count('"A" [label="A"') == 1
    assert nodes.count('"B" [label="B"') == 1
    assert '"A" -> "B"' in edges
    assert '"B" -> "A"' in edges


def test_draw_graph_with_real_agent_no_handoffs():
    """Test that draw_graph works with a real Agent object without handoffs.

    This test ensures that the visualization code does not use isinstance()
    with generic types (like Tool), which would fail on Python 3.12+.
    See: https://github.com/openai/openai-agents-python/issues/2397
    """
    agent = Agent(name="TestAgent", instructions="Test instructions")

    # This should not raise TypeError on Python 3.12+
    graph = draw_graph(agent)

    assert isinstance(graph, graphviz.Source)
    assert '"TestAgent"' in graph.source
    assert '"__start__" -> "TestAgent"' in graph.source
    # Agent without handoffs should connect to __end__
    assert '"TestAgent" -> "__end__"' in graph.source


def test_draw_graph_with_real_agent_with_handoffs():
    """Test draw_graph with real Agent objects that have handoffs."""
    child_agent = Agent(name="ChildAgent", instructions="Child instructions")
    parent_agent = Agent(
        name="ParentAgent",
        instructions="Parent instructions",
        handoffs=[child_agent],
    )

    graph = draw_graph(parent_agent)

    assert isinstance(graph, graphviz.Source)
    assert '"ParentAgent"' in graph.source
    assert '"ChildAgent"' in graph.source
    assert '"ParentAgent" -> "ChildAgent"' in graph.source
    # Parent has handoffs, so should NOT connect directly to __end__
    assert '"ParentAgent" -> "__end__"' not in graph.source
    # Child has no handoffs, so should connect to __end__
    assert '"ChildAgent" -> "__end__"' in graph.source
