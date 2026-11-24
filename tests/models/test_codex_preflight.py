from pathlib import Path

import pytest

from agents import (
    Agent,
    ModelRequest,
    ModelSettings,
    ResumableSession,
    RunConfig,
    RunHooks,
    Runner,
    ToolExecutionConfig,
    UserError,
    WebSearchTool,
    function_tool,
)
from agents.models.codex import CodexProvider
from agents.testing import ScriptedModel
from tests.memory.test_resumable_session import MemoryStore

from .test_codex_pending_approval import ApprovalThread, raw
from .test_codex_provider import FakeThread, native_pair, threads as threads


@pytest.mark.parametrize(
    "model_request, message",
    [
        (
            ModelRequest(
                input=[{"role": "user", "content": [{"type": "input_file", "file_id": "file-pdf"}]}]
            ),
            "only text",
        ),
        (
            ModelRequest(input="start", model_settings=ModelSettings(tool_choice="editor")),
            "tool_choice",
        ),
        (ModelRequest(input="start", previous_response_id="response-private"), "state or prompt"),
        (ModelRequest(input="start", conversation_id="conversation-private"), "state or prompt"),
        (
            ModelRequest(
                input="start",
                tool_execution=ToolExecutionConfig(require_native_approval_recovery=True),
            ),
            "ResumableSession",
        ),
    ],
)
def test_codex_preflight_reports_known_incompatibility_without_native_startup(
    tmp_path, model_request, message
):
    provider = CodexProvider(cwd=tmp_path)
    report = provider.get_model("configured").preflight(model_request)
    assert report.adapter == "agents.models.codex._CodexModel"
    assert report.static_validation == "failed"
    with pytest.raises(UserError, match=message):
        report.raise_for_errors()
    assert provider._manager is None
    assert not list(tmp_path.iterdir())


def test_preflight_does_not_read_or_upload_local_images(tmp_path, monkeypatch):
    provider = CodexProvider(cwd=tmp_path)

    def unexpected_read(self):
        raise AssertionError("Preflight must not read media files.")

    monkeypatch.setattr(Path, "read_bytes", unexpected_read)
    report = provider.get_model(None).preflight(
        ModelRequest(
            input=[
                {
                    "role": "user",
                    "content": [{"type": "input_image", "image_url": str(tmp_path / "image.png")}],
                }
            ]
        )
    )
    assert report.static_validation == "passed"
    assert any("readability" in check for check in report.unchecked)
    assert provider._manager is None


def test_third_party_preflight_reports_unknown_and_actual_adapter():
    model = ScriptedModel([])
    report = model.preflight(ModelRequest(input="start"))
    assert report.static_validation == "unknown"
    assert report.adapter == f"{type(model).__module__}.{type(model).__qualname__}"
    assert report.unchecked
    report.raise_for_errors()


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_runner_rejects_required_native_recovery_before_startup(tmp_path, threads, streamed):
    started = []

    class Observer(RunHooks):
        async def on_llm_start(self, *args):
            started.append("request")

    provider = CodexProvider(cwd=tmp_path)
    config = RunConfig(
        model_provider=provider,
        tracing_disabled=True,
        tool_execution={"require_native_approval_recovery": True},
    )
    with pytest.raises(UserError, match="ResumableSession"):
        if streamed:
            run = Runner.run_streamed(
                Agent(name="worker"), "start", run_config=config, hooks=Observer()
            )
            async for _ in run.stream_events():
                pass
        else:
            await Runner.run(Agent(name="worker"), "start", run_config=config, hooks=Observer())
    assert provider._manager is None
    assert not threads[1] and not started


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("require_recovery", [False, True])
async def test_required_sdk_tool_with_resumable_session(
    tmp_path, threads, streamed, require_recovery
):
    started = []

    @function_tool
    def lookup() -> str:
        started.append("tool")
        return "found"

    class Observer(RunHooks):
        async def on_llm_start(self, *args):
            started.append("request")

    thread = FakeThread(call={"callId": "lookup", "tool": "lookup", "arguments": {}})
    threads[0].append(thread)
    provider = CodexProvider(cwd=tmp_path)
    execution = ToolExecutionConfig(require_native_approval_recovery=require_recovery)
    settings = ModelSettings(tool_choice="required")
    agent = Agent(
        name="worker",
        tools=[lookup],
        model_settings=settings,
        tool_use_behavior="stop_on_first_tool",
    )
    config = RunConfig(model_provider=provider, tool_execution=execution, tracing_disabled=True)
    session = await ResumableSession.create(MemoryStore())
    report = provider.get_model(None).preflight(
        ModelRequest(
            input="start",
            tools=[lookup],
            model_settings=settings,
            tool_execution=execution,
            resumable=True,
        )
    )

    async def run():
        if streamed:
            result = Runner.run_streamed(
                agent, "start", run_config=config, hooks=Observer(), session=session
            )
            async for _ in result.stream_events():
                pass
            return result
        return await Runner.run(
            agent, "start", run_config=config, hooks=Observer(), session=session
        )

    if require_recovery:
        with pytest.raises(UserError, match="tool_choice='required'"):
            await run()
        assert report.static_validation == "failed"
        assert provider._manager is None and not threads[1] and not started
    else:
        assert report.static_validation == "passed"
        assert (await run()).final_output == "found"
        assert started == ["request", "tool"]
        assert thread.recover_approvals is False


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_sdk_workspace_owns_write_without_preparation_hooks(tmp_path, threads, streamed):
    queue, _ = threads
    effects = []

    @function_tool
    def edit_file() -> str:
        (tmp_path / "written.txt").write_text("SDK-owned edit")
        effects.append("sdk")
        return "edited"

    first = FakeThread(call={"callId": "write", "tool": "edit_file", "arguments": {}})
    second = FakeThread(text="done")
    queue.extend([first, second])
    provider = CodexProvider(cwd=tmp_path)
    execution = ToolExecutionConfig(workspace_execution="sdk")
    model = provider.get_model("configured")
    report = model.preflight(
        ModelRequest(input="start", tools=[edit_file], tool_execution=execution)
    )
    assert report.static_validation == "passed"
    assert [(tool.name, tool.executor) for tool in report.tools] == [("edit_file", "sdk")]
    assert any("no usable execution environment" in check for check in report.unchecked)
    assert any("Native delegation may sample" in check for check in report.unchecked)
    native_report = model.preflight(ModelRequest(input="start", tools=[edit_file]))
    assert [(tool.name, tool.executor) for tool in native_report.tools] == [
        ("edit_file", "sdk"),
        ("Codex workspace tools", "native"),
    ]
    config = RunConfig(model_provider=provider, tool_execution=execution, tracing_disabled=True)
    agent = Agent(name="worker", model="configured", tools=[edit_file])
    if streamed:
        result = Runner.run_streamed(agent, "edit", run_config=config)
        async for _ in result.stream_events():
            pass
    else:
        result = await Runner.run(agent, "edit", run_config=config)
    assert result.final_output == "done"
    assert effects == ["sdk"]
    assert (tmp_path / "written.txt").read_text() == "SDK-owned edit"
    assert first.settings["sdk_sandbox"] is True and second.settings["sdk_sandbox"] is True
    assert first.closed and second.closed


