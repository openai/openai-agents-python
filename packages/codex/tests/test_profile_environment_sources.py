"""Distinct graph sources join once without sharing task-affine ownership."""

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar

import pytest

from _agents_codex._agent_profiles import _capture_agent_graph
from _agents_codex._environment_lifetime import _EnvironmentLease, _release_environment
from _agents_codex._profile_environments import _acquire_profile_environments
from agents import Agent, Environment


@pytest.mark.parametrize("fail", ["none", "entry", "exit"])
async def test_graph_sources_join_in_reverse_order_and_preserve_task_context(tmp_path, fail):
    events = []
    task_ids = {}
    marker = ContextVar("source", default="application")
    environment = Environment.local(cwd=str(tmp_path), tools=())

    @asynccontextmanager
    async def source(name):
        assert marker.get() == "application"
        token = marker.set(name)
        task_ids[name] = asyncio.current_task()
        events.append("enter-" + name)
        try:
            if name == "C" and fail == "entry":
                raise RuntimeError("entry-C")
            yield environment
        finally:
            assert asyncio.current_task() is task_ids[name]
            assert marker.get() == name
            marker.reset(token)
            events.append("exit-" + name)
            if name == "B" and fail == "exit":
                raise RuntimeError("exit-B")

    root_source = source("A")
    root_lease = _EnvironmentLease(root_source)
    selected = await root_lease.acquire()
    repeated = source("B")
    last = Agent(name="C", instructions="C", environment=source("C"))
    alias = Agent(name="Alias", instructions="Alias", environment=repeated)
    target = Agent(name="B", instructions="B", environment=repeated, handoffs=[alias, last])
    root = Agent(name="A", instructions="A", handoffs=[target])
    profiles = _capture_agent_graph(root)
    if fail == "entry":
        with pytest.raises(RuntimeError, match="entry-C"):
            await _acquire_profile_environments(profiles, selected, root_lease)
    else:
        environments, sources, owner, workspace = await _acquire_profile_environments(
            profiles, selected, root_lease
        )
        assert owner is root_lease
        assert len({id(value) for value in sources.values()}) == 3
        assert all(value is environment for value in environments.values())
        assert marker.get() == "application"
        if fail == "exit":
            with pytest.raises(RuntimeError, match="exit-B"):
                await _release_environment(owner)
        else:
            await _release_environment(owner)
    assert events == ["enter-A", "enter-B", "enter-C", "exit-C", "exit-B", "exit-A"]
    assert len(set(task_ids.values())) == 3
    assert marker.get() == "application"


async def test_root_source_is_not_reentered_for_an_explicit_alias(tmp_path):
    events = []
    environment = Environment.local(cwd=str(tmp_path), tools=())

    @asynccontextmanager
    async def source():
        events.append("enter")
        try:
            yield environment
        finally:
            events.append("exit")

    selected_source = source()
    lease = _EnvironmentLease(selected_source)
    selected = await lease.acquire()
    target = Agent(name="Target", instructions="Target", environment=selected_source)
    root = Agent(name="Root", instructions="Root", handoffs=[target])
    environments, sources, owner, workspace = await _acquire_profile_environments(
        _capture_agent_graph(root), selected, lease
    )
    assert owner is lease
    assert all(value is environment for value in environments.values())
    assert all(value is selected_source for value in sources.values())
    await _release_environment(owner)
    assert events == ["enter", "exit"]


async def test_distinct_workspace_text_is_captured_and_joined(tmp_path):
    from _agents_codex._managed_workspace import _WORKSPACE_INSTRUCTIONS_ATTRIBUTE

    events = []
    root_environment = Environment.local(cwd=str(tmp_path), tools=())
    target_path = tmp_path / "target"
    target_path.mkdir()
    target_environment = Environment.local(cwd=str(target_path), tools=())

    @asynccontextmanager
    async def source(name, environment):
        events.append("enter-" + name)
        try:
            yield environment
        finally:
            events.append("exit-" + name)

    root_source = source("root", root_environment)
    lease = _EnvironmentLease(root_source)
    selected = await lease.acquire()
    target_source = source("target", target_environment)
    setattr(target_source, _WORKSPACE_INSTRUCTIONS_ATTRIBUTE, "target-only instructions")
    target = Agent(name="Target", instructions="Target", environment=target_source)
    root = Agent(name="Root", instructions="Root", handoffs=[target])
    _, _, owner, workspace = await _acquire_profile_environments(
        _capture_agent_graph(root), selected, lease
    )
    assert workspace == {"agent_0": None, "agent_1": "target-only instructions"}
    setattr(target_source, _WORKSPACE_INSTRUCTIONS_ATTRIBUTE, "later source mutation")
    assert workspace["agent_1"] == "target-only instructions"
    await _release_environment(owner)
    assert events == ["enter-root", "enter-target", "exit-target", "exit-root"]
