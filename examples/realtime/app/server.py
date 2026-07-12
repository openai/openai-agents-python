import asyncio
import base64
import json
import logging
import os
import struct
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from typing_extensions import assert_never

from agents.realtime import RealtimeRunner, RealtimeSession, RealtimeSessionEvent
from agents.realtime.config import RealtimeUserInputMessage
from agents.realtime.items import RealtimeItem
from agents.realtime.model import RealtimeModelConfig
from agents.realtime.model_events import (
    RealtimeModelItemUpdatedEvent,
    RealtimeModelRawServerEvent,
    RealtimeModelUsageEvent,
)
from agents.realtime.model_inputs import RealtimeModelSendRawMessage

# Import TwilioHandler class - handle both module and package use cases
if TYPE_CHECKING:
    # For type checking, use the relative import
    from .agent import get_starting_agent
else:
    # At runtime, try both import styles
    try:
        # Try relative import first (when used as a package)
        from .agent import get_starting_agent
    except ImportError:
        # Fall back to direct import (when run as a script)
        from agent import get_starting_agent


_requested_log_level = os.getenv("LOG_LEVEL", "INFO").upper()
_log_level = getattr(logging, _requested_log_level, logging.INFO)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logger.setLevel(_log_level)


class RealtimeWebSocketManager:
    def __init__(self):
        self.active_sessions: dict[str, RealtimeSession] = {}
        self.session_contexts: dict[str, Any] = {}
        self.websockets: dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, session_id: str):
        await websocket.accept()
        self.websockets[session_id] = websocket

        agent = get_starting_agent()
        runner = RealtimeRunner(agent)
        # If you want to customize the runner behavior, you can pass options:
        # runner_config = RealtimeRunConfig(async_tool_calls=False)
        # runner = RealtimeRunner(agent, config=runner_config)
        model_config: RealtimeModelConfig = {
            "initial_model_settings": {
                "model_name": "gpt-realtime-2.1",
                "turn_detection": {
                    "type": "server_vad",
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": 500,
                    "interrupt_response": True,
                    "create_response": True,
                },
            },
        }
        session_context = await runner.run(model_config=model_config)
        session = await session_context.__aenter__()
        self.active_sessions[session_id] = session
        self.session_contexts[session_id] = session_context

        # Start event processing task
        asyncio.create_task(self._process_events(session_id))

    async def disconnect(self, session_id: str):
        if session_id in self.session_contexts:
            await self.session_contexts[session_id].__aexit__(None, None, None)
            del self.session_contexts[session_id]
        if session_id in self.active_sessions:
            del self.active_sessions[session_id]
        if session_id in self.websockets:
            del self.websockets[session_id]

    async def send_audio(self, session_id: str, audio_bytes: bytes):
        if session_id in self.active_sessions:
            await self.active_sessions[session_id].send_audio(audio_bytes)

    async def send_client_event(self, session_id: str, event: dict[str, Any]):
        """Send a raw client event to the underlying realtime model."""
        session = self.active_sessions.get(session_id)
        if not session:
            return
        await session.model.send_event(
            RealtimeModelSendRawMessage(
                message={
                    "type": event["type"],
                    "other_data": {k: v for k, v in event.items() if k != "type"},
                }
            )
        )

    async def send_user_message(self, session_id: str, message: RealtimeUserInputMessage):
        """Send a structured user message via the higher-level API (supports input_image)."""
        session = self.active_sessions.get(session_id)
        if not session:
            return
        await session.send_message(message)  # delegates to RealtimeModelSendUserInput path

    async def approve_tool_call(self, session_id: str, call_id: str, *, always: bool = False):
        """Approve a pending tool call for a session."""
        session = self.active_sessions.get(session_id)
        if not session:
            return
        await session.approve_tool_call(call_id, always=always)

    async def reject_tool_call(self, session_id: str, call_id: str, *, always: bool = False):
        """Reject a pending tool call for a session."""
        session = self.active_sessions.get(session_id)
        if not session:
            return
        await session.reject_tool_call(call_id, always=always)

    async def interrupt(self, session_id: str) -> None:
        """Interrupt current model playback/response for a session."""
        session = self.active_sessions.get(session_id)
        if not session:
            return
        await session.interrupt()

    async def _process_events(self, session_id: str):
        try:
            session = self.active_sessions[session_id]
            websocket = self.websockets[session_id]

            async for event in session:
                self._log_debug_event(session_id, event)
                event_data = await self._serialize_event(event)
                await websocket.send_text(json.dumps(event_data))
        except Exception as e:
            print(e)
            logger.error(f"Error processing events for session {session_id}: {e}")

    def _log_debug_event(self, session_id: str, event: RealtimeSessionEvent) -> None:
        """Log useful event summaries without noisy audio or delta payloads."""
        if not logger.isEnabledFor(logging.DEBUG):
            return

        if event.type == "audio":
            return
        if event.type == "audio_end":
            return
        if event.type == "audio_interrupted":
            return

        if event.type == "raw_model_event":
            self._log_debug_model_event(session_id, event)
            return

        event_summary: dict[str, Any] = {"type": event.type}
        if event.type == "agent_start":
            event_summary["agent"] = event.agent.name
        elif event.type == "agent_end":
            event_summary["agent"] = event.agent.name
        elif event.type == "handoff":
            event_summary["from_agent"] = event.from_agent.name
            event_summary["to_agent"] = event.to_agent.name
        elif event.type == "tool_start":
            event_summary["tool"] = event.tool.name
        elif event.type == "tool_end":
            event_summary["tool"] = event.tool.name
        elif event.type == "tool_approval_required":
            event_summary.update(
                {
                    "agent": event.agent.name,
                    "tool": event.tool.name,
                    "call_id": event.call_id,
                }
            )
        elif event.type == "history_updated":
            event_summary["item_count"] = len(event.history)
            if event.history:
                event_summary["last_item"] = self._item_debug_summary(event.history[-1])
        elif event.type == "history_added":
            event_summary["item"] = self._item_debug_summary(event.item)
        elif event.type == "guardrail_tripped":
            event_summary["guardrails"] = [
                result.guardrail.name for result in event.guardrail_results
            ]
        elif event.type == "error":
            event_summary["error"] = str(event.error)
        elif event.type == "input_audio_timeout_triggered":
            pass
        else:
            assert_never(event)

        logger.debug("Realtime session event session_id=%s event=%s", session_id, event_summary)

    def _log_debug_model_event(self, session_id: str, event: Any) -> None:
        model_event = event.data
        if model_event.type in {"audio", "transcript_delta"}:
            return

        if isinstance(model_event, RealtimeModelRawServerEvent):
            raw_event = model_event.data
            if not isinstance(raw_event, dict):
                return

            raw_type = raw_event.get("type")
            if isinstance(raw_type, str) and raw_type.endswith(".delta"):
                return

            raw_summary: dict[str, Any] = {
                "type": raw_type,
                "event_id": raw_event.get("event_id"),
            }
            response = raw_event.get("response")
            if isinstance(response, dict):
                raw_summary["response_id"] = response.get("id")
                raw_summary["response_status"] = response.get("status")
            item = raw_event.get("item")
            if isinstance(item, dict):
                raw_summary["item_id"] = item.get("id")
                raw_summary["item_type"] = item.get("type")
            else:
                raw_summary["item_id"] = raw_event.get("item_id")

            raw_summary = {key: value for key, value in raw_summary.items() if value is not None}

            if raw_type == "response.done":
                raw_summary["usage"] = response.get("usage") if isinstance(response, dict) else None
                logger.debug(
                    "Realtime raw response completed session_id=%s event=%s",
                    session_id,
                    raw_summary,
                )
            else:
                logger.debug(
                    "Realtime raw server event session_id=%s event=%s",
                    session_id,
                    raw_summary,
                )
            return

        if isinstance(model_event, RealtimeModelUsageEvent):
            self._log_debug_usage_event(session_id, event, model_event)
            return

        model_summary: dict[str, Any] = {"type": model_event.type}
        for field_name in (
            "item_id",
            "response_id",
            "call_id",
            "name",
            "status",
            "content_index",
        ):
            value = getattr(model_event, field_name, None)
            if value is not None:
                model_summary[field_name] = value
        if isinstance(model_event, RealtimeModelItemUpdatedEvent):
            model_summary["item"] = self._item_debug_summary(model_event.item)

        logger.debug(
            "Realtime model event session_id=%s event=%s",
            session_id,
            model_summary,
        )

    def _log_debug_usage_event(
        self,
        session_id: str,
        event: Any,
        model_event: RealtimeModelUsageEvent,
    ) -> None:
        response_usage = model_event.usage
        cumulative_usage = event.info.context.usage
        logger.debug(
            "Realtime typed response usage session_id=%s aggregate=%s "
            "input_details=%s output_details=%s",
            session_id,
            {
                "requests": response_usage.requests,
                "input_tokens": response_usage.input_tokens,
                "output_tokens": response_usage.output_tokens,
                "total_tokens": response_usage.total_tokens,
                "cached_input_tokens": response_usage.input_tokens_details.cached_tokens,
            },
            (
                asdict(model_event.input_tokens_details)
                if model_event.input_tokens_details is not None
                else None
            ),
            (
                asdict(model_event.output_tokens_details)
                if model_event.output_tokens_details is not None
                else None
            ),
        )
        logger.debug(
            "Realtime cumulative session usage session_id=%s aggregate=%s",
            session_id,
            {
                "requests": cumulative_usage.requests,
                "input_tokens": cumulative_usage.input_tokens,
                "output_tokens": cumulative_usage.output_tokens,
                "total_tokens": cumulative_usage.total_tokens,
                "cached_input_tokens": cumulative_usage.input_tokens_details.cached_tokens,
            },
        )

    @staticmethod
    def _item_debug_summary(item: RealtimeItem) -> dict[str, Any]:
        content = getattr(item, "content", None)
        return {
            "item_id": item.item_id,
            "type": item.type,
            "role": getattr(item, "role", None),
            "status": getattr(item, "status", None),
            "content_types": (
                [getattr(part, "type", type(part).__name__) for part in content]
                if isinstance(content, list)
                else []
            ),
        }

    def _sanitize_history_item(self, item: RealtimeItem) -> dict[str, Any]:
        """Remove large binary payloads from history items while keeping transcripts."""
        item_dict = item.model_dump()
        content = item_dict.get("content")
        if isinstance(content, list):
            sanitized_content: list[Any] = []
            for part in content:
                if isinstance(part, dict):
                    sanitized_part = part.copy()
                    if sanitized_part.get("type") in {"audio", "input_audio"}:
                        sanitized_part.pop("audio", None)
                    sanitized_content.append(sanitized_part)
                else:
                    sanitized_content.append(part)
            item_dict["content"] = sanitized_content
        return item_dict

    async def _serialize_event(self, event: RealtimeSessionEvent) -> dict[str, Any]:
        base_event: dict[str, Any] = {
            "type": event.type,
        }

        if event.type == "agent_start":
            base_event["agent"] = event.agent.name
        elif event.type == "agent_end":
            base_event["agent"] = event.agent.name
        elif event.type == "handoff":
            base_event["from"] = event.from_agent.name
            base_event["to"] = event.to_agent.name
        elif event.type == "tool_start":
            base_event["tool"] = event.tool.name
        elif event.type == "tool_end":
            base_event["tool"] = event.tool.name
            base_event["output"] = str(event.output)
        elif event.type == "tool_approval_required":
            base_event["tool"] = event.tool.name
            base_event["call_id"] = event.call_id
            base_event["arguments"] = event.arguments
            base_event["agent"] = event.agent.name
        elif event.type == "audio":
            base_event["audio"] = base64.b64encode(event.audio.data).decode("utf-8")
        elif event.type == "audio_interrupted":
            pass
        elif event.type == "audio_end":
            pass
        elif event.type == "history_updated":
            base_event["history"] = [self._sanitize_history_item(item) for item in event.history]
        elif event.type == "history_added":
            # Provide the added item so the UI can render incrementally.
            try:
                base_event["item"] = self._sanitize_history_item(event.item)
            except Exception:
                base_event["item"] = None
        elif event.type == "guardrail_tripped":
            base_event["guardrail_results"] = [
                {"name": result.guardrail.name} for result in event.guardrail_results
            ]
        elif event.type == "raw_model_event":
            base_event["raw_model_event"] = {
                "type": event.data.type,
            }
        elif event.type == "error":
            base_event["error"] = str(event.error) if hasattr(event, "error") else "Unknown error"
        elif event.type == "input_audio_timeout_triggered":
            pass
        else:
            assert_never(event)

        return base_event


