import pytest
from httpx import ASGITransport, AsyncClient
from inline_snapshot import snapshot

from ..fake_model import FakeModel
from ..test_responses import get_text_message
from .streaming_app import agent, app


@pytest.mark.asyncio
async def test_streaming_context():
    """This ensures that FastAPI streaming works. The context for this test is that the Runner
    method was called in one async context, and the streaming was ended in another context,
    leading to a tracing error because the context was closed in the wrong context. This test
    ensures that this actually works.
    """
    model = FakeModel()
    agent.model = model
    model.set_next_output([get_text_message("done")])

    transport = ASGITransport(app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        async with ac.stream("POST", "/stream") as r:
            assert r.status_code == 200
            body = (await r.aread()).decode("utf-8")
            lines = [line for line in body.splitlines() if line]
            assert lines == snapshot(
                ["agent_updated_stream_event", "raw_response_event", "run_item_stream_event"]
            )
