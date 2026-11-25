import asyncio

import pytest
from openai import AsyncOpenAI

from agents import (
    Agent,
    RunConfig,
    RunInbox,
    Runner,
    ToolGuardrailFunctionOutput,
    function_tool,
    tool_input_guardrail,
)
from agents.models.openai_provider import OpenAIProvider

from .test_codex_mutable_tool_hooks import (
    codex_fixture,
    final_message,
    model_endpoint as model_endpoint,
)
from .test_codex_provider import FakeThread, config, threads as threads


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True], ids=["run", "streamed"])
@pytest.mark.parametrize("follow_ups", [False, True], ids=["steer", "steer-and-queue"])
async def test_codex_inbox_during_final_answer(tmp_path, threads, streamed, follow_ups):
    # Only the native thread is scripted. Capture the inputs and replay history Codex receives.
    release = asyncio.Event()
    first = FakeThread(text="A draft.", gate=release)
    requests = [(first, "Write a draft."), (FakeThread(text="Shorter draft."), "Shorten it.")]
    if follow_ups:
        requests.extend(
            [
                (FakeThread(text="Title."), "Give it a title."),
                (FakeThread(text="Summary."), "Summarize it."),
            ]
        )
    threads[0].extend(thread for thread, _ in requests)
    agent = Agent(name="editor")
    run_config = config(tmp_path)

    async def send_during_request(inbox):
        try:
            await asyncio.wait_for(first.started.wait(), 5)
            # Submit queued tasks first so arrival order alone cannot satisfy the test.
            queued = [inbox.queue(prompt) for _, prompt in requests[2:]]
            receipts = [inbox.steer("Shorten it."), *queued]
            assert all(receipt.status == "queued" for receipt in receipts)
            return receipts
        finally:
            release.set()

    if streamed:
        stream = Runner.run_streamed(agent, "Write a draft.", run_config=run_config)
        sender = asyncio.create_task(send_during_request(stream))
    else:
        inbox = RunInbox()
        sender = asyncio.create_task(send_during_request(inbox))
    try:
        if streamed:
            async for _ in stream.stream_events():
                pass
            result = stream
        else:
            result = await Runner.run(agent, "Write a draft.", inbox=inbox, run_config=run_config)
        receipts = await sender
        outcomes = await asyncio.wait_for(asyncio.gather(*(r.wait() for r in receipts)), 5)
        assert [(outcome.status, outcome.turn_number) for outcome in outcomes] == [
            ("submitted", turn) for turn in range(2, len(requests) + 1)
        ]
        assert result.final_output == requests[-1][0].text
        history = []
        for thread, prompt in requests:
            assert thread.inputs == [{"type": "text", "text": prompt, "text_elements": []}]
            assert [
                (item["role"], item["content"][0]["text"]) for item in thread.history
            ] == history
            history.extend([("user", prompt), ("assistant", thread.text)])
            assert thread.closed and not thread.interrupted
        assert not threads[0]
    finally:
        sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)
        await run_config.model_provider.aclose()


@pytest.mark.asyncio
@pytest.mark.allow_call_model_methods
@pytest.mark.parametrize("harness", ["responses", "codex"])
@pytest.mark.parametrize("streamed", [False, True], ids=["run", "streamed"])
async def test_mid_tool_steer_reaches_next_tool_guardrail(
    tmp_path, monkeypatch, model_endpoint, harness, streamed
):
    # Script the wire so we can compare what the model receives with what the guardrail sees.
    port, requests, replies = model_endpoint
    for name in ("prepare_draft", "publish"):
        replies.append({"type": "function_call", "name": name, "call_id": name, "arguments": "{}"})
    replies.append(final_message())
    original = "Prepare and publish the draft."
    restriction = "Do not publish the draft."
    inbox = RunInbox()
    checked = []
    effects = []

    @function_tool
    async def prepare_draft() -> str:
        """Prepare a draft before publication."""
        inbox.steer(restriction)
        return "Draft ready."

    @tool_input_guardrail
    def check_permission(data):
        messages = [
            item["content"] for item in data.context.turn_input if item.get("role") == "user"
        ]
        checked.append(messages)
        if restriction in messages:
            return ToolGuardrailFunctionOutput.reject_content("Publication blocked.")
        return ToolGuardrailFunctionOutput.allow()

    @function_tool(tool_input_guardrails=[check_permission])
    async def publish() -> str:
        """Publish the draft."""
        effects.append("published")
        return "Published."

    client = None
    if harness == "codex":
        provider = codex_fixture(tmp_path, port, monkeypatch)
    else:
        client = AsyncOpenAI(
            base_url=f"http://127.0.0.1:{port}/v1",
            api_key="synthetic-fixture-key",
        )
        provider = OpenAIProvider(openai_client=client, use_responses=True)
    agent = Agent(name="editor", model="gpt-5.4", tools=[prepare_draft, publish])
    run_config = RunConfig(model_provider=provider, tracing_disabled=True)
    try:
        if streamed:
            stream = Runner.run_streamed(agent, original, inbox=inbox, run_config=run_config)
            async for _ in stream.stream_events():
                pass
            final_output = stream.final_output
        else:
            result = await Runner.run(agent, original, inbox=inbox, run_config=run_config)
            final_output = result.final_output
        assert final_output == "done"
        steer = requests[1]["input"][-1]
        assert steer["role"] == "user"
        content = steer["content"]
        assert (content if isinstance(content, str) else content[0]["text"]) == restriction
        assert (checked, effects) == ([[original, restriction]], [])
        assert any(
            item.get("type") == "function_call_output"
            and item["call_id"] == "publish"
            and item["output"] == "Publication blocked."
            for item in requests[2]["input"]
        )
    finally:
        await provider.aclose()
        if client is not None:
            await client.close()
