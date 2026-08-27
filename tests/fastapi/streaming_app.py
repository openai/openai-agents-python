from collections.abc import AsyncIterator

from fastapi import FastAPI
from starlette.responses import StreamingResponse

from agents import Agent, Runner, RunResultStreaming

agent = Agent(
    name="Assistant",
    instructions="You are a helpful assistant.",
)


app = FastAPI()


@app.post("/stream")
async def stream():
    result = Runner.run_streamed(agent, input="Tell me a joke")
    stream_handler = StreamHandler(result)
    return StreamingResponse(stream_handler.stream_events(), media_type="application/x-ndjson")


class StreamHandler:
    def __init__(self, result: RunResultStreaming):
        self.result = result

    async def stream_events(self) -> AsyncIterator[str]:
        async for event in self.result.stream_events():
            yield f"{event.type}\n\n"
