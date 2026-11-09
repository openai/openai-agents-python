"""Record a reply through an application-owned canonical Realtime V2 socket.

Build/install this branch with its voice extra and set OPENAI_API_KEY. Run:
python examples/codex/native_realtime_adapter.py 'Explain a rainbow' answer.wav
This pass-through adapter uses the native-selected endpoint and credentials.
It makes live API requests and records at most 30 seconds of mono PCM16 audio.
"""

import argparse
import asyncio
import wave
from collections.abc import AsyncIterator
from pathlib import Path

from websockets.asyncio.client import ClientConnection, connect

from agents import Agent
from agents.realtime import (
    NativeRealtimeConnection,
    RealtimeAdapterContext,
    RealtimeAudio,
    RealtimeConfig,
)


class DirectSocket(connect):
    def process_redirect(self, exc: Exception) -> Exception | str:
        # Do not forward the private headers to a redirected endpoint.
        return exc


class SocketConnection:
    def __init__(self, socket: ClientConnection) -> None:
        self.socket = socket

    @property
    def incoming(self) -> AsyncIterator[str]:
        return self.receive()

    async def receive(self) -> AsyncIterator[str]:
        async for frame in self.socket:
            if not isinstance(frame, str):
                raise TypeError("Expected a Realtime text frame")
            yield frame

    async def send(self, frame: str) -> None:
        await self.socket.send(frame)

    async def aclose(self) -> None:
        # Closing the socket wakes its independent reader and pending writes.
        await self.socket.close()


class SocketAdapter:
    async def connect(self, context: RealtimeAdapterContext) -> NativeRealtimeConnection:
        # websockets cleans up its handshake if this await fails or is cancelled.
        # Keep the private URL and authentication headers out of logs.
        socket = await DirectSocket(
            context.url,
            additional_headers=dict(context.headers),
            max_size=8 * 1024 * 1024,
        )
        return SocketConnection(socket)


async def main(prompt: str, destination: Path) -> None:
    agent = Agent(instructions="Answer spoken questions briefly.")
    async with agent.session() as session:
        async with session.realtime(
            config=RealtimeConfig(adapter=SocketAdapter(), settings={"output_format": "pcm16"})
        ) as voice:
            with wave.open(str(destination), "wb") as output:
                output.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                await voice.send_text(prompt)

                async def record() -> None:
                    async for event in voice.events():
                        if isinstance(event, RealtimeAudio):
                            output.writeframes(event.data)

                try:
                    await asyncio.wait_for(record(), timeout=30)
                except asyncio.TimeoutError:
                    pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt")
    parser.add_argument("output", type=Path)
    arguments = parser.parse_args()
    asyncio.run(main(arguments.prompt, arguments.output))
