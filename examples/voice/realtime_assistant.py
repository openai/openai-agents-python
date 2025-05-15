#!/usr/bin/env python
"""
This example shows how to use the RealtimeVoicePipeline with continuous audio streaming
using a real microphone for input and speakers for output.

Requirements:
1. OpenAI API key with APPROVED access to the gpt-4o-realtime-preview model
2. Python 3.10+
3. Required packages: openai, numpy, sounddevice
4. A working microphone and speaker setup.

Important Note:
   Access to gpt-4o-realtime-preview requires special approval from OpenAI.
   If you receive WebSocket connection closures with code 1000, it's likely that your
   API key does not have approved access to the model yet.

   Visit https://platform.openai.com/docs/guides/realtime for more information
   on applying for access to the realtime API.

Usage:
    python realtime_assistant.py
"""

import asyncio
import logging
import os
import time
from typing import Dict, Any
from dataclasses import dataclass

import numpy as np
import sounddevice as sd  # For microphone and speaker I/O

from agents.voice import (
    RealtimeVoicePipeline,
    StreamedAudioInput,
    VoicePipelineConfig,
    VoiceStreamEvent,
    VoiceStreamEventLifecycle,
    VoiceStreamEventToolCall,
    VoiceStreamEventError,
    VoiceStreamEventAudio,
)
from agents.tool import function_tool, Tool
from agents.voice.models.sdk_realtime import SDKRealtimeLLM
from agents.run_context import RunContextWrapper

# Import the new event types from our SDK
from agents.voice.realtime.model import (
    RealtimeEventResponseDone,
    RealtimeEventRateLimitsUpdated,
    RealtimeEventInputAudioTranscriptionDelta,
    RealtimeEventInputAudioTranscriptionCompleted,
)
import dotenv

# Load environment variables
dotenv.load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("realtime_assistant")


# Define a dataclass for our application context
@dataclass
class MyAppContext:
    """A simple context for the realtime voice assistant example."""

    user_name: str
    interaction_count: int = 0


# Define some sample tools
@function_tool
def get_weather(city: str) -> Dict[str, Any]:
    """Gets the current weather for a given city."""
    logger.info(f"Getting weather for {city}")
    return {"temperature": 22, "condition": "sunny", "humidity": 60}


@function_tool
def get_time(timezone: str = "UTC") -> Dict[str, Any]:
    """Gets the current time in the specified timezone."""
    logger.info(f"Getting time for timezone {timezone}")
    return {"time": time.strftime("%H:%M:%S", time.gmtime()), "timezone": timezone}


# Define a context-aware tool
@function_tool
def greet_user_and_count(context: RunContextWrapper[MyAppContext]) -> str:
    """Greets the user by name and counts interactions."""
    logger.info(f"greet_user_and_count called with context: {context}")
    # Increment the interaction count
    context.context.interaction_count += 1

    logger.info(
        f"Greeting user: {context.context.user_name}, "
        f"Interaction count: {context.context.interaction_count}"
    )

    return f"Hello {context.context.user_name}! This is interaction number {context.context.interaction_count}."


# Another context-aware tool that reads but doesn't modify the context
@function_tool
def get_user_details(context: RunContextWrapper[MyAppContext]) -> Dict[str, Any]:
    """Gets the user's details from the context."""
    logger.info(f"get_user_details called with context: {context}")

    logger.info(
        f"Returning user details: name={context.context.user_name}, count={context.context.interaction_count}"
    )
    return {
        "user_name": context.context.user_name,
        "interaction_count": context.context.interaction_count,
    }


# Get the OpenAI API key from environment variables
api_key = os.environ.get("OPENAI_API_KEY")
if not api_key:
    logger.error("OPENAI_API_KEY environment variable is not set.")
    logger.error(
        "Please set your OpenAI API key with access to gpt-4o-realtime-preview model."
    )
    logger.error("You can get API keys from https://platform.openai.com/api-keys")
    exit(1)
else:
    # Show first and last 4 characters of the key for debugging
    masked_key = f"{api_key[:8]}********************"
    logger.info(f"Using OpenAI API key: {masked_key}")


# Audio settings
INPUT_SAMPLE_RATE = 24000  # OpenAI Realtime API expects 24kHz input for pcm16
OUTPUT_SAMPLE_RATE = 24000  # OpenAI TTS audio is 24kHz for pcm16
CHANNELS = 1
INPUT_DTYPE = "int16"  # Microphone input type
OUTPUT_DTYPE = np.int16  # Speaker output type, OpenAI sends int16 PCM
CHUNK_DURATION_S = 0.1  # Send audio in 100ms chunks
INPUT_CHUNK_SIZE = int(INPUT_SAMPLE_RATE * CHUNK_DURATION_S)

