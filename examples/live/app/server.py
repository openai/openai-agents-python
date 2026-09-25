"""Local WebRTC microphone demo with a server-owned Live sideband."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from openai import APIStatusError, AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field

from agents import Agent

from .agent import create_order_agent, session_config
from .delegation import DelegationHandler

app = FastAPI()
static = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static), name="static")
logger = logging.getLogger(__name__)
CLOSE_TIMEOUT = 15
LOCAL_ORIGINS = {"http://127.0.0.1:8000", "http://localhost:8000"}


class Offer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sdp: str = Field(min_length=1, max_length=65536)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(static / "index.html")


async def relay(
    connection: Any, browser: WebSocket, agent: Agent, answer: dict[str, Any] | None = None
) -> bool:
    """Own one receiver, one worker, and one browser-control task until finalization."""
    finalized = False
    browser_connected = True
    stopping = False

    async def notify(event: dict[str, Any]) -> None:
        nonlocal browser_connected
        if browser_connected:
            try:
                await browser.send_json(event)
            except (WebSocketDisconnect, RuntimeError, OSError):
                browser_connected = False

    async def send(event: dict[str, Any]) -> None:
        if stopping:
            raise RuntimeError("The Live session is closing.")
        await connection.send(event)

    handler = DelegationHandler(agent, send, notify)

    async def read_live() -> None:
        nonlocal finalized, stopping
        async for event in connection:
            data = event.model_dump()
            kind = data["type"]
            if kind == "session.closed":
                stopping = True
                finalized = True
                await notify(
                    {"type": "closed", "usage": data.get("usage"), "reason": data["reason"]}
                )
                return
            if kind == "error":
                stopping = True
                # Any rejected command stops this demo; never continue an uncertain batch.
                raise RuntimeError("Live rejected a command or reported an error.")
            handler.receive(data)
            if kind in {
                "session.input_transcript.delta",
                "session.output_transcript.delta",
                "session.usage.updated",
            }:
                await notify(data)

    async def read_browser() -> None:
        nonlocal browser_connected, stopping
        try:
            while True:
                command = await browser.receive_json()
                if command.get("type") == "close":
                    stopping = True
                    return
                raise ValueError("Unsupported browser command.")
        except WebSocketDisconnect:
            browser_connected = False
            stopping = True

    reader = asyncio.create_task(read_live())
    worker = asyncio.create_task(handler.work())
    controls = asyncio.create_task(read_browser())
    tasks = [reader, worker, controls]
    try:
        if answer is not None:
            await notify(answer)
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        stopping = True
        # Stop Agent work before sending session.close or closing the transport.
        worker.cancel()
        controls.cancel()
        await asyncio.gather(worker, controls, return_exceptions=True)
        if not finalized:
            try:
                await asyncio.wait_for(connection.session.close(), CLOSE_TIMEOUT)
                await asyncio.wait_for(asyncio.shield(reader), CLOSE_TIMEOUT)
            except Exception:
                pass
        if not finalized:
            await notify(
                {"type": "error", "message": "Session finalization could not be confirmed."}
            )
            logger.warning("Live session finalization could not be confirmed.")
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
    return finalized


async def close_unattached_session(client: Any, session_id: str) -> None:
    """Attempt finalization when setup failed after a session was created."""

    async def finish() -> None:
        async with client.live.sideband.connect(
            session_id=session_id,
            graceful_close=False,
            websocket_connection_options={"open_timeout": 5},
        ) as connection:
            await connection.session.close()
            async for event in connection:
                if event.type == "session.closed":
                    return
            raise RuntimeError("No final session event.")

    try:
        await asyncio.wait_for(finish(), CLOSE_TIMEOUT)
    except Exception:
        logger.warning("Setup failed and Live session finalization could not be confirmed.")


@app.websocket("/ws")
async def session(browser: WebSocket) -> None:
    # This is a local demo, not an authentication mechanism for a hosted service.
    if browser.headers.get("origin") not in LOCAL_ORIGINS:
        await browser.close(code=1008)
        return
    await browser.accept()
    try:
        offer = Offer.model_validate(await asyncio.wait_for(browser.receive_json(), timeout=30))
        async with AsyncOpenAI(max_retries=0, timeout=30) as client:
            # Live dependencies are example-local; the SDK package minimum stays unchanged.
            if not hasattr(client, "live"):
                raise RuntimeError("Install the dependencies in this example's requirements.txt.")
            result = await client.live.create(
                session=session_config(),
                transport={"type": "webrtc", "sdp": offer.sdp},
            )
            attached = False
            try:
                async with client.live.sideband.connect(
                    session_id=result.session.id,
                    graceful_close=False,
                    websocket_connection_options={"open_timeout": 15},
                ) as connection:
                    attached = True
                    # The relay owns cleanup before it sends the SDP answer to the browser.
                    await relay(
                        connection,
                        browser,
                        create_order_agent(),
                        answer={
                            "type": "answer",
                            "sdp": result.transport.sdp,
                            "session_id": result.session.id,
                        },
                    )
            finally:
                if not attached:
                    await close_unattached_session(client, result.session.id)
    except WebSocketDisconnect:
        pass
    except Exception as error:
        # SDK errors can contain request details; do not return or log their payloads.
        api_status = error.status_code if isinstance(error, APIStatusError) else None
        logger.warning(
            "Live example session failed (%s, API status %s).", type(error).__name__, api_status
        )
        with suppress(WebSocketDisconnect, RuntimeError, OSError):
            await browser.send_json(
                {
                    "type": "error",
                    "message": "The session failed. Check server dependencies and GPT Live access.",
                }
            )
    finally:
        with suppress(WebSocketDisconnect, RuntimeError, OSError):
            await browser.close()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000, ws_max_size=65536)
