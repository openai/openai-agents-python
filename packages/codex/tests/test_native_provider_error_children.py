"""Captured native children retain their own provider error information."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import NativeError
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.test_native_child_agents import ChildPeer, agent_for, handles, metadata, trust
from tests.test_native_executor_tools import declaration


class RejectedChildren(ChildPeer):
    def __init__(self) -> None:
        super().__init__(("default", "default"))
        self.rejected = {name: asyncio.Event() for name in ("alpha", "beta")}
        self.release_errors = asyncio.Event()

    async def handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        headers = {key.lower(): value for key, value in request.headers.items()}
        name = metadata(body, headers)["agent_name"].rsplit("/", 1)[-1]
        if name in self.rejected:
            self.rejected[name].set()
            await self.release_errors.wait()
            return web.json_response(
                {"error": {"message": "Synthetic rejection", "code": f"rejected_{name}"}},
                status=401 if name == "alpha" else 403,
                headers={"Retry-After": "0", "x-request-id": f"synthetic-{name}-request"},
            )
        return await super().handle(request)


async def test_captured_children_keep_provider_errors_separate_after_session_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = RejectedChildren()
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    async with (
        timeout(30),
        TestServer(app) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        peer.url = str(server.make_url("/v1"))
        agent = agent_for(peer, executor)
        provider = agent.provider
        assert provider is not None
        try:
            async with agent.session(environment=declaration(executor)) as session:
                root = await session.submit("Use alpha and beta.")
                await asyncio.gather(*(event.wait() for event in peer.rejected.values()))
                assert (await root.get_result()).final_output == "root complete"
                children = await handles(session, 2)
                peer.release_errors.set()
                failures = await asyncio.gather(
                    *(child.get_result() for child in children), return_exceptions=True
                )
                errors = []
                for failure in failures:
                    assert isinstance(failure, NativeError)
                    errors.append(failure)
                    assert str(failure) == "Native child turn failed"
                    assert failure.category == "authentication_error"
                    assert failure.code is not None
                    assert failure.code in {"rejected_alpha", "rejected_beta"}
                    name = failure.code.removeprefix("rejected_")
                    assert failure.status_code == (401 if name == "alpha" else 403)
                    assert failure.request_id == f"synthetic-{name}-request"
                assert {failure.code for failure in errors} == {
                    "rejected_alpha",
                    "rejected_beta",
                }
            for child, original in zip(children, errors, strict=True):
                with pytest.raises(NativeError) as retained:
                    await child.get_result()
                assert retained.value.args == original.args
                assert vars(retained.value) == vars(original)
        finally:
            peer.release_errors.set()
            await provider.aclose()
