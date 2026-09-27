import asyncio
import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from xml.sax.saxutils import quoteattr

from anyio import CancelScope
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from starlette.datastructures import FormData
from starlette.formparsers import FormParser, MultiPartException

from twilio.request_validator import RequestValidator

# Example policy: bound unauthenticated webhook parsing as well as media messages.
MAX_WEBHOOK_BYTES = 64 * 1024
MAX_WEBHOOK_FIELDS = 100
MAX_WEBHOOK_FIELD_BYTES = 8 * 1024

# Import TwilioHandler class - handle both module and package use cases
if TYPE_CHECKING:
    from .twilio_handler import TwilioHandler
else:
    try:
        from .twilio_handler import TwilioHandler
    except ImportError:
        from twilio_handler import TwilioHandler


@asynccontextmanager
async def lifespan(app: FastAPI):
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    public_base_url = os.environ.get("TWILIO_PUBLIC_BASE_URL", "").rstrip("/")
    if not auth_token:
        raise RuntimeError("TWILIO_AUTH_TOKEN is required")
    try:
        parsed = urlsplit(public_base_url)
        # urlsplit defers port syntax and range validation until this property is read.
        _ = parsed.port
    except ValueError:
        raise RuntimeError("TWILIO_PUBLIC_BASE_URL must be a valid HTTPS origin") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("TWILIO_PUBLIC_BASE_URL must be an HTTPS origin without a path or query")
    app.state.public_base_url = parsed.geturl()
    app.state.twilio_validator = RequestValidator(auth_token)
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def root():
    return {"message": "Twilio Media Stream Server is running!"}


@app.post("/incoming-call")
@app.get("/incoming-call")
async def incoming_call(request: Request):
    """Return TwiML only for an authenticated Twilio call webhook."""
    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
    params = FormData()
    if request.method == "POST":
        if request.headers.get("content-type", "").split(";", 1)[0] != (
            "application/x-www-form-urlencoded"
        ):
            raise HTTPException(status_code=415, detail="Expected a form-encoded Twilio webhook")

        async def bounded_body():
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_WEBHOOK_BYTES:
                    raise HTTPException(status_code=413, detail="Twilio webhook is too large")
                yield chunk

        try:
            params = await FormParser(
                request.headers,
                bounded_body(),
                max_fields=MAX_WEBHOOK_FIELDS,
                max_part_size=MAX_WEBHOOK_FIELD_BYTES,
            ).parse()
        except MultiPartException:
            raise HTTPException(
                status_code=413, detail="Twilio webhook exceeds form limits"
            ) from None
    public_url = f"{request.app.state.public_base_url}/incoming-call"
    if request.url.query:
        public_url += f"?{request.url.query}"
    if not request.app.state.twilio_validator.validate(public_url, params, signature):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    stream_url = (
        request.app.state.public_base_url.replace("https://", "wss://", 1) + "/media-stream"
    )
    twiml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Hello! You're now connected to an AI assistant. You can start talking!</Say>
    <Connect>
        <Stream url={quoteattr(stream_url)} />
    </Connect>
</Response>"""
    return PlainTextResponse(content=twiml_response, media_type="text/xml")


@app.websocket("/media-stream")
async def media_stream_endpoint(websocket: WebSocket):
    """Authenticate the handshake before allocating any OpenAI resources."""
    signature = websocket.headers.get("X-Twilio-Signature", "")
    stream_url = (
        websocket.app.state.public_base_url.replace("https://", "wss://", 1) + "/media-stream"
    )
    validator = websocket.app.state.twilio_validator
    # Stream URLs do not support query parameters. Twilio documents the trailing-slash
    # signature variant for Voice WSS handshakes.
    if (
        not signature
        or websocket.url.query
        or not (
            validator.validate(stream_url, {}, signature)
            or validator.validate(stream_url + "/", {}, signature)
        )
    ):
        await websocket.close(code=1008)
        return

    handler = TwilioHandler(websocket)
    try:
        await handler.start()
        await handler.wait_until_done()
    except WebSocketDisconnect:
        print("WebSocket disconnected")
    except Exception:
        print("Twilio session failed")
    finally:
        # Shield both AnyIO scope cancellation and Uvicorn's raw task cancellation.
        with CancelScope(shield=True):
            cleanup = asyncio.create_task(handler.close())
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await asyncio.wait({cleanup})
                cleanup.result()  # Preserve cleanup failures before propagating cancellation.
                raise


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 8000))
    uvicorn.run(
        app, host="0.0.0.0", port=port, ws="websockets", ws_max_size=TwilioHandler.MAX_MESSAGE_BYTES
    )
