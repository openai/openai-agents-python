import asyncio
import json
import traceback
from threading import Barrier, Event

import pytest

from agents import (
    Agent,
    AgentsException,
    ModelRetrySettings,
    ModelSettings,
    RunConfig,
    Runner,
    UserError,
    function_tool,
)
from agents.models.codex import CodexChildLimitError, CodexChildren, CodexProvider, CodexRole
from agents.models.interface import ModelTracing

from .test_codex_mutable_tool_hooks import codex_fixture, final_message
from .test_codex_pending_approval import ApprovalThread, approval, raw, response
from .test_codex_provider import native_pair, threads as threads

pytest_plugins = ["tests.models.test_codex_mutable_tool_hooks"]

ROLE = CodexRole("editor", "Edit the draft.", 'Return "edited".\nStay concise.')
PROFILE = CodexChildren(roles=(ROLE,), max_delegation_attempts=1)


@pytest.mark.parametrize("name", ["", " ", "default", "explorer", "worker", "a/b"])
def test_invalid_child_role(name):
    with pytest.raises(ValueError, match="name"):
        CodexRole(name, "description", "instructions")


@pytest.mark.parametrize("roles", [[], (), "editor", [None], [ROLE, ROLE]])
def test_invalid_roles(roles):
    with pytest.raises(ValueError):
        CodexChildren(roles=roles, max_delegation_attempts=1)


def test_invalid_child_declarations():
    with pytest.raises(ValueError, match="nonblank"):
        CodexRole("editor", "description", " ")
    with pytest.raises(ValueError, match="Duplicate"):
        CodexChildren(roles=(ROLE, ROLE), max_delegation_attempts=1)
    for limit in [0, -1, True, 1.5, 2**31]:
        for field in ("max_depth", "max_delegation_attempts"):
            kwargs = {"max_delegation_attempts": 1, field: limit}
            with pytest.raises(ValueError, match="limits"):
                CodexChildren(roles=(ROLE,), **kwargs)


@pytest.mark.asyncio
@pytest.mark.allow_call_model_methods
async def test_child_incompatible_sdk_tool_rejected_without_native_start():
    @function_tool
    def local() -> str:
        raise AssertionError("No tool invocation expected")

    provider = CodexProvider(children=PROFILE)
    try:
        with pytest.raises(UserError, match="Native children"):
            await Runner.run(
                Agent(name="manager", model=provider.get_model(None), tools=[local]),
                "draft",
                run_config=RunConfig(tracing_disabled=True),
            )
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_native_children_reject_saved_workspace_approval_before_start(tmp_path, threads):
    call, _ = native_pair(call_id="pending")
    threads[0].append(
        ApprovalThread(events=[raw(call), approval(), {"type": "raw_response_completed"}])
    )
    checkpoint = (await response(tmp_path)).output[0].checkpoint
    provider = CodexProvider(cwd=tmp_path, children=PROFILE)
    with pytest.raises(UserError, match="cannot resume workspace approvals"):
        await provider._run_approved_native_call(checkpoint, ModelTracing.DISABLED, hooks=None)
    assert provider._manager is None


@pytest.mark.asyncio
async def test_native_children_reject_unexpected_workspace_approval(tmp_path, threads):
    thread = ApprovalThread(events=[approval()])
    threads[0].append(thread)
    provider = CodexProvider(cwd=tmp_path, children=PROFILE)
    provider._manager = CodexProvider(cwd=tmp_path)._native_manager()
    try:
        with pytest.raises(UserError, match="native workspace tools enabled"):
            await Runner.run(
                Agent(name="manager", model=provider.get_model(None)),
                "draft",
                run_config=RunConfig(tracing_disabled=True),
            )
        assert thread.closed
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_provider_text_cannot_signal_a_child_limit(tmp_path, threads):
    thread = ApprovalThread(
        events=[{"type": "error", "message": "Codex native delegation limit exceeded."}]
    )
    thread.delegation_error = None
    threads[0].append(thread)
    provider = CodexProvider(cwd=tmp_path, children=PROFILE)
    provider._manager = CodexProvider(cwd=tmp_path)._native_manager()
    try:
        with pytest.raises(RuntimeError, match="Codex model turn failed") as caught:
            await ask(provider)
        assert not isinstance(caught.value, CodexChildLimitError)
        assert thread.closed
    finally:
        await provider.aclose()


