from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any, List, Optional, Set, Tuple, Union, cast
import os
import numpy as np

from ..exceptions import AgentsException, UserError
from .imports import npt
from ..logger import logger
from ..tool import Tool
from .input import StreamedAudioInput
from .pipeline_config import VoicePipelineConfig
from .result_realtime import StreamedRealtimeResult
from .realtime.model import (
    RealtimeEventError as LLMErrorEvent,
    RealtimeEventToolCall as LLMToolCallEvent,
    RealtimeEventSessionEnds,
    RealtimeLLMModel,
    RealtimeSession,
)
from .realtime.tool_exec import ToolExecutor

# Import the new SDK-based implementation
from .models.sdk_realtime import SDKRealtimeLLM, SDKRealtimeSession
from openai import AsyncOpenAI


class RealtimeVoicePipeline:
    """A voice agent pipeline for real-time, bidirectional audio and tool interaction with an LLM."""

    def __init__(
        self,
        *,
        model: RealtimeLLMModel | str | None = None,
        tools: Sequence[Tool] = (),
        config: VoicePipelineConfig | None = None,
        shared_context: Any | None = None,
    ):
        """Create a new real-time voice pipeline.

        Args:
            model: The real-time LLM model to use. Can be an instance of RealtimeLLMModel
                   or a string identifier for a model from the provider.
            tools: A sequence of tools available to the LLM.
            config: The pipeline configuration. If not provided, a default will be used.
            shared_context: An optional context object that will be passed to tools when they are executed.
        """
        if isinstance(model, str) or model is None:
            self._model_name_to_load: str | None = model
            self._model_instance: RealtimeLLMModel | None = None
        elif isinstance(model, RealtimeLLMModel):
            self._model_instance = model
            self._model_name_to_load = None
        else:
            raise UserError(
                f"Invalid type for model: {type(model)}. Expected RealtimeLLMModel or str."
            )

        self._tools = tools
        self._config = config or VoicePipelineConfig()
        self._shared_context = shared_context
        self._tool_executor = ToolExecutor(tools, shared_context=shared_context)

    def _get_model(self) -> RealtimeLLMModel:
        """Get the real-time LLM model to use."""
        if self._model_instance is None:
            self._model_instance = get_realtime_llm_model(
                self._model_name_to_load, self._config
            )
            if self._model_instance is None:
                raise AgentsException(
                    f"Failed to load real-time LLM model: {self._model_name_to_load or 'default'}"
                )
        return self._model_instance

    async def _pump_audio_to_llm(
        self,
        audio_input: StreamedAudioInput,
        llm_session: RealtimeSession,
    ) -> None:
        """Coroutine to continuously read from audio_input and send to LLM session.

        This method will continue pumping audio chunks until cancelled or the audio_input
        is closed. It does not use a None sentinel to detect the end of audio.
        """
        try:
            # Start an infinite loop to process audio chunks as they arrive
            while True:
                try:
                    # Get the next audio chunk, this will block until a chunk is available
                    audio_chunk = await audio_input.queue.get()

                    # Skip the None sentinel for backward compatibility
                    if audio_chunk is None:
                        logger.debug(
                            "Received None sentinel value (deprecated), ignoring"
                        )
                        audio_input.queue.task_done()
                        continue

                    # Check if we need to convert the audio format
                    if audio_chunk.dtype == np.float32:
                        audio_chunk_int16 = (audio_chunk * 32767).astype(np.int16)
                    elif audio_chunk.dtype == np.int16:
                        audio_chunk_int16 = audio_chunk
                    else:
                        logger.error(
                            f"Unsupported audio chunk dtype: {audio_chunk.dtype}"
                        )
                        raise ValueError(
                            f"Unsupported audio chunk dtype: {audio_chunk.dtype}"
                        )

                    # Send the audio chunk to the LLM session
                    await llm_session.send_audio_chunk(audio_chunk_int16)

                    # Mark the queue task as done
                    audio_input.queue.task_done()

                except asyncio.CancelledError:
                    # If we're cancelled, break out of the loop
                    logger.info("Audio pump task cancelled")
                    break

                except Exception as e:
                    # Log any errors but continue the loop to process the next chunk
                    logger.error(f"Error sending audio chunk: {e}", exc_info=True)
                    audio_input.queue.task_done()

                # Check if the input is closed
                if audio_input.is_closed:
                    logger.info("Audio input closed, stopping audio pump")
                    break

        except asyncio.CancelledError:
            logger.info("Audio pump task cancelled")
        except Exception as e:
            logger.error(f"Error in audio pump task: {e}", exc_info=True)
        finally:
            # Ensure we mark the queue as done for any pending tasks
            try:
                # Try to get any pending items from the queue and mark them as done
                while True:
                    try:
                        audio_input.queue.get_nowait()
                        audio_input.queue.task_done()
                    except asyncio.QueueEmpty:
                        break
            except Exception:
                pass

    async def _handle_tool_call(
        self,
        event: LLMToolCallEvent,
        llm_session: RealtimeSession,
        result: StreamedRealtimeResult,
        audio_input_queue: asyncio.Queue,
    ) -> None:
        """Execute a tool call and send the result back to the LLM."""
        try:
            logger.info(
                f"Handling tool call: {event.tool_name} (ID: {event.tool_call_id})"
            )
            # Add the tool call event to the result stream
            await result.push_llm_event(event)

            # Execute the tool and get the result
            tool_output_content = await self._tool_executor.execute(event)

            # Send the result back to the LLM
            await llm_session.send_tool_result(event.tool_call_id, tool_output_content)
            logger.info(
                f"Tool call {event.tool_name} (ID: {event.tool_call_id}) result sent."
            )

        except asyncio.CancelledError:
            logger.info(f"Tool call handler for {event.tool_name} cancelled.")
        except Exception as e:
            logger.error(
                f"Error handling tool call {event.tool_name} (ID: {event.tool_call_id}): {e}",
                exc_info=True,
            )
            # Try to send an error result back to the LLM
            error_content = json.dumps({"error": str(e), "tool_name": event.tool_name})
            try:
                await llm_session.send_tool_result(event.tool_call_id, error_content)
            except Exception as send_err:
                logger.error(
                    f"Failed to send error result for tool call {event.tool_call_id}: {send_err}"
                )
            # Also push a general error to the result stream
            await result.push_llm_event(
                LLMErrorEvent(
                    message=f"Tool execution error for {event.tool_name}: {str(e)}"
                )
            )

    async def _consume_llm_events(
        self,
        llm_session: RealtimeSession,
        result: StreamedRealtimeResult,
        audio_input_queue: asyncio.Queue,
    ) -> None:
        """Continuously receive events from LLM and process them."""
        tool_call_tasks: set[asyncio.Task] = set()
        try:
            async for event in llm_session.receive_events():
                if isinstance(event, LLMToolCallEvent):
                    task = asyncio.create_task(
                        self._handle_tool_call(
                            event, llm_session, result, audio_input_queue
                        )
                    )
                    tool_call_tasks.add(task)
                    task.add_done_callback(tool_call_tasks.discard)
                else:
                    # Push other events directly to the result stream
                    await result.push_llm_event(event)

                    # If it's an error or session end event, break out of the loop
                    if isinstance(event, LLMErrorEvent) or isinstance(
                        event, RealtimeEventSessionEnds
                    ):
                        break
        except asyncio.CancelledError:
            logger.info("LLM event consumer task cancelled")
        except Exception as e:
            logger.error(f"Error in LLM event consumer task: {e}", exc_info=True)
            await result.push_llm_event(
                LLMErrorEvent(message=f"LLM event consumer error: {str(e)}")
            )
        finally:
            # Wait for any outstanding tool calls to complete
            if tool_call_tasks:
                logger.info(
                    f"Waiting for {len(tool_call_tasks)} outstanding tool call(s) to complete..."
                )
                await asyncio.gather(*tool_call_tasks, return_exceptions=True)
                logger.info("All outstanding tool calls completed")

            # Signal completion to the result stream
            await result.signal_completion()

    async def run(self, audio_input: StreamedAudioInput) -> StreamedRealtimeResult:
        """Run the real-time voice pipeline.

        Args:
            audio_input: A StreamedAudioInput instance from which user audio is read.
                The pipeline will continue to process audio from this input until
                the pipeline is stopped or the audio_input is closed.

        Returns:
            A StreamedRealtimeResult instance to stream events from the pipeline.
        """
        model = self._get_model()
        result = StreamedRealtimeResult(config=self._config)

        # Ensure the model instance is SDKRealtimeLLM for this orchestrator logic
        if not isinstance(model, SDKRealtimeLLM):
            raise UserError(
                f"RealtimeVoicePipeline currently requires an SDKRealtimeLLM instance, got {type(model)}"
            )

        main_pipeline_task: asyncio.Task | None = None

        async def _pipeline_orchestrator():
            audio_pump_task: asyncio.Task | None = None
            event_consumer_task: asyncio.Task | None = None
            tool_call_tasks: set[asyncio.Task] = set()  # Track tool call tasks

            # Store audio input queue for potential use in _handle_tool_call (for nudge)
            audio_input_queue = audio_input.queue

            # Create the OpenAI SDK client
            # API key, base_url, organization should be part of SDKRealtimeLLM's config
            # and accessed here if needed, or SDKRealtimeLLM should expose a way to get client_kwargs
            client_kwargs = {}
            if (
                model._api_key
            ):  # Accessing protected member, consider a getter or passing config
                client_kwargs["api_key"] = model._api_key
            if model._base_url:  # Accessing protected member
                # The SDK's `AsyncOpenAI` client does not directly take `base_url` for realtime like websockets
                # Instead, the `client.beta.realtime.connect()` might take it if supported,
                # or it's part of the main client init for other APIs.
                # For realtime, the endpoint is usually fixed or configured via env for the SDK.
                # Let's assume for now the SDK handles this internally or via `model._base_url` if it were for the ws URI.
                # The SDKRealtimeLLM.create_session no longer uses its own base_url for connect.
                pass
            if model._organization:  # Accessing protected member
                client_kwargs["organization"] = model._organization

            client = AsyncOpenAI(**client_kwargs)

            try:
                logger.info(
                    f"Attempting to connect to OpenAI Realtime API with model: {model.model_name}"
                )
                async with client.beta.realtime.connect(
                    model=model.model_name
                ) as connection:
                    logger.info("Successfully connected to OpenAI Realtime API.")

                    turn_detection_setting = self._config.realtime_settings.get(
                        "turn_detection", "server_vad"
                    )
                    system_message_setting = self._config.realtime_settings.get(
                        "system_message"
                    )
                    assistant_voice_setting = self._config.realtime_settings.get(
                        "assistant_voice"
                    )

                    # Create and configure the session using the active connection
                    llm_session = await model.create_session(
                        connection=connection,  # Pass the active connection
                        tools=self._tools,
                        system_message=system_message_setting,
                        assistant_voice=assistant_voice_setting,
                        turn_detection=turn_detection_setting,
                    )

                    # Start receiver on the session now that it's fully configured
                    if isinstance(llm_session, SDKRealtimeSession):
                        await llm_session.start_receiver()
                    else:
                        # This case should ideally not happen if _get_model ensures SDKRealtimeLLM
                        logger.error(
                            "LLM session is not an SDKRealtimeSession, cannot start receiver."
                        )
                        raise AgentsException(
                            "Invalid LLM session type for SDK pipeline."
                        )

                    logger.info(
                        f"Realtime LLM session configured: {llm_session._session_id if llm_session else 'N/A'}"
                    )

                    audio_pump_task = asyncio.create_task(
                        self._pump_audio_to_llm(audio_input, llm_session)
                    )
                    event_consumer_task = asyncio.create_task(
                        self._consume_llm_events(llm_session, result, audio_input_queue)
                    )

                    done, pending = await asyncio.wait(
                        [audio_pump_task, event_consumer_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    for task in pending:
                        task.cancel()
                    for task in done:
                        if task.exception():
                            logger.error(
                                f"Exception in pipeline task: {task.exception()}",
                                exc_info=task.exception(),
                            )
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)

            except AgentsException as e:  # Catch our specific exceptions
                logger.error(
                    f"Agent-specific error during pipeline run: {e}", exc_info=True
                )
                await result.push_llm_event(LLMErrorEvent(message=str(e)))
            except Exception as e:
                logger.error(
                    f"Unexpected error during RealtimeVoicePipeline run: {e}",
                    exc_info=True,
                )
                await result.push_llm_event(
                    LLMErrorEvent(message=f"Pipeline run error: {str(e)}")
                )
            finally:
                active_tasks = []
                if audio_pump_task and not audio_pump_task.done():
                    audio_pump_task.cancel()
                    active_tasks.append(audio_pump_task)
                if event_consumer_task and not event_consumer_task.done():
                    event_consumer_task.cancel()
                    active_tasks.append(event_consumer_task)

                if active_tasks:
                    await asyncio.gather(*active_tasks, return_exceptions=True)

                if llm_session:
                    # llm_session.close() now only handles internal tasks, not the SDK connection
                    await llm_session.close()

                # The `async with` block for `client.beta.realtime.connect` will handle closing the SDK connection.
                logger.info("RealtimeVoicePipeline orchestrator finished.")
                await result.signal_completion()

        main_pipeline_task = asyncio.create_task(_pipeline_orchestrator())
        result.set_pipeline_task(main_pipeline_task)
        return result

    async def stop(self) -> None:
        """Stop the pipeline and clean up resources."""
        # This method does nothing since the pipeline is managed
        # by the context manager of the client.beta.realtime.connect()
        # When the pipeline task is cancelled, it will clean up properly
        pass


def get_realtime_llm_model(
    model_name: str | None, config: VoicePipelineConfig
) -> RealtimeLLMModel:
    # For now, this always returns SDKRealtimeLLM, ignoring provider logic in config for simplicity
    # A more robust implementation would check config.model_provider

    # Retrieve API key and other necessary params from a secure config or env
    # This is a placeholder; actual key management should be handled carefully.
    api_key = os.environ.get("OPENAI_API_KEY")
    # base_url and organization can also be retrieved if needed by SDKRealtimeLLM constructor

    return SDKRealtimeLLM(
        model_name=model_name or SDKRealtimeLLM.DEFAULT_MODEL_NAME, api_key=api_key
    )
