import asyncio
import json
from threading import Event

import pytest

from agents import Agent, RunConfig, Runner, UserError, function_tool
from agents.models.codex import CodexChildren, CodexProvider, CodexRole

from .test_codex_mutable_tool_hooks import codex_fixture, final_message

pytest_plugins = ["tests.models.test_codex_mutable_tool_hooks"]

ROLE = CodexRole("editor", "Edit the draft.", 'Return "edited".\nStay concise.')
PROFILE = CodexChildren(roles=(ROLE,), max_children=1)


@pytest.mark.parametrize("name", ["", " ", "default", "explorer", "worker", "a/b"])
def test_invalid_child_role(name):
    with pytest.raises(ValueError, match="name"):
        CodexRole(name, "description", "instructions")


def test_invalid_child_declarations():
    with pytest.raises(ValueError, match="nonblank"):
        CodexRole("editor", "description", " ")
    with pytest.raises(ValueError, match="Duplicate"):
        CodexChildren(roles=(ROLE, ROLE), max_children=1)
    for limit in [0, -1, True, 1.5, 2**31]:
        for field in ("max_depth", "max_children"):
            kwargs = {"max_children": 1, field: limit}
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
async def test_native_children_slots_and_depth(tmp_path, monkeypatch, model_endpoint, catalog):
    port, requests, items = model_endpoint
    provider = codex_fixture(tmp_path, port, monkeypatch, catalog, children=PROFILE)
    release = Event()

    def step(request):
        receipts = outputs(request)
        if "LEAF" in json.dumps([i for i in request["input"] if i.get("role") == "user"]):
            if not receipts:
                assert release.wait(10)
                return call("spawn_agent", "grand", message="GRAND")
            return final_message()
        if not receipts:
            return [
                call("spawn_agent", tag, message="LEAF", agent_type=ROLE.name)
                for tag in ("one", "two")
            ]
        if "wait" not in receipts:
            release.set()
            target = next(json.loads(v)["agent_id"] for v in receipts.values() if "agent_id" in v)
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
        assert any("limit" in parent[tag] for tag in ("one", "two"))
        assert "done" in parent["wait"]
    finally:
        release.set()
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
        children=PROFILE,
    )
    child_id = ""

    def step(request):
        nonlocal child_id
        if "LEAF" in json.dumps([i for i in request["input"] if i.get("role") == "user"]):
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
    children = CodexChildren(roles=(ROLE,), max_children=2, max_depth=2)
    provider = codex_fixture(tmp_path, port, monkeypatch, catalog, children=children)
    started, release = Event(), Event()
    child_id = ""

    def step(request):
        nonlocal child_id
        prompt = json.dumps([i for i in request["input"] if i.get("role") == "user"])
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

    items.extend([step] * 9)
    root = asyncio.create_task(ask(provider))
    try:
        assert await asyncio.to_thread(started.wait, 10)
        await ask(provider, prompt="FOREIGN")
        await root
        receipts = [outputs(r) for r in requests]
        assert any("not found" in r.get("foreign", "") for r in receipts)
        assert sum("done" in r.get("wait", "") for r in receipts) == 2
        assert len(requests) == 9
    finally:
        release.set()
        root.cancel()
        await asyncio.gather(root, return_exceptions=True)
        await provider.aclose()
