"""Let a Realtime speech agent delegate work to a native Session.

Install the matching SDK's voice and classic-realtime extras and set
OPENAI_API_KEY. Run: python examples/codex/realtime_voice.py 'Your question' answer.wav
A text prompt starts the speech conversation; Realtime generates PCM audio.
The two conversations stay separate. Application code must handle any native
Session interactions; this example grants no approvals. Realtime barge-in does
not automatically cancel native work. Scope exit explicitly cancels that work. Record for a bounded --seconds window;
a spoken preamble does not end the example before delegation completes.
"""

import argparse
import asyncio
import wave
from contextlib import aclosing
from pathlib import Path

from agents import Agent
from agents.classic import function_tool as realtime_function_tool
from agents.classic.realtime import RealtimeAgent, RealtimeRunner
from agents.voice import NativeSessionVoiceWorkflow


async def main(prompt: str, destination: Path, seconds: float) -> None:
    if seconds <= 0:
        raise ValueError("seconds must be positive")
    native = Agent(instructions="Solve the user's request. Return a concise answer for speech.")
    async with native.session() as session:
        workflow = NativeSessionVoiceWorkflow(session)

        @realtime_function_tool(failure_error_function=None)
        async def ask_native(input: str) -> str:
            """Delegate substantive work to the native Session."""
            async with aclosing(workflow.run(input)) as chunks:
                return "".join([chunk async for chunk in chunks])

        speech = RealtimeAgent(
            name="Voice concierge",
            instructions="Always use ask_native for the user's request, then speak its answer.",
            tools=[ask_native],
        )
        try:
            async with await RealtimeRunner(speech).run() as realtime:
                with wave.open(str(destination), "wb") as output:
                    output.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                    await realtime.send_message(prompt)

                    async def record() -> None:
                        async for event in realtime:
                            if event.type == "audio":
                                output.writeframes(event.audio.data)
                            elif event.type == "tool_end":
                                print("Native answer:", event.output)
                            elif event.type == "error":
                                raise RuntimeError("Realtime speech failed")

                    try:
                        await asyncio.wait_for(record(), timeout=seconds)
                    except asyncio.TimeoutError:
                        print("Recording window ended; stopping delegated work.")
        finally:
            await workflow.cancel()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt")
    parser.add_argument("output", type=Path)
    parser.add_argument("--seconds", type=float, default=30)
    options = parser.parse_args()
    asyncio.run(main(options.prompt, options.output, options.seconds))
