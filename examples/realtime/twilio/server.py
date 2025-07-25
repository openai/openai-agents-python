import os
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse

# Import TwilioHandler class - handle both module and package use cases
if TYPE_CHECKING:
    # For type checking, use the relative import
    from .twilio_handler import TwilioHandler
else:
    # At runtime, try both import styles
    try:
        # Try relative import first (when used as a package)
        from .twilio_handler import TwilioHandler
    except ImportError:
        # Fall back to direct import (when run as a script)
        from twilio_handler import TwilioHandler


class TwilioWebSocketManager:
    def __init__(self):
        self.active_handlers: dict[str, TwilioHandler] = {}

    async def new_session(self, websocket: WebSocket) -> TwilioHandler:
        """Create and configure a new session."""
        print("Creating twilio handler")

        handler = TwilioHandler(websocket)
        return handler

    # In a real app, you'd also want to clean up/close the handler when the call ends


manager = TwilioWebSocketManager()
app = FastAPI()


@app.get("/")
async def root():
    return {"message": "Twilio Media Stream Server is running!"}


@app.post("/incoming-call")
@app.get("/incoming-call")
async def incoming_call(request: Request):
    """Handle incoming Twilio phone calls"""
    host = request.headers.get("Host")

    twiml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Hello! You're now connected to an AI assistant. You can start talking!</Say>
    <Connect>
        <Stream url="wss://{host}/media-stream" />
    </Connect>
</Response>"""
    return PlainTextResponse(content=twiml_response, media_type="text/xml")


@app.websocket("/media-stream")
async def media_stream_endpoint(websocket: WebSocket):
    """WebSocket endpoint for Twilio Media Streams"""

    try:
        handler = await manager.new_session(websocket)
        await handler.start()

        await handler.wait_until_done()

    except WebSocketDisconnect:
        print("WebSocket disconnected")
    except Exception as e:
        print(f"WebSocket error: {e}")


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
