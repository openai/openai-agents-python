import pytest

from _agents_codex._agent_profiles import _capture_agent_graph
from agents import Agent


def test_late_reachable_leaf_preserves_existing_profile_ids():
    leaf = Agent(name="Leaf", instructions="leaf")
    child = Agent(name="Child", instructions="child", handoffs=(leaf,))
    before = Agent(name="Source", instructions="source", handoffs=(child,))
    late = Agent(name="Runtime target", instructions="late")
    after = Agent(name="Source", instructions="source", handoffs=(late, child))
    original = _capture_agent_graph(before)
    current = _capture_agent_graph(after, restored_ids={"Runtime target": "runtime_7_0"})
    assert [(p.agent.name, p.id) for p in original] == [
        ("Source", "agent_0"),
        ("Child", "agent_1"),
        ("Leaf", "agent_2"),
    ]
    assert [(p.agent.name, p.id) for p in current] == [
        ("Source", "agent_0"),
        ("Runtime target", "runtime_7_0"),
        ("Child", "agent_1"),
        ("Leaf", "agent_2"),
    ]
    assert current[0].handoffs["transfer_to_child"] == "agent_1"
    assert current[0].handoffs["transfer_to_runtime_target"] == "runtime_7_0"
    assert current[2].handoffs["transfer_to_leaf"] == "agent_2"


def test_enrollment_cannot_replace_original_root():
    with pytest.raises(ValueError, match="original root"):
        _capture_agent_graph(
            Agent(name="Runtime target", instructions="late"),
            restored_ids={"Runtime target": "runtime_7_0"},
        )


def test_current_enrollment_owner_requires_unique_reachable_name():
    first = Agent(name="Runtime target", instructions="one")
    second = Agent(name="Runtime target", instructions="two")
    root = Agent(name="Source", instructions="source", handoffs=(first, second))
    with pytest.raises(ValueError, match="unique current Agent names"):
        _capture_agent_graph(root, restored_ids={"Runtime target": "runtime_7_0"})
