import re
from unittest.mock import Mock, PropertyMock

import graphviz  # type: ignore
import pytest

from agents import Agent, handoff
from agents.extensions.visualization import (
    draw_graph,
    get_all_edges,
    get_all_nodes,
    get_main_graph,
)
from agents.handoffs import Handoff

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
        '"Handoff1" [label="Handoff1", shape=box, style="filled,rounded", '
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
        '"Handoff1" [label="Handoff1", shape=box, style="filled,rounded", '
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
        '"Handoff1" [label="Handoff1", shape=box, style="filled,rounded", '
        "fillcolor=lightyellow, width=1.5, height=0.8];" in graph.source
    )
    _assert_mcp_nodes(graph.source)


def test_draw_graph_renders_filename(monkeypatch, mock_agent):
    render_calls: list[tuple[str, str, bool]] = []

    def fake_render(self, filename: str, *, format: str, cleanup: bool):
        render_calls.append((filename, format, cleanup))

    monkeypatch.setattr(graphviz.Source, "render", fake_render)

    graph = draw_graph(mock_agent, filename="agent_graph")

    assert isinstance(graph, graphviz.Source)
    assert render_calls == [("agent_graph", "png", True)]


def _assert_mcp_nodes(source: str):
    assert (
        '"MCPServer1" [label="MCPServer1", shape=box, style=filled, '
        "fillcolor=lightgrey, width=1, height=0.5];" in source
    )


def _assert_mcp_edges(source: str):
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


def test_graph_keeps_different_node_types_with_the_same_name_distinct():
    shared_tool = Mock()
    shared_tool.name = "shared"
    shared_handoff = Mock(spec=Handoff)
    shared_handoff.agent_name = "shared"
    agent = Mock(spec=Agent)
    agent.name = "shared"
    agent.tools = [shared_tool]
    agent.mcp_servers = [FakeMCPServer(server_name="shared")]
    agent.handoffs = [shared_handoff]

    source = get_main_graph(agent)

    node_ids = re.findall(r'"([^"]+)" \[label="shared"', source)
    assert len(node_ids) == 4
    assert len(set(node_ids)) == 4
    agent_id, tool_id, server_id, handoff_id = node_ids
    assert f'"{agent_id}" -> "{tool_id}" [style=dotted' in source
    assert f'"{agent_id}" -> "{server_id}" [style=dashed' in source
    assert f'"{agent_id}" -> "{handoff_id}";' in source
    assert all(f'"{node_id}" -> "{node_id}"' not in source for node_id in node_ids)


def test_graph_keeps_names_that_escape_to_the_same_id_distinct():
    shared_tool = Mock()
    shared_tool.name = "shared\r"
    shared_handoff = Mock(spec=Handoff)
    shared_handoff.agent_name = "shared\r\n"
    shared_handoff._agent_ref = None
    agent = Mock(spec=Agent)
    agent.name = "shared\n"
    agent.tools = [shared_tool]
    agent.mcp_servers = []
    agent.handoffs = [shared_handoff]

    source = get_main_graph(agent)

    node_ids = re.findall(r'"([^"]+)" \[label="shared\\n"', source)
    assert len(node_ids) == 3
    assert len(set(node_ids)) == 3
    agent_id, tool_id, handoff_id = node_ids
    assert f'"{agent_id}" -> "{tool_id}" [style=dotted' in source
    assert f'"{agent_id}" -> "{handoff_id}";' in source
    assert all(f'"{node_id}" -> "{node_id}"' not in source for node_id in node_ids)


@pytest.mark.parametrize("use_handoff_object", [False, True])
def test_graph_traverses_different_agents_with_the_same_name(
    use_handoff_object: bool,
):
    child_tool = Mock()
    child_tool.name = "child_tool"
    child = Agent(name="duplicate", tools=[child_tool])
    child_handoff = handoff(child) if use_handoff_object else child
    parent = Agent(name="duplicate", handoffs=[child_handoff])

    source = get_main_graph(parent)

    agent_ids = re.findall(r'"([^"]+)" \[label="duplicate"', source)
    assert len(agent_ids) == 2
    assert len(set(agent_ids)) == 2
    parent_id, child_id = agent_ids
    assert f'"{parent_id}" -> "{child_id}";' in source
    assert f'"{child_id}" -> "child_tool" [style=dotted' in source


@pytest.mark.parametrize("use_handoff_object", [False, True])
def test_get_all_nodes_honors_prepopulated_visited_names(
    use_handoff_object: bool,
):
    child = Agent(name="child")
    child_handoff = handoff(child) if use_handoff_object else child
    parent = Agent(name="parent", handoffs=[child_handoff])
    visited = {"child"}

    nodes = get_all_nodes(parent, visited=visited)

    assert '"child" [label="child"' not in nodes
    assert visited == {"parent", "child"}


@pytest.mark.parametrize("renderer", [get_all_nodes, get_all_edges])
def test_graph_does_not_inspect_previsited_root(renderer):
    agent = Mock(spec=Agent)
    agent.name = "visited"
    type(agent).tools = PropertyMock(
        side_effect=AssertionError("previsited agent should not be inspected")
    )

    assert renderer(agent, visited={"visited"}) == ""


@pytest.mark.parametrize("renderer", [get_all_nodes, get_all_edges])
def test_previsited_subgraph_does_not_affect_included_node_ids(renderer):
    included_tool = Mock()
    included_tool.name = "shared"
    skipped_tool = Mock()
    skipped_tool.name = "shared"
    child = Agent(name="child", tools=[skipped_tool])
    parent = Agent(name="parent", tools=[included_tool], handoffs=[child])

    source = renderer(parent, visited={"child"})

    assert '"shared"' in source
    assert "__agents_graph_tool_" not in source


