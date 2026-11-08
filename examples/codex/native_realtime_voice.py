"""Record native Session-owned Realtime audio for a fixed window.

Set OPENAI_API_KEY and build this branch, or use the matching release example. Run:
python examples/codex/native_realtime_voice.py 'Your question' answer.wav
The recording window includes preambles and delegated work. Scope exit closes
Realtime and joins its delegated turns before closing the caller's Session.
"""

import argparse
import asyncio
import wave
from pathlib import Path

from agents import Agent
from agents.realtime import RealtimeAudio, RealtimeConfig


async def main(prompt: str, destination: Path, seconds: float) -> None:
    if seconds <= 0:
        raise ValueError("seconds must be positive")
    agent = Agent(instructions="Solve the user's request and return a concise answer for speech.")
    async with agent.session() as session:
        async with session.realtime(
            config=RealtimeConfig(
                instructions="Use background_agent to solve the user's request, then speak its answer."
            )
        ) as realtime:
            with wave.open(str(destination), "wb") as output:
                output.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                await realtime.send_text(prompt)

                async def record() -> None:
                    async for event in realtime.events():
                        if isinstance(event, RealtimeAudio):
                            output.writeframes(event.data)

                try:
                    await asyncio.wait_for(record(), timeout=seconds)
                except asyncio.TimeoutError:
                    pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt")
    parser.add_argument("output", type=Path)
    parser.add_argument("--seconds", type=float, default=30)
    options = parser.parse_args()
    asyncio.run(main(options.prompt, options.output, options.seconds))
