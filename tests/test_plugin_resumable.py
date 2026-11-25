from dataclasses import replace

import pytest

from agents import ResumableSession, Runner, UserError
from agents.memory.resumable_session import ResumableSessionError
from agents.testing import ScriptedModel, assistant_message
from tests.memory.test_resumable_session import MemoryStore
from tests.test_plugin_state import config, reader


@pytest.mark.asyncio
async def test_missing_plugin_revision_leaves_session_ready_without_model_work() -> None:
    store = MemoryStore()
    session = await ResumableSession.create(store)
    ready = store.records[session.session_id]
    agent = reader([], [])
    with pytest.raises(UserError, match="plugin_revision"):
        await Runner.run(
            agent, "read", session=session, run_config=replace(config(), plugin_revision=None)
        )
    assert store.records[session.session_id] == ready
    assert isinstance(agent.model, ScriptedModel) and not agent.model.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("reload", [False, True], ids=["same-worker", "fresh-worker"])
async def test_resumable_plugin_approval_requires_current_revision_and_owner(reload: bool) -> None:
    store = MemoryStore()
    session = await ResumableSession.create(store)
    effects: list[str] = []
    agent = reader(effects, [])
    paused = await Runner.run(
        agent,
        "read",
        session=session,
        context={"allowed": True, "key": "private-key"},
        run_config=config(),
    )
    saved = store.records[session.session_id]
    assert session.is_paused and "private-key" not in saved
    if reload:
        session = await ResumableSession.resume(store, session.session_id)
        discoveries: list[str] = []
        agent = reader(effects, discoveries).clone(
            model=ScriptedModel([[assistant_message("done")]])
        )
        changed = agent.clone(plugins=[replace(agent.plugins[0], name="other"), agent.plugins[1]])
        for candidate, revision in ((agent, "old"), (changed, "r1")):
            with pytest.raises(ResumableSessionError):
                await session.load_state(
                    candidate, plugin_revision=revision, context_override={"allowed": True}
                )
        assert not discoveries
        state = await session.load_state(
            agent, plugin_revision="r1", context_override={"allowed": True}
        )
        with pytest.raises(UserError):
            state.approve(paused.interruptions[0])
    else:
        state = paused.to_state()
    state.approve(state.get_interruptions()[0])
    with pytest.raises(UserError):
        await Runner.run(agent, state, run_config=replace(config(), plugin_revision="old"))
    assert store.records[session.session_id] == saved and not effects
    finished = await Runner.run(agent, state, run_config=config())
    assert finished.final_output == "done" and effects == ["N42"]
    assert store.checkpoint(session.session_id)["status"] == "ready"


@pytest.mark.asyncio
async def test_approved_plugin_tool_requires_its_text_editor_before_session_claim() -> None:
    from agents import ToolExecutionConfig

    store = MemoryStore()
    session = await ResumableSession.create(store)
    effects: list[str] = []
    agent = reader(effects, [])
    selected = agent.plugins[0].tools[0]

    async def edit(_call, slots):
        assert slots == ("ok",)
        return ("edited",)

    cfg = replace(
        config(), tool_execution=ToolExecutionConfig(tool_text_transforms=[(selected, edit)])
    )
    paused = await Runner.run(
        agent, "read", session=session, context={"allowed": True}, run_config=cfg
    )
    state = paused.to_state()
    state.approve(state.get_interruptions()[0])
    with pytest.raises(UserError):
        await Runner.run(agent, state, run_config=config())
    assert session.is_paused and not effects
    await Runner.run(agent, state, run_config=cfg)
    assert effects == ["N42"]
    outputs = [
        item["output"]
        for item in store.checkpoint(session.session_id)["history"]
        if item.get("type") == "function_call_output"
    ]
    assert outputs == ["edited"]