@pytest.mark.parametrize("use_handoff_object", [False, True])
def test_previsited_agent_reserves_its_id(use_handoff_object: bool):
    tool = Mock()
    tool.name = "shared"
    child = Agent(name="shared")
    child_handoff = handoff(child) if use_handoff_object else child
    parent = Agent(name="parent", tools=[tool], handoffs=[child_handoff])

    nodes = get_all_nodes(parent, visited={"shared"})
    edges = get_all_edges(parent, visited={"shared"})

    tool_ids = re.findall(r'"([^"]+)" \[label="shared"', nodes)
    assert len(tool_ids) == 1
    assert tool_ids[0].startswith("__agents_graph_tool_")
    assert f'"parent" -> "{tool_ids[0]}" [style=dotted' in edges
    assert '"parent" -> "shared";' in edges


def test_collision_free_escaped_ids_keep_legacy_names():
    tool = Mock()
    tool.name = "shared\n"
    agent = Agent(name=r"shared\n", tools=[tool])

    source = get_main_graph(agent)

    assert '"shared\\\\n" [label="shared\\\\n"' in source
    assert '"shared\\n" [label="shared\\n"' in source
    assert "__agents_graph_" not in source


def test_graph_reserves_start_and_end_node_ids():
    agent = Agent(name="__start__")

    source = get_main_graph(agent)

    start_ids = re.findall(r'"([^"]+)" \[label="__start__"', source)
    assert len(start_ids) == 2
    assert len(set(start_ids)) == 2
    assert start_ids[0] == "__start__"
    assert f'"__start__" -> "{start_ids[1]}";' in source


def test_names_with_quotes_and_backslashes_are_escaped(mock_agent):
    """Names containing double quotes or backslashes must be escaped in DOT.

    Otherwise an embedded quote closes the Graphviz identifier early and
    produces a malformed graph. Backslashes are escaped first, then quotes.
    """
    mock_agent.name = 'Weird"Name'
    mock_agent.tools[0].name = "Back\\slash"

    nodes = get_all_nodes(mock_agent)
    edges = get_all_edges(mock_agent)

    # The quote is backslash-escaped and the bare unescaped form is gone.
    assert '"Weird\\"Name" [label="Weird\\"Name"' in nodes
    assert '"Weird"Name"' not in nodes
    # The backslash is doubled.
    assert '"Back\\\\slash"' in nodes
    # Edges escape names too, so the start arrow points at the escaped id.
    assert '"__start__" -> "Weird\\"Name";' in edges


def test_names_with_line_breaks_are_escaped(mock_agent):
    """Line breaks in names must be encoded instead of splitting quoted DOT strings."""
    mock_agent.name = "Agent\nName"
    mock_agent.tools[0].name = "CRLF\r\nTool"
    mock_agent.tools[1].name = "CR\rTool"

    nodes = get_all_nodes(mock_agent)
    edges = get_all_edges(mock_agent)

    assert '"Agent\\nName" [label="Agent\\nName"' in nodes
    assert '"CRLF\\nTool"' in nodes
    assert '"CR\\nTool"' in nodes
    assert '"__start__" -> "Agent\\nName";' in edges
    assert '"Agent\nName"' not in nodes


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


def test_draw_graph_with_real_handoff_object():
    """Test draw_graph with a real Handoff object (not just Agent) in handoffs.

    Exercises the ``isinstance(handoff, Handoff)`` branches in get_all_nodes /
    get_all_edges (rather than the ``isinstance(handoff, Agent)`` branches),
    using the public ``handoff()`` factory rather than ``Mock(spec=Handoff)``.
    """
    child_tool = Mock()
    child_tool.name = "ChildTool"
    child_agent = Agent(
        name="ChildAgent",
        instructions="Child instructions",
        tools=[child_tool],
    )
    real_handoff = handoff(child_agent)
    assert isinstance(real_handoff, Handoff)

    parent_agent = Agent(
        name="ParentAgent",
        instructions="Parent instructions",
        handoffs=[real_handoff],
    )

    graph = draw_graph(parent_agent)

    assert isinstance(graph, graphviz.Source)
    assert '"ParentAgent"' in graph.source
    # Node uses the live handoff target agent, matching Agent-in-handoffs graphs.
    assert (
        '"ChildAgent" [label="ChildAgent", shape=box, style="filled,rounded", '
        "fillcolor=lightyellow, width=1.5, height=0.8];" in graph.source
    )
    assert (
        '"ChildTool" [label="ChildTool", shape=ellipse, style=filled, '
        "fillcolor=lightgreen, width=0.5, height=0.3];" in graph.source
    )
    # Edge points from parent to handoff target
    assert '"ParentAgent" -> "ChildAgent";' in graph.source
    # Parent has handoffs, so should NOT connect directly to __end__
    assert '"ParentAgent" -> "__end__"' not in graph.source
    # Child has no handoffs, so should connect to __end__ like Agent handoffs.
    assert '"ChildAgent" -> "__end__"' in graph.source


def test_draw_graph_keeps_stub_for_handoff_without_agent_ref():
    """Handoff objects without a recoverable agent remain name-only stubs."""
    stub_handoff = Mock(spec=Handoff)
    stub_handoff.agent_name = "ExternalHandoff"
    stub_handoff._agent_ref = None

    parent_agent = Agent(
        name="ParentAgent",
        instructions="Parent instructions",
        handoffs=[stub_handoff],
    )

    graph = draw_graph(parent_agent)

    assert (
        '"ExternalHandoff" [label="ExternalHandoff", shape=box, style="filled,rounded", '
        "fillcolor=lightyellow, width=1.5, height=0.8];" in graph.source
    )
    assert '"ParentAgent" -> "ExternalHandoff";' in graph.source
    assert '"ExternalHandoff" -> "__end__"' not in graph.source