@pytest.mark.asyncio
async def test_native_patch_recovery_accepts_resumable_session_and_policy(tmp_path, threads):
    call, _ = native_pair("custom_tool_call", "pending", "apply_patch")
    pending = ApprovalThread(
        events=[
            raw(call),
            {
                "type": "apply_patch_approval_request",
                "call_id": "pending",
                "turn_id": "turn_1",
                "changes": {},
            },
            {"type": "raw_response_completed"},
        ]
    )
    threads[0].append(pending)
    provider = CodexProvider(cwd=tmp_path)
    execution = ToolExecutionConfig(require_native_approval_recovery=True)
    report = provider.get_model(None).preflight(
        ModelRequest(
            input="edit",
            tool_execution=execution,
            resumable=True,
        )
    )
    assert report.static_validation == "passed"
    session = await ResumableSession.create(MemoryStore())
    result = await Runner.run(
        Agent(name="worker"),
        "edit",
        session=session,
        run_config=RunConfig(
            model_provider=provider,
            tool_execution=execution,
            tracing_disabled=True,
        ),
    )
    assert len(result.interruptions) == 1
    assert result.interruptions[0].raw_item.name == "apply_patch"
    assert pending.closed


@pytest.mark.parametrize(
    "overrides",
    [
        ['mcp_servers.native.command="worker"'],
        ['mcp_servers.native.command="worker"', "mcp_servers.native.enabled=false"],
    ],
)
def test_preflight_defers_effective_native_config_to_existing_runtime(tmp_path, overrides):
    provider = CodexProvider(cwd=tmp_path, config_overrides=overrides)
    report = provider.get_model(None).preflight(
        ModelRequest(
            input="start",
            tool_execution=ToolExecutionConfig(workspace_execution="sdk"),
        )
    )
    assert report.static_validation == "passed"
    assert any("host policy" in check for check in report.unchecked)
    assert provider._manager is None


def test_sdk_workspace_rejects_competing_native_web_tool(tmp_path):
    report = (
        CodexProvider(cwd=tmp_path)
        .get_model(None)
        .preflight(
            ModelRequest(
                input="start",
                tools=[WebSearchTool()],
                tool_execution=ToolExecutionConfig(workspace_execution="sdk"),
            )
        )
    )
    with pytest.raises(UserError, match="SDK sandbox tools"):
        report.raise_for_errors()


def test_native_recovery_cannot_be_required_with_sdk_workspace(tmp_path):
    report = (
        CodexProvider(cwd=tmp_path)
        .get_model(None)
        .preflight(
            ModelRequest(
                input="start",
                resumable=True,
                tool_execution=ToolExecutionConfig(
                    workspace_execution="sdk", require_native_approval_recovery=True
                ),
            )
        )
    )
    with pytest.raises(UserError, match="provider-owned"):
        report.raise_for_errors()
