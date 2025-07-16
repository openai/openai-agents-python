from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse
from openai import OpenAI
import logging
from agents.realtime.config import RealtimeRunConfig, RealtimeSessionModelSettings, RealtimeTurnDetectionConfig, RealtimeAudioFormat
from agents.realtime.runner import RealtimeRunner
from agents.realtime.events import RealtimeSessionEvent
from twilio.twiml.voice_response import VoiceResponse, Connect
from agents.realtime.agent import RealtimeAgent
import json
import asyncio
import base64
import random
from agents import function_tool

FASTAPI_APP = FastAPI()

@function_tool
async def get_random_number() -> int:
    return random.randint(0, 100)


AUDIO_AGENT = RealtimeAgent(
    name="ODAI-Voice",
    instructions='You are a voice assistant that can answer questions and help with tasks.',
    tools=[get_random_number]
)

def _truncate_str(s: str, max_length: int) -> str:
    if len(s) > max_length:
        return s[:max_length] + "..."
    return s

@FASTAPI_APP.post('/incoming')
async def incoming_voice_get(request: Request):
    """Handle incoming call and return TwiML response to connect to Media Stream."""
    response = VoiceResponse()
    host = request.url.hostname
    # response.say("Welcome to ODAI. How can I help you today?")
    connect = Connect()
    connect.stream(url=f'wss://{host}/twilio/streaming')
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")

@FASTAPI_APP.websocket('/streaming')
async def voice_streaming(websocket: WebSocket):
    await websocket.accept()
    # user = User.get_user_by_id('lvX2TjNNcYYSroYeJ3LpRuUwwWs1')
    config = RealtimeRunConfig(
        model_settings=RealtimeSessionModelSettings(
            voice="sage",
            turn_detection=RealtimeTurnDetectionConfig(
                type='server_vad'
            ),
            input_audio_format='g711_ulaw',
            output_audio_format='g711_ulaw'
        )
    )
    runner = RealtimeRunner(AUDIO_AGENT, config=config)
    async with await runner.run() as realtime_session:
        stream_sid = None
        continue_streaming = True

        async def receive_from_twilio():
            """Receive audio data from Twilio and send it to the OpenAI Realtime API."""
            nonlocal stream_sid, continue_streaming
            
            async for message in websocket.iter_text():
                data = json.loads(message)
                if data['event'] == 'media':
                    audio = base64.b64decode(data['media']['payload'])
                    await realtime_session.send_audio(audio)
                elif data['event'] == 'start':
                    stream_sid = data['start']['streamSid']
                    print(f"Incoming stream has started {stream_sid}")
                elif data['event'] == 'stop':
                    continue_streaming = False
                    break

        async def send_to_twilio():
            nonlocal stream_sid
            await realtime_session.send_message("Greet the user with 'Hello! Welcome to the ODAI Voice Assistant. How can I help you today?' and then wait for the user to speak.")
            async for event in realtime_session:
                await _on_event(event, stream_sid)
            print("Twilio session ended")
        
        async def _on_event(event: RealtimeSessionEvent, stream_sid: str | None) -> None:
            try:
                if event.type == "agent_start":
                    print(f"Agent started: {event.agent.name}")
                elif event.type == "agent_end":
                    print(f"Agent ended: {event.agent.name}")
                elif event.type == "handoff":
                    print(
                        f"Handoff from {event.from_agent.name} to {event.to_agent.name}"
                    )
                elif event.type == "tool_start":
                    print(f"Tool started: {event.tool.name}")
                elif event.type == "tool_end":
                    print(f"Tool ended: {event.tool.name}; output: {event.output}")
                elif event.type == "audio_end":
                    print("Audio ended")
                elif event.type == "audio":
                    audio_payload = base64.b64encode(event.audio.data).decode('utf-8')
                    audio_delta = {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {
                                "payload": audio_payload
                            }
                        }
                    await websocket.send_json(audio_delta)
                elif event.type == "audio_interrupted":
                    print("Audio interrupted")
                elif event.type == "error":
                    pass
                elif event.type == "history_updated":
                    pass
                elif event.type == "history_added":
                    pass
                elif event.type == "raw_model_event":
                    print(f"Raw model event: {_truncate_str(str(event.data), 50)}")
                else:
                    print(f"Unknown event type: {event.type}")
            except Exception as e:
                print(f"Error processing event: {_truncate_str(str(e), 50)}")

        async def monitor_connection(send_to_twilio_task, receive_from_twilio_task):
            nonlocal continue_streaming
            while continue_streaming:
                await asyncio.sleep(0.25)
            continue_streaming = False
            send_to_twilio_task.cancel()
            receive_from_twilio_task.cancel()
            await realtime_session.close()
            await websocket.close()

        send_to_twilio_task = asyncio.create_task(send_to_twilio())
        receive_from_twilio_task = asyncio.create_task(receive_from_twilio())
        monitor_connection_task = asyncio.create_task(monitor_connection(send_to_twilio_task, receive_from_twilio_task))
        
        await asyncio.gather(receive_from_twilio_task, send_to_twilio_task, monitor_connection_task)