@pytest.fixture
def catalog(tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "slug": version,
                        "display_name": version,
                        "base_instructions": "Answer briefly.",
                        "supported_reasoning_levels": [],
                        "shell_type": "unified_exec",
                        "visibility": "list",
                        "supported_in_api": True,
                        "priority": 1,
                        "support_verbosity": False,
                        "truncation_policy": {"mode": "tokens", "limit": 10000},
                        "experimental_supported_tools": [],
                        "multi_agent_version": version,
                        "tool_mode": "code_mode_only",
                        "supports_search_tool": True,
                        "apply_patch_tool_type": "freeform",
                    }
                    for version in ("v1", "v2")
                ]
            }
        )
    )
    return [
        f"model_catalog_json={json.dumps(str(path))}",
        'sandbox_mode="read-only"',
        'approval_policy="never"',
    ]


async def ask(provider, model="v1", prompt="draft"):
    result = await asyncio.wait_for(
        Runner.run(
            Agent(name="manager", model=provider.get_model(model)),
            prompt,
            run_config=RunConfig(tracing_disabled=True),
        ),
        30,
    )
    assert result.final_output == "done"


def call(name, tag, **args):
    return {
        "type": "function_call",
        "namespace": "multi_agent_v1",
        "name": name,
        "call_id": tag,
        "arguments": json.dumps(args),
    }


def user_input(request):
    return json.dumps([i for i in request["input"] if i.get("role") == "user"])


def outputs(request):
    return {
        i["call_id"]: i["output"]
        for i in request["input"]
        if i.get("type") == "function_call_output"
    }


@pytest.mark.asyncio
@pytest.mark.allow_call_model_methods
@pytest.mark.parametrize(
    "model,extra",
    [
        ("v2", []),
        ("unknown", []),
        ("v1", ["agents.max_depth=2"]),
        ("v1", ["features.shell_tool=true"]),
        ("v1", ["features.multi_agent_v2=true"]),
    ],
)
async def test_unsupported_children_before_generation(
    tmp_path, monkeypatch, model_endpoint, catalog, model, extra
):
    port, requests, items = model_endpoint
    items.append(final_message())
    provider = codex_fixture(tmp_path, port, monkeypatch, catalog + extra, children=PROFILE)
    try:
        with pytest.raises(RuntimeError, match="V1|conflict"):
            await ask(provider, model)
        assert not requests
    finally:
        await provider.aclose()


@pytest.mark.asyncio
@pytest.mark.allow_call_model_methods
async def test_native_children_at_depth_limit(tmp_path, monkeypatch, model_endpoint, catalog):
    port, requests, items = model_endpoint
    roles = [ROLE]
    children = CodexChildren(roles=roles, max_delegation_attempts=2)
    roles.clear()
    assert children.roles == (ROLE,)
    provider = codex_fixture(tmp_path, port, monkeypatch, catalog, children=children)

    def step(request):
        receipts = outputs(request)
        if "LEAF" in user_input(request):
            if not receipts:
                return call("spawn_agent", "grand", message="GRAND")
            return final_message()
        if not receipts:
            return call("spawn_agent", "spawn", message="LEAF", agent_type=ROLE.name)
        if "wait" not in receipts:
            target = json.loads(receipts["spawn"])["agent_id"]
            return call("wait_agent", "wait", targets=[target], timeout_ms=10000)
        return final_message()

    items.extend([step] * 5)
    try:
        await ask(provider)
        assert len(requests) == 5
        assert ROLE.description in json.dumps(requests[0])
        leaf = next(r for r in requests if "grand" in outputs(r))
        assert json.dumps(ROLE.instructions)[1:-1] in json.dumps(leaf)
        assert not leaf.get("tools")
        assert all(i.get("type") != "additional_tools" for i in leaf["input"])
        assert "unsupported" in outputs(leaf)["grand"].lower()
        parent = outputs(requests[-1])
        assert "done" in parent["wait"]
    finally:
        await provider.aclose()


