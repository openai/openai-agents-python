"""Minimal FastAPI server for handling OpenAI Realtime SIP calls with Twilio."""

from __future__ import annotations

import asyncio
import logging
import os

import websockets
from fastapi import FastAPI, HTTPException, Request, Response
from openai import APIStatusError, AsyncOpenAI, InvalidWebhookSignatureError

from agents.realtime.config import RealtimeSessionModelSettings
from agents.realtime.items import (
    AssistantAudio,
    AssistantMessageItem,
    AssistantText,
    InputText,
    UserMessageItem,
)
from agents.realtime.model_inputs import RealtimeModelSendRawMessage
from agents.realtime.openai_realtime import OpenAIRealtimeSIPModel
from agents.realtime.runner import RealtimeRunner

from .agents import WELCOME_MESSAGE, get_starting_agent

logging.basicConfig(level=logging.INFO)

logger = logging.getLogger("twilio_sip_example")


def _get_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing environment variable: {name}")
    return value


OPENAI_API_KEY = _get_env("OPENAI_API_KEY")
OPENAI_WEBHOOK_SECRET = _get_env("OPENAI_WEBHOOK_SECRET")

client = AsyncOpenAI(api_key=OPENAI_API_KEY, webhook_secret=OPENAI_WEBHOOK_SECRET)

# Build the multi-agent graph (triage + specialist agents) from agents.py.
assistant_agent = get_starting_agent()

app = FastAPI()

# Track background tasks so repeated webhooks do not spawn duplicates.
active_call_tasks: dict[str, asyncio.Task[None]] = {}


async def accept_call(call_id: str) -> None:
    """Accept the incoming SIP call and configure the realtime session."""

    # The starting agent uses static instructions, so we can forward them directly to the accept
    # call payload. If someone swaps in a dynamic prompt, fall back to a sensible default.
    instructions_payload = (
        assistant_agent.instructions
        if isinstance(assistant_agent.instructions, str)
        else "You are a helpful triage agent for ABC customer service."
    )

    try:
        # AsyncOpenAI does not yet expose high-level helpers like client.realtime.calls.accept, so
        # we call the REST endpoint directly via client.post(). Keep this until the SDK grows an
        # async helper.
        await client.post(
            f"/realtime/calls/{call_id}/accept",
            body={
                "type": "realtime",
                "model": "gpt-realtime",
                "instructions": instructions_payload,
            },
            cast_to=dict,
        )
    except APIStatusError as exc:
        if exc.status_code == 404:
            # Twilio occasionally retries webhooks after the caller hangs up; treat as a no-op so
            # the webhook still returns 200.
            logger.warning(
                "Call %s no longer exists when attempting accept (404). Skipping.", call_id
            )
            return

        detail = exc.message
        if exc.response is not None:
            try:
                detail = exc.response.text
            except Exception:  # noqa: BLE001
                detail = str(exc.response)

        logger.error("Failed to accept call %s: %s %s", call_id, exc.status_code, detail)
        raise HTTPException(status_code=500, detail="Failed to accept call") from exc

    logger.info("Accepted call %s", call_id)


async def observe_call(call_id: str) -> None:
    """Attach to the realtime session and log conversation events."""

    runner = RealtimeRunner(assistant_agent, model=OpenAIRealtimeSIPModel())

    try:
        initial_model_settings: RealtimeSessionModelSettings = {
            "turn_detection": {
                "type": "semantic_vad",
                "interrupt_response": True,
            }
        }
        async with await runner.run(
            model_config={
                "call_id": call_id,
                "initial_model_settings": initial_model_settings,
            }
        ) as session:
            # Trigger an initial greeting so callers hear the agent right away.
            # Issue a response.create immediately after the WebSocket attaches so the model speaks
            # before the caller says anything. Using the raw client message ensures zero latency
            # and avoids threading the greeting through history.
            await session.model.send_event(
                RealtimeModelSendRawMessage(
                    message={
                        "type": "response.create",
                        "other_data": {
                            "response": {
                                "instructions": (
                                    "Say exactly '"
                                    f"{WELCOME_MESSAGE}"
                                    "' now before continuing the conversation."
                                )
                            }
                        },
                    }
                )
            )

            async for event in session:
                if event.type == "history_added":
                    item = event.item
                    if isinstance(item, UserMessageItem):
                        for user_content in item.content:
                            if isinstance(user_content, InputText) and user_content.text:
                                logger.info("Caller: %s", user_content.text)
                    elif isinstance(item, AssistantMessageItem):
                        for assistant_content in item.content:
                            if (
                                isinstance(assistant_content, AssistantText)
                                and assistant_content.text
                            ):
                                logger.info("Assistant (text): %s", assistant_content.text)
                            elif (
                                isinstance(assistant_content, AssistantAudio)
                                and assistant_content.transcript
                            ):
                                logger.info(
                                    "Assistant (audio transcript): %s",
                                    assistant_content.transcript,
                                )
                elif event.type == "error":
                    logger.error("Realtime session error: %s", event.error)

    except websockets.exceptions.ConnectionClosedError:
        # Callers hanging up causes the WebSocket to close without a frame; log at info level so it
        # does not surface as an error.
        logger.info("Realtime WebSocket closed for call %s", call_id)
    except Exception as exc:  # noqa: BLE001 - demo logging only
        logger.exception("Error while observing call %s", call_id, exc_info=exc)
    finally:
        logger.info("Call %s ended", call_id)
        active_call_tasks.pop(call_id, None)


def _track_call_task(call_id: str) -> None:
    existing = active_call_tasks.get(call_id)
    if existing:
        if not existing.done():
            logger.info(
                "Call %s already has an active observer; ignoring duplicate webhook delivery.",
                call_id,
            )
            return
        # Remove completed tasks so a new observer can start for a fresh call.
        active_call_tasks.pop(call_id, None)

    task = asyncio.create_task(observe_call(call_id))
    active_call_tasks[call_id] = task


@app.post("/openai/webhook")
async def openai_webhook(request: Request) -> Response:
    body = await request.body()

    try:
        event = client.webhooks.unwrap(body, request.headers)
    except InvalidWebhookSignatureError as exc:
        raise HTTPException(status_code=400, detail="Invalid webhook signature") from exc

    if event.type == "realtime.call.incoming":
        call_id = event.data.call_id
        await accept_call(call_id)
        _track_call_task(call_id)
        return Response(status_code=200)

    # Ignore other webhook event types for brevity.
    return Response(status_code=200)


@app.get("/")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}
