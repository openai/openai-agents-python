from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from typing import Any

from ..agent import Agent
from ..items import TResponseInputItem
from ..result import RunResultStreaming
from ..run import Runner


class VoiceWorkflowBase(abc.ABC):
    """
    A base class for a voice workflow. You must implement the `run` method. A "workflow" is any
    code you want, that receives a transcription and yields text that will be turned into speech
    by a text-to-speech model.
    In most cases, you'll create `Agent`s and use `Runner.run_streamed()` to run them, returning
    some or all of the text events from the stream. You can use the `VoiceWorkflowHelper` class to
    help with extracting text events from the stream.
    If you have a simple workflow that has a single starting agent and no custom logic, you can
    use `SingleAgentVoiceWorkflow` directly.
    """

    @abc.abstractmethod
    def run(self, transcription: str) -> AsyncIterator[str]:
        """
        Run the voice workflow. You will receive an input transcription, and must yield text that
        will be spoken to the user. You can run whatever logic you want here. In most cases, the
        final logic will involve calling `Runner.run_streamed()` and yielding any text events from
        the stream.
        """
        pass


class VoiceWorkflowHelper:
    @classmethod
    async def stream_text_from(cls, result: RunResultStreaming) -> AsyncIterator[str]:
        """Wraps a `RunResultStreaming` object and yields text events from the stream."""
        async for event in result.stream_events():
            if (
                event.type == "raw_response_event"
                and event.data.type == "response.output_text.delta"
            ):
                yield event.data.delta


class SingleAgentWorkflowCallbacks:
    def on_run(self, workflow: SingleAgentVoiceWorkflow, transcription: str) -> None:
        """Called when the workflow is run."""
        pass


class SingleAgentVoiceWorkflow(VoiceWorkflowBase):
    """A simple voice workflow that runs a single agent. Each transcription and result is added to
    the input history.
    For more complex workflows (e.g. multiple Runner calls, custom message history, custom logic,
    custom configs), subclass `VoiceWorkflowBase` and implement your own logic.
    """

    def __init__(self, agent: Agent[Any], callbacks: SingleAgentWorkflowCallbacks | None = None):
        """Create a new single agent voice workflow.

        Args:
            agent: The agent to run.
            callbacks: Optional callbacks to call during the workflow.
        """
        self._input_history: list[TResponseInputItem] = []
        self._current_agent = agent
        self._callbacks = callbacks

    async def run(self, transcription: str) -> AsyncIterator[str]:
        if self._callbacks:
            self._callbacks.on_run(self, transcription)

        # Add the transcription to the input history
        self._input_history.append(
            {
                "role": "user",
                "content": transcription,
            }
        )

        # Run the agent
        result = Runner.run_streamed(self._current_agent, self._input_history)

        # Stream the text from the result
        async for chunk in VoiceWorkflowHelper.stream_text_from(result):
            yield chunk

        # Update the input history and current agent
        self._input_history = result.to_input_list()
        self._current_agent = result.last_agent