@pytest.mark.asyncio
@pytest.mark.allow_call_model_methods
async def test_child_native_fork_reopen_and_model_selection(
    tmp_path, monkeypatch, model_endpoint, catalog
):
    port, requests, items = model_endpoint
    provider = codex_fixture(
        tmp_path,
        port,
        monkeypatch,
        [*catalog, 'agents.ambient.description="ambient specialist"'],
        children=CodexChildren(roles=(ROLE,), max_delegation_attempts=5),
    )
    child_id = ""

    def step(request):
        nonlocal child_id
        if "LEAF" in user_input(request):
            return final_message()
        receipts = outputs(request)
        if not receipts:
            return [
                call("spawn_agent", "switch", message="LEAF", model="v2"),
                call("spawn_agent", "ambient", message="LEAF", agent_type="ambient"),
            ]
        if "fork" not in receipts:
            return call("spawn_agent", "fork", message="LEAF", fork_context=True)
        if "spawn" not in receipts:
            return call("spawn_agent", "spawn", message="LEAF", agent_type=ROLE.name)
        if "wait" not in receipts:
            child_id = json.loads(receipts["spawn"])["agent_id"]
            return call("wait_agent", "wait", targets=[child_id], timeout_ms=10000)
        if "close" not in receipts:
            return call("close_agent", "close", target=child_id)
        if "reopen" not in receipts:
            return call("resume_agent", "reopen", id=child_id)
        return final_message()

    items.extend([step] * 8)
    try:
        await ask(provider)
        parent = outputs(requests[-1])
        assert "not found" in parent["reopen"]
        assert "ambient specialist" not in json.dumps(requests[0])
        assert "unknown" in parent["ambient"].lower()
        assert "Unknown model" in parent["switch"]
        assert "no thread" in parent["fork"]
        assert "done" in parent["wait"]
        assert len(requests) == 8
        items.extend([call("resume_agent", "foreign", id=child_id), final_message()])
        await ask(provider)
        assert "not found" in outputs(requests[-1])["foreign"]
        assert len(requests) == 10
    finally:
        await provider.aclose()


@pytest.mark.asyncio
@pytest.mark.allow_call_model_methods
async def test_child_native_depth_two_and_live_tree_isolation(
    tmp_path, monkeypatch, model_endpoint, catalog
):
    port, requests, items = model_endpoint
    children = CodexChildren(roles=(ROLE,), max_delegation_attempts=2, max_depth=2)
    provider = codex_fixture(tmp_path, port, monkeypatch, catalog, children=children)
    started, release = Event(), Event()
    child_id = ""

    def step(request):
        nonlocal child_id
        prompt = user_input(request)
        receipts = outputs(request)
        if "FOREIGN" in prompt:
            if not receipts:
                return call("resume_agent", "foreign", id=child_id)
            release.set()
            return final_message()
        if "GRAND" in prompt:
            assert release.wait(10)
            return final_message()
        if not receipts:
            return call(
                "spawn_agent",
                "spawn",
                agent_type=ROLE.name,
                message="GRAND" if "LEAF" in prompt else "LEAF",
            )
        if "wait" not in receipts:
            target = json.loads(receipts["spawn"])["agent_id"]
            if "LEAF" in prompt:
                child_id = target
                started.set()
            return call("wait_agent", "wait", targets=[target], timeout_ms=20000)
        return final_message()

    # A late completion notification can prompt one more reply per child after a wait.
    items.extend([step] * 11)
    root = asyncio.create_task(ask(provider))
    try:
        assert await asyncio.to_thread(started.wait, 10)
        await ask(provider, prompt="FOREIGN")
        await root
        assert any("not found" in outputs(r).get("foreign", "") for r in requests)
        waits = {}
        notified = set()
        for request in requests:
            receipts = outputs(request)
            if "wait" not in receipts:
                continue
            target = json.loads(receipts["spawn"])["agent_id"]
            completed = {"completed": "done"}
            assert json.loads(receipts["wait"]) == {
                "timed_out": False,
                "status": {target: completed},
            }
            history = request["input"]
            if target not in waits:
                waits[target] = history
                continue

            assert target not in notified and history[:-2] == waits[target]
            answer, notice = history[-2:]
            assert answer["type"] == notice["type"] == "message"
            assert answer["role"] == "assistant" and answer["content"] == final_message()["content"]
            assert notice["role"] == "user"
            (part,) = notice["content"]
            start, payload, end = part["text"].splitlines()
            assert (start, end) == ("<subagent_notification>", "</subagent_notification>")
            assert json.loads(payload) == {"agent_path": target, "status": completed}
            notified.add(target)
        assert len(waits) == 2
        assert len(requests) == 9 + len(notified)
    finally:
        release.set()
        root.cancel()
        await asyncio.gather(root, return_exceptions=True)
        await provider.aclose()