manager = RealtimeWebSocketManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(lifespan=lifespan)


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    await manager.connect(websocket, session_id)
    image_buffers: dict[str, dict[str, Any]] = {}
    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)

            if message["type"] == "audio":
                # Convert int16 array to bytes
                int16_data = message["data"]
                audio_bytes = struct.pack(f"{len(int16_data)}h", *int16_data)
                await manager.send_audio(session_id, audio_bytes)
            elif message["type"] == "image":
                logger.info("Received image message from client (session %s).", session_id)
                # Build a conversation.item.create with input_image (and optional input_text)
                data_url = message.get("data_url")
                prompt_text = message.get("text") or "Please describe this image."
                if data_url:
                    logger.info(
                        "Forwarding image (structured message) to Realtime API (len=%d).",
                        len(data_url),
                    )
                    user_msg: RealtimeUserInputMessage = {
                        "type": "message",
                        "role": "user",
                        "content": (
                            [
                                {"type": "input_image", "image_url": data_url, "detail": "high"},
                                {"type": "input_text", "text": prompt_text},
                            ]
                            if prompt_text
                            else [{"type": "input_image", "image_url": data_url, "detail": "high"}]
                        ),
                    }
                    await manager.send_user_message(session_id, user_msg)
                    # Acknowledge to client UI
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "client_info",
                                "info": "image_enqueued",
                                "size": len(data_url),
                            }
                        )
                    )
                else:
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "error",
                                "error": "No data_url for image message.",
                            }
                        )
                    )
            elif message["type"] == "commit_audio":
                # Force close the current input audio turn
                await manager.send_client_event(session_id, {"type": "input_audio_buffer.commit"})
            elif message["type"] == "image_start":
                img_id = str(message.get("id"))
                image_buffers[img_id] = {
                    "text": message.get("text") or "Please describe this image.",
                    "chunks": [],
                }
                await websocket.send_text(
                    json.dumps({"type": "client_info", "info": "image_start_ack", "id": img_id})
                )
            elif message["type"] == "image_chunk":
                img_id = str(message.get("id"))
                chunk = message.get("chunk", "")
                if img_id in image_buffers:
                    image_buffers[img_id]["chunks"].append(chunk)
                    if len(image_buffers[img_id]["chunks"]) % 10 == 0:
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "type": "client_info",
                                    "info": "image_chunk_ack",
                                    "id": img_id,
                                    "count": len(image_buffers[img_id]["chunks"]),
                                }
                            )
                        )
            elif message["type"] == "image_end":
                img_id = str(message.get("id"))
                buf = image_buffers.pop(img_id, None)
                if buf is None:
                    await websocket.send_text(
                        json.dumps({"type": "error", "error": "Unknown image id for image_end."})
                    )
                else:
                    data_url = "".join(buf["chunks"]) if buf["chunks"] else None
                    prompt_text = buf["text"]
                    if data_url:
                        logger.info(
                            "Forwarding chunked image (structured message) to Realtime API (len=%d).",
                            len(data_url),
                        )
                        user_msg2: RealtimeUserInputMessage = {
                            "type": "message",
                            "role": "user",
                            "content": (
                                [
                                    {
                                        "type": "input_image",
                                        "image_url": data_url,
                                        "detail": "high",
                                    },
                                    {"type": "input_text", "text": prompt_text},
                                ]
                                if prompt_text
                                else [
                                    {"type": "input_image", "image_url": data_url, "detail": "high"}
                                ]
                            ),
                        }
                        await manager.send_user_message(session_id, user_msg2)
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "type": "client_info",
                                    "info": "image_enqueued",
                                    "id": img_id,
                                    "size": len(data_url),
                                }
                            )
                        )
                    else:
                        await websocket.send_text(
                            json.dumps({"type": "error", "error": "Empty image."})
                        )
            elif message["type"] == "tool_approval_decision":
                call_id = message.get("call_id")
                approve = bool(message.get("approve"))
                always = bool(message.get("always", False))
                if not call_id:
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "error",
                                "error": "Missing call_id for tool approval decision.",
                            }
                        )
                    )
                    continue
                if approve:
                    await manager.approve_tool_call(session_id, call_id, always=always)
                else:
                    await manager.reject_tool_call(session_id, call_id, always=always)
            elif message["type"] == "interrupt":
                await manager.interrupt(session_id)

    except WebSocketDisconnect:
        await manager.disconnect(session_id)


app.mount("/", StaticFiles(directory="static", html=True), name="static")


@app.get("/")
async def read_index():
    return FileResponse("static/index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        # Increased WebSocket frame size to comfortably handle image data URLs.
        ws_max_size=16 * 1024 * 1024,
    )
