import asyncio

import pytest

from agents import Agent, RunInbox, Runner

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