@pytest.mark.asyncio
@pytest.mark.allow_call_model_methods
async def test_child_close_does_not_refund(tmp_path, monkeypatch, model_endpoint, catalog):
    port, requests, items = model_endpoint
    provider = codex_fixture(tmp_path, port, monkeypatch, catalog, children=PROFILE)

    def step(request):
        prompt = user_input(request)
        if "LEAF" in prompt:
            return final_message()
        receipts = outputs(request)
        if not receipts:
            return call("spawn_agent", "spawn", message="LEAF")
        target = json.loads(receipts["spawn"])["agent_id"]
        if "wait" not in receipts:
            return call("wait_agent", "wait", targets=[target], timeout_ms=10000)
        if "close" not in receipts:
            return call("close_agent", "close", target=target)
        if "fresh" in prompt:
            return final_message()
        return call("spawn_agent", "spawn", message="REPLACEMENT")

    items.extend([step] * 10)
    try:
        with pytest.raises(CodexChildLimitError) as caught:
            await ask(provider)
        assert caught.value.limit == "delegation_attempts"
        assert len(requests) == 5
        await ask(provider, prompt="fresh")
        assert len(requests) == 10
        assert sum("LEAF" in user_input(r) for r in requests) == 2
        assert all("REPLACEMENT" not in user_input(r) for r in requests)
        for i in (4, 9):
            assert "done" in outputs(requests[i])["wait"]
            assert "done" in outputs(requests[i])["close"]
    finally:
        await provider.aclose()


@pytest.mark.asyncio
@pytest.mark.allow_call_model_methods
@pytest.mark.parametrize(
    "attempt",
    [
        call("resume_agent", "first", id="missing"),
        call("send_input", "first", target="missing", message="continue"),
        {**call("spawn_agent", "first"), "arguments": "{"},
        {**call("wait_agent", "first"), "namespace": "functions"},
    ],
)
async def test_child_failed_calls_consume_budget(
    tmp_path, monkeypatch, model_endpoint, catalog, attempt
):
    port, requests, items = model_endpoint
    provider = codex_fixture(tmp_path, port, monkeypatch, catalog, children=PROFILE)
    items.extend([attempt, call("spawn_agent", "next", message="LEAF"), final_message()])
    try:
        with pytest.raises(CodexChildLimitError) as caught:
            await ask(provider)
        assert caught.value.limit == "delegation_attempts"
        assert len(requests) == 2
        assert "LEAF" not in user_input(requests[-1])
    finally:
        await provider.aclose()


@pytest.mark.asyncio
@pytest.mark.allow_call_model_methods
async def test_children_share_last_delegation(tmp_path, monkeypatch, model_endpoint, catalog):
    port, requests, items = model_endpoint
    children = CodexChildren(roles=(ROLE,), max_delegation_attempts=3, max_depth=2)
    provider = codex_fixture(tmp_path, port, monkeypatch, catalog, children=children)
    siblings = Barrier(2)

    def step(request):
        prompt = user_input(request)
        receipts = outputs(request)
        if "GRAND" in prompt:
            return final_message()
        if "LEAF" in prompt:
            if not receipts:
                siblings.wait(timeout=10)
                return call("spawn_agent", "grand", message="GRAND")
            names = ["grand"]
        else:
            names = ["one", "two"]
            if not receipts:
                return [call("spawn_agent", tag, message="LEAF") for tag in names]
        for name in names:
            if "wait" + name not in receipts:
                target = json.loads(receipts[name])["agent_id"]
                return call("wait_agent", "wait" + name, targets=[target], timeout_ms=10000)
        return final_message()

    items.extend([step] * 9)
    try:
        with pytest.raises(CodexChildLimitError) as caught:
            await ask(provider)
        assert caught.value.limit == "delegation_attempts"
        waits = [outputs(requests[-1])["wait" + tag] for tag in ("one", "two")]
        assert sum("delegation limit exceeded" in result for result in waits) == 1
        assert sum("done" in result for result in waits) == 1
        assert sum("GRAND" in user_input(r) for r in requests) == 1
        assert len(requests) == 9
    finally:
        siblings.abort()
        await provider.aclose()


@pytest.mark.asyncio
@pytest.mark.allow_call_model_methods
async def test_child_rejects_custom_payload(tmp_path, monkeypatch, model_endpoint, catalog):
    port, requests, items = model_endpoint
    provider = codex_fixture(tmp_path, port, monkeypatch, catalog, children=PROFILE)
    items.append(
        {
            "type": "custom_tool_call",
            "namespace": "multi_agent_v1",
            "name": "close_agent",
            "call_id": "first",
            "input": "{}",
        }
    )
    try:
        with pytest.raises(RuntimeError, match="Unsupported native delegation") as caught:
            await ask(provider)
        assert not isinstance(caught.value, CodexChildLimitError)
        assert len(requests) == 1
    finally:
        await provider.aclose()