# Buffer time (seconds) after last assistant audio chunk before we resume mic capture
ASSISTANT_AUDIO_SILENCE_BUFFER_S = 0.3


async def main():
    logger.info("Initializing RealtimeVoicePipeline...")

    # Create the SDK-based OpenAI realtime model
    model = SDKRealtimeLLM(
        model_name="gpt-4o-realtime-preview",
        api_key=api_key,
    )

    # Create an audio input and pipeline config with server-side VAD
    config = VoicePipelineConfig(
        realtime_settings={
            "turn_detection": "server_vad",  # Use server-side VAD
            "assistant_voice": "alloy",
            "system_message": "You are a helpful assistant that responds concisely. You can use the greet_user_and_count tool to greet the user by name and the get_user_details tool to retrieve information about the user.",
            # Enable server-side noise / echo reduction
            "input_audio_noise_reduction": {},
        }
    )
    input_stream = StreamedAudioInput()

    # Create our application context
    app_context = MyAppContext(user_name="Anurag", interaction_count=0)

    # Create the realtime pipeline with shared context
    pipeline = RealtimeVoicePipeline(
        model=model,
        tools=[get_weather, get_time, greet_user_and_count, get_user_details],
        config=config,
        shared_context=app_context,  # Pass the context to the pipeline
    )

    # Track events and errors
    event_count = 0
    error_occurred = False
    should_continue_streaming = True  # Controls the main audio streaming loop

    # This example simulates a "Push-to-Talk" interface
    # The push_to_talk_active flag controls when audio is being sent
    push_to_talk_active = asyncio.Event()  # Use an asyncio.Event for push-to-talk state

    # Timestamp of the most recent assistant audio chunk that was played
    last_assistant_audio_ts: float = 0.0

    # Function to handle microphone input in a separate task
    async def mic_input_loop():
        nonlocal should_continue_streaming, error_occurred
        logger.info("Starting microphone input loop...")
        try:
            with sd.InputStream(
                samplerate=INPUT_SAMPLE_RATE, channels=CHANNELS, dtype=INPUT_DTYPE
            ) as mic:
                logger.info(
                    f"Microphone opened. Default input device: {sd.query_devices(kind='input')['name']}"
                )
                while should_continue_streaming:
                    # Wait until push-to-talk is active
                    await push_to_talk_active.wait()

                    # Check if enough data is available to read our desired chunk size
                    if mic.read_available >= INPUT_CHUNK_SIZE:
                        data, overflowed = mic.read(INPUT_CHUNK_SIZE)
                        if overflowed:
                            logger.warning("Microphone input overflowed!")

                        # Only forward to server if enough time has passed since assistant spoke
                        if (
                            time.time() - last_assistant_audio_ts
                        ) >= ASSISTANT_AUDIO_SILENCE_BUFFER_S:
                            if data.size > 0:
                                logger.debug(
                                    f"Forwarding {data.size} samples to server (PTT active)."
                                )
                                await input_stream.queue.put(data.astype(np.int16))
                        else:
                            # Discard or optionally monitor but don't send
                            logger.debug(
                                "Discarding mic samples to avoid echo (within buffer window)."
                            )
                    else:
                        # Not enough data yet, yield control briefly
                        await asyncio.sleep(
                            0.001
                        )  # Sleep for a very short duration (1ms)

        except sd.PortAudioError as pae:
            logger.error(f"PortAudioError in microphone loop: {pae}")
            logger.error(
                "This might be due to no microphone being available or permissions issues."
            )
            logger.error(f"Available input devices: {sd.query_devices(kind='input')}")
            error_occurred = True
        except Exception as e:
            logger.error(f"Error in microphone input loop: {e}", exc_info=True)
            error_occurred = True
        finally:
            logger.info("Microphone input loop ended.")

    # Run the pipeline
    try:
        # Initialize and start the speaker output stream
        speaker_stream = sd.OutputStream(
            samplerate=OUTPUT_SAMPLE_RATE, channels=CHANNELS, dtype=OUTPUT_DTYPE
        )
        speaker_stream.start()
        logger.info(
            f"Speaker output stream started. Default output device: {sd.query_devices(kind='output')['name']}"
        )

        # Start the pipeline
        result = await pipeline.run(input_stream)
        logger.info("Pipeline started successfully. Listening for events...")

        # Start the microphone input task
        mic_task = asyncio.create_task(mic_input_loop())

        # Simulate push-to-talk actions with a timer
        async def toggle_push_to_talk_simulation():
            nonlocal should_continue_streaming
            await asyncio.sleep(2)  # Initial delay before first interaction
            try:
                while should_continue_streaming:
                    logger.info(
                        "🎤 Press Enter to START simulated push-to-talk, or type 'q' to quit..."
                    )
                    action = await asyncio.to_thread(
                        input, ""
                    )  # Non-blocking input for demo
                    if action.lower() == "q":
                        should_continue_streaming = False
                        push_to_talk_active.set()  # Unblock mic loop if it was waiting
                        break

                    logger.info("🎤 PUSH-TO-TALK: ON (Recording from microphone...)")
                    push_to_talk_active.set()  # Signal mic_input_loop to send audio

                    logger.info("🎤 Press Enter to STOP simulated push-to-talk...")
                    await asyncio.to_thread(input, "")
                    logger.info("🎤 PUSH-TO-TALK: OFF (Stopped recording)")
                    push_to_talk_active.clear()  # Signal mic_input_loop to stop sending

                    # Optional: Send a commit if manual VAD was used (not in this example)
                    # if config.realtime_settings.get("turn_detection") == "manual":
                    #     await pipeline.commit_audio_buffer() # Assuming pipeline exposes this

            except asyncio.CancelledError:
                logger.info("Push-to-talk simulation cancelled.")
            finally:
                logger.info("Push-to-talk simulation ended.")
                push_to_talk_active.set()  # Ensure mic loop can exit if it was waiting

        # Start the push-to-talk simulation
        ptt_simulation_task = asyncio.create_task(toggle_push_to_talk_simulation())

        # Process events from the pipeline
        async for event in result.stream():
            if not should_continue_streaming:
                break
            event_count += 1

            if isinstance(event, VoiceStreamEventLifecycle):
                logger.info(f"Lifecycle event: {event.event}")
                if event.event == "session_ended":
                    logger.info("Real-time session ended.")
                    should_continue_streaming = False
                    break

            elif isinstance(event, VoiceStreamEventAudio):
                if event.data is not None and speaker_stream:
                    logger.info(f"Received audio: {len(event.data)} bytes. Playing...")
                    speaker_stream.write(event.data.astype(OUTPUT_DTYPE))
                    # Update last audio timestamp for mic gating
                    last_assistant_audio_ts = time.time()
                else:
                    logger.info(
                        "Received empty audio data or speaker stream not active."
                    )

            elif isinstance(event, VoiceStreamEventToolCall):
                logger.info(f"Tool call: {event.tool_name}({event.arguments})")

            elif isinstance(event, VoiceStreamEventError):
                logger.error(f"Pipeline Error: {event.error}")
                error_occurred = True
                if "1000" in str(event.error):
                    logger.warning("WebSocket was closed normally (code 1000).")
                    logger.warning(
                        "This typically happens when your API key lacks access to the gpt-4o-realtime-preview model."
                    )
                should_continue_streaming = False
                break

            # Handle newly defined RealtimeEvent types that are passed through StreamedRealtimeResult
            elif isinstance(event, RealtimeEventResponseDone):
                logger.info(f"Assistant response for item '{event.item_id}' is done.")

            elif isinstance(event, RealtimeEventRateLimitsUpdated):
                logger.info(f"Rate limits updated by server: {event.data}")

            elif isinstance(event, RealtimeEventInputAudioTranscriptionDelta):
                logger.info(
                    f"[TRANSCRIPTION DELTA] item {event.item_id} idx {event.content_index}: {event.delta}"
                )

            elif isinstance(event, RealtimeEventInputAudioTranscriptionCompleted):
                logger.info(
                    f"[TRANSCRIPTION COMPLETED] item {event.item_id}: {event.transcript}"
                )

            else:
                logger.info(f"Unknown Event: {event.type} - {event}")

        # Wait for the push-to-talk simulation to complete or be cancelled
        if not ptt_simulation_task.done():
            ptt_simulation_task.cancel()
            try:
                await ptt_simulation_task
            except asyncio.CancelledError:
                pass

        logger.info(f"Total events processed: {event_count}")

        # Print the final interaction count from the context
        logger.info(f"Final interaction count: {app_context.interaction_count}")

        # Provide troubleshooting information if needed
        if error_occurred or event_count <= 1:  # <=1 because turn_started is an event
            logger.error(f"Error occurred: {error_occurred}")

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt detected, stopping...")
    except Exception as e:
        logger.error(f"Main loop error: {e}", exc_info=True)
    finally:
        logger.info("Cleaning up resources...")
        should_continue_streaming = False  # Signal all loops to stop

        if mic_task and not mic_task.done():
            push_to_talk_active.set()  # Unblock mic_input_loop if waiting
            mic_task.cancel()
            try:
                await mic_task
            except asyncio.CancelledError:
                pass
            logger.info("Microphone task stopped.")

        if speaker_stream:
            speaker_stream.stop()
            speaker_stream.close()
            logger.info("Speaker stream closed.")

        if input_stream and not input_stream.is_closed:
            await input_stream.close()
            logger.info("Audio input stream closed.")

        logger.info("Application shutdown complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nExiting by user request.")
