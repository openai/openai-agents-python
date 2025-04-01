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


def test_get_all_edges(mock_agent):
    result = get_all_edges(mock_agent)
    assert '"__start__" -> "Agent1";' in result
    assert '"Agent1" -> "__end__";'
    assert '"Agent1" -> "Tool1" [style=dotted, penwidth=1.5];' in result
    assert '"Tool1" -> "Agent1" [style=dotted, penwidth=1.5];' in result
    assert '"Agent1" -> "Tool2" [style=dotted, penwidth=1.5];' in result
    assert '"Tool2" -> "Agent1" [style=dotted, penwidth=1.5];' in result
    assert '"Agent1" -> "Handoff1";' in result


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