@pytest.mark.asyncio
@pytest.mark.allow_call_model_methods
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("limit", ["delegation_attempts", "wait_close_attempts"])
async def test_child_counts_each_item_in_one_response(
    tmp_path, monkeypatch, model_endpoint, catalog, streamed, limit
):
    port, requests, items = model_endpoint
    provider = codex_fixture(tmp_path, port, monkeypatch, catalog, children=PROFILE)
    if limit == "delegation_attempts":
        items.append(
            [
                call("send_input", "same", message="missing target"),
                call("spawn_agent", "same", message="LEAF"),
            ]
        )
    else:
        items.append([call("wait_agent", str(i)) for i in range(3)])
    items.append(final_message())

    def unexpected_retry(context):
        pytest.fail("A limit refusal must not reach the retry policy.")

    try:
        with pytest.raises(AgentsException) as caught:
            agent = Agent(
                name="manager",
                model=provider.get_model("v1"),
                model_settings=ModelSettings(
                    retry=ModelRetrySettings(max_retries=1, policy=unexpected_retry)
                ),
            )
            config = RunConfig(
                tracing_disabled=True, redact_text=lambda s: s.replace("private", "*")
            )
            if streamed:
                run = Runner.run_streamed(agent, "private draft", run_config=config)
                async for _ in run.stream_events():
                    pass
            else:
                await Runner.run(agent, "private draft", run_config=config)
        assert isinstance(caught.value, CodexChildLimitError)
        assert caught.value.run_data is None
        assert caught.value.limit == limit
        assert "private draft" not in "".join(
            traceback.format_exception_only(type(caught.value), caught.value)
        )
        assert len(requests) == 1
    finally:
        await provider.aclose()


@pytest.mark.asyncio
@pytest.mark.allow_call_model_methods
async def test_native_wait_and_close_share_a_finite_allowance(
    tmp_path, monkeypatch, model_endpoint, catalog
):
    port, requests, items = model_endpoint
    provider = codex_fixture(tmp_path, port, monkeypatch, catalog, children=PROFILE)
    # Even without a child, failed calls can each trigger another provider request.
    attempts = [call("wait_agent", "wait"), call("close_agent", "close")]
    try:
        items.extend([*attempts, call("wait_agent", "again"), final_message()])
        with pytest.raises(CodexChildLimitError) as caught:
            await ask(provider)
        assert caught.value.limit == "wait_close_attempts"
        assert len(requests) == 3
        items.clear()
        items.extend([*attempts, final_message()])
        await ask(provider)
        assert len(requests) == 6
    finally:
        await provider.aclose()


@pytest.mark.asyncio
@pytest.mark.allow_call_model_methods
async def test_native_child_uses_the_same_wait_and_close_allowance(
    tmp_path, monkeypatch, model_endpoint, catalog
):
    port, requests, items = model_endpoint
    children = CodexChildren(roles=(ROLE,), max_delegation_attempts=1, max_depth=2)
    provider = codex_fixture(tmp_path, port, monkeypatch, catalog, children=children)

    def step(request):
        receipts = outputs(request)
        if "LEAF" in user_input(request):
            return final_message() if receipts else call("wait_agent", "empty")
        if not receipts:
            return call("spawn_agent", "spawn", message="LEAF")
        target = json.loads(receipts["spawn"])["agent_id"]
        if "wait" not in receipts:
            return call("wait_agent", "wait", targets=[target], timeout_ms=10000)
        return call("close_agent", "close", target=target)

    items.extend([step] * 5 + [final_message()])
    try:
        with pytest.raises(CodexChildLimitError) as caught:
            await ask(provider)
        assert caught.value.limit == "wait_close_attempts"
        assert len(requests) == 5
        assert "done" in outputs(requests[-1])["wait"]
    finally:
        await provider.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_bounded_override_leaves_saved_approval_paused(tmp_path, monkeypatch, streamed):
    from agents.memory.resumable_session import ResumableSession
    from tests.memory.test_native_approval_recovery import setup_native

    agent, store, session, config, pending, calls, _ = await setup_native(tmp_path, monkeypatch)
    first = await Runner.run(agent, "draft", session=session, run_config=config)
    saved = store.records[session.session_id]
    state = first.to_state()
    state.approve(first.interruptions[0])
    provider = CodexProvider(cwd=tmp_path, children=PROFILE)
    bounded = RunConfig(model=provider.get_model(None), tracing_disabled=True)
    with pytest.raises(UserError, match="cannot resume workspace approvals"):
        if streamed:
            stream = Runner.run_streamed(agent, state, run_config=bounded)
            async for _ in stream.stream_events():
                pass
        else:
            await Runner.run(agent, state, run_config=bounded)
    assert provider._manager is None and not calls
    assert store.records[session.session_id] == saved
    recovered = await ResumableSession.resume(store, session.session_id)
    state = await recovered.load_state(agent)
    state.approve(state.get_interruptions()[0])
    result = await Runner.run(agent, state, run_config=config)
    assert result.final_output == "finished"
    assert calls == [pending.checkpoint]
