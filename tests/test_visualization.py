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

# Common test graph elements
START_NODE = '"__start__" [label="__start__", shape=ellipse, style=filled, fillcolor=lightblue, width=0.5, height=0.3];'
END_NODE = '"__end__" [label="__end__", shape=ellipse, style=filled, fillcolor=lightblue, width=0.5, height=0.3];'
AGENT_NODE = '"Agent1" [label="Agent1", shape=box, style=filled, fillcolor=lightyellow, width=1.5, height=0.8];'
TOOL1_NODE = '"Tool1" [label="Tool1", shape=ellipse, style=filled, fillcolor=lightgreen, width=0.5, height=0.3];'
TOOL2_NODE = '"Tool2" [label="Tool2", shape=ellipse, style=filled, fillcolor=lightgreen, width=0.5, height=0.3];'
HANDOFF_NODE = '"Handoff1" [label="Handoff1", shape=box, style=filled, fillcolor=lightyellow, width=1.5, height=0.8];'


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


def test_get_main_graph(mock_agent):
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
    result = get_all_nodes(mock_agent)
    assert START_NODE in result
    assert END_NODE in result
    assert AGENT_NODE in result
    assert TOOL1_NODE in result
    assert TOOL2_NODE in result
    assert HANDOFF_NODE in result


def test_get_all_edges(mock_agent):
    result = get_all_edges(mock_agent)
    assert '"__start__" -> "Agent1";' in result
    assert '"Agent1" -> "Tool1" [style=dotted, penwidth=1.5];' in result
    assert '"Tool1" -> "Agent1" [style=dotted, penwidth=1.5];' in result
    assert '"Agent1" -> "Tool2" [style=dotted, penwidth=1.5];' in result
    assert '"Tool2" -> "Agent1" [style=dotted, penwidth=1.5];' in result
    assert '"Agent1" -> "Handoff1";' in result


def test_draw_graph(mock_agent):
    graph = draw_graph(mock_agent)
    assert isinstance(graph, graphviz.Source)
    source = graph.source
    assert "digraph G" in source
    assert "graph [splines=true];" in source
    assert 'node [fontname="Arial"];' in source
    assert "edge [penwidth=1.5];" in source
    assert START_NODE in source
    assert END_NODE in source
    assert AGENT_NODE in source
    assert TOOL1_NODE in source
    assert TOOL2_NODE in source
    assert HANDOFF_NODE in source


def test_recursive_handoff_loop(mock_recursive_agents):
    agent1 = mock_recursive_agents
    dot = get_main_graph(agent1)

    assert (
        '"Agent1" [label="Agent1", shape=box, style=filled, fillcolor=lightyellow, width=1.5, height=0.8];'
        in dot
    )
    assert (
        '"Agent2" [label="Agent2", shape=box, style=filled, fillcolor=lightyellow, width=1.5, height=0.8];'
        in dot
    )
    assert '"Agent1" -> "Agent2";' in dot
    assert '"Agent2" -> "Agent1";' in dot
