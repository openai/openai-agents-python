"""Provider-hosted images retain their managed native child's ownership."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Agent, FileSearch, ImageGeneration, NativeAgents, ToolRef
from agents.observation import HostedToolCallProgress, ModelOutputItemCompleted, TurnFinished
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output
from tests.test_native_child_agents import ChildPeer, handles, metadata, trust
from tests.test_native_executor_tools import declaration

PREVIEW_BYTES = bytes(range(256)) * 512
PARTIAL = {
    "type": "response.image_generation_call.partial_image",
    "item_id": "ig_child",
    "output_index": 0,
    "sequence_number": 1,
    "partial_image_index": 0,
    "partial_image_b64": base64.b64encode(PREVIEW_BYTES).decode("ascii"),
    "future_metadata": {"retained": True},
}
FINAL_IMAGE = {
    "type": "image_generation_call",
    "id": "ig_child",
    "status": "completed",
    "result": "ZmluYWwtY2hpbGQtaW1hZ2U=",
    "revised_prompt": "A blue square from the child.",
}


class HostedChildPeer(ChildPeer):
    def response(self) -> list[dict[str, Any]]:
        events = super().response()
        if metadata(self.requests[-1], self.request_headers[-1])["agent_name"] == "/root":
            return events
        finished = assistant_output("Child image complete.")
        finished["item"]["id"] = "done-alpha"
        return [
            PARTIAL,
            {"type": "response.output_item.done", "output_index": 0, "item": FINAL_IMAGE},
            finished,
        ]


async def test_managed_child_inherits_hosted_image_grant_and_retains_large_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = HostedChildPeer(("default",))
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    image = ImageGeneration(partial_images=1, quality="low", output_format="png")
    selected_image = ToolRef("image_generation", "functions")
    children = NativeAgents(
        max_children=1,
        before_step=lambda step: [selected_image],
    )
    async with (
        timeout(30),
        TestServer(app, shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        async with (
            peer.provider(websocket=False) as provider,
            Agent(
                instructions="Delegate image generation to a native child.",
                model="gpt-5.5",
                provider=provider,
                tools=[children, image, FileSearch(vector_store_ids=["vs_unselected"])],
            ).session(environment=declaration(executor)) as session,
        ):
            try:
                root = await session.submit("Draw a square.", tools=[children, selected_image])
                await peer.entered["alpha"].wait()
                child = (await handles(session, 1))[0]
                assert child.turn_id != root.turn_id
                assert child.submission_id != root.submission_id
                assert child.child_id == peer.child_metadata["alpha"][0]["thread_id"]
                assert (await root.get_result()).final_output == "root complete"
                peer.release["alpha"].set()
                result = await child.get_result()
                assert result.turn is child
                assert result.final_output == "Child image complete."

                events = [event async for event in child.events(include_execution_details=True)]
                progress = [e for e in events if isinstance(e, HostedToolCallProgress)]
                assert len(progress) == 1
                preview = progress[0]
                assert preview.item_id == "ig_child"
                assert preview.event == PARTIAL
                encoded = preview.event["partial_image_b64"]
                assert isinstance(encoded, str)
                assert len(PREVIEW_BYTES) >= 100 * 1024
                assert base64.b64decode(encoded, validate=True) == PREVIEW_BYTES
                completed = [
                    e
                    for e in events
                    if isinstance(e, ModelOutputItemCompleted)
                    and e.item["type"] == "image_generation_call"
                ]
                assert len(completed) == 1
                for event in [preview, *completed]:
                    assert event.session_id == session.id
                    assert event.turn_id == child.turn_id
                    assert event.submission_id == child.submission_id
                assert [e.status for e in events if isinstance(e, TurnFinished)] == ["completed"]
                images = [
                    item
                    async for item in session.items(subagent_id=child.child_id)
                    if item["type"] == "image_generation_call"
                ]
                assert len(images) == 1
                assert images[0]["turn_id"] == child.turn_id
                assert images[0]["result"] == FINAL_IMAGE["result"]
                assert images[0]["provider_data"] == FINAL_IMAGE
                assert completed[0].item == images[0]
                assert not [e async for e in root.events() if isinstance(e, HostedToolCallProgress)]
                assert len(peer.child_requests["alpha"]) == 1
                expected = {
                    "type": "image_generation",
                    "partial_images": 1,
                    "quality": "low",
                    "output_format": "png",
                }
                for request in [peer.requests[0], peer.child_requests["alpha"][0]]:
                    hosted = [
                        tool
                        for tool in request["tools"]
                        if tool["type"] in {"image_generation", "file_search"}
                    ]
                    assert hosted == [expected]
            finally:
                peer.release["alpha"].set()
