# ModelsLab integration for OpenAI Agents SDK
# Provides direct access to ModelsLab's comprehensive multi-modal APIs

from __future__ import annotations

import json
import time
import asyncio
from collections.abc import AsyncIterator
from typing import Any, Literal, cast, overload
from copy import deepcopy

import httpx
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails
from openai.types.chat import (
    ChatCompletionMessage,
    ChatCompletionMessageParam,
    ChatCompletionMessageFunctionToolCall,
    ChatCompletionMessageCustomToolCall,
)
from openai.types.chat.chat_completion_message_function_tool_call import Function
from pydantic import BaseModel

from agents.exceptions import ModelBehaviorError
from agents.agent_output import AgentOutputSchemaBase
from agents.handoffs import Handoff
from agents.items import ModelResponse, TResponseInputItem, TResponseStreamEvent
from agents.logger import logger
from agents.model_settings import ModelSettings
from agents.models.chatcmpl_converter import Converter
from agents.models.interface import Model, ModelTracing
from agents.tool import Tool
from agents.tracing import generation_span
from agents.tracing.span_data import GenerationSpanData
from agents.tracing.spans import Span
from agents.usage import Usage
from agents.util._json import _to_dump_compatible


class ModelsLabModel(Model):
    """
    ModelsLab integration for OpenAI Agents SDK.
    
    Provides direct access to ModelsLab's comprehensive multi-modal APIs including:
    - Uncensored chat completion (OpenAI-compatible)
    - Image generation (Flux, SDXL, community models)  
    - Video generation (CogVideoX, AnimateDiff)
    - Audio generation (TTS with emotion control)
    
    This is the first multi-modal provider for OpenAI Agents SDK, enabling agents
    to generate text, images, videos, and audio in their workflows.
    
    Usage:
        agent = Agent(
            model=ModelsLabModel(api_key="your-api-key"),
            instructions="You can generate any type of content."
        )
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://modelslab.com/api",
        chat_model: str = "ModelsLab/Llama-3.1-8b-Uncensored-Dare",
        timeout: int = 300,
    ):
        """
        Initialize ModelsLab model.
        
        Args:
            api_key: Your ModelsLab API key
            base_url: ModelsLab API base URL  
            chat_model: Default chat model for LLM responses
            timeout: Request timeout in seconds
        """
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.chat_model = chat_model
        self.timeout = timeout
        
        # Initialize HTTP client
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "OpenAI-Agents-ModelsLab/1.0",
            }
        )

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        previous_response_id: str | None = None,
        conversation_id: str | None = None,
        prompt: Any | None = None,
    ) -> ModelResponse:
        """Get response from ModelsLab API with multi-modal content detection."""
        
        with generation_span(
            model=self.chat_model,
            model_config=model_settings.to_json_dict() | {
                "base_url": self.base_url,
                "model_impl": "modelslab",
            },
            disabled=tracing.is_disabled(),
        ) as span_generation:
            
            # Convert input to message format
            messages = self._convert_input_to_messages(system_instructions, input)
            
            if tracing.include_data():
                span_generation.span_data.input = messages
            
            # Detect content type from conversation context
            content_type = self._detect_content_type(messages)
            
            logger.debug(f"ModelsLab: Detected content type: {content_type}")
            
            # Route to appropriate API based on content type
            if content_type == "image":
                response_data = await self._generate_image(messages[-1])
            elif content_type == "video":  
                response_data = await self._generate_video(messages[-1])
            elif content_type == "audio":
                response_data = await self._generate_audio(messages[-1])
            else:
                # Default to chat completion
                response_data = await self._chat_completion(
                    messages, model_settings, tools, handoffs
                )
            
            # Convert response to OpenAI format
            message = self._convert_response_to_message(response_data, content_type)
            
            if tracing.include_data():
                span_generation.span_data.output = [message.model_dump()]
            
            # Calculate usage (estimated for non-chat endpoints)
            usage = self._calculate_usage(response_data, content_type)
            span_generation.span_data.usage = usage.model_dump()
            
            # Convert to agent output items
            items = Converter.message_to_output_items(
                message, provider_data={"model": self.chat_model}
            )
            
            return ModelResponse(
                output=items,
                usage=usage,
                response_id=response_data.get("id"),
            )

    async def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        previous_response_id: str | None = None,
        conversation_id: str | None = None,
        prompt: Any | None = None,
    ) -> AsyncIterator[TResponseStreamEvent]:
        """Stream response from ModelsLab (falls back to non-streaming for multi-modal)."""
        
        # Convert input to messages 
        messages = self._convert_input_to_messages(system_instructions, input)
        content_type = self._detect_content_type(messages)
        
        # Only chat supports streaming, others fall back to regular response
        if content_type != "chat":
            logger.debug("ModelsLab: Multi-modal content detected, using non-streaming mode")
            response = await self.get_response(
                system_instructions, input, model_settings, tools, output_schema,
                handoffs, tracing, previous_response_id, conversation_id, prompt
            )
            
            # Simulate streaming by yielding the complete response
            for item in response.output:
                yield {"type": "response.content", "content": item}
                
            yield {
                "type": "response.completed", 
                "response": response,
                "usage": response.usage
            }
            return
        
        # Stream chat completion
        async for chunk in self._stream_chat_completion(
            messages, model_settings, tools, handoffs
        ):
            yield chunk

    def _convert_input_to_messages(
        self, 
        system_instructions: str | None, 
        input: str | list[TResponseInputItem]
    ) -> list[dict[str, Any]]:
        """Convert agent input to OpenAI message format."""
        
        messages = []
        
        if system_instructions:
            messages.append({
                "role": "system",
                "content": system_instructions
            })
        
        if isinstance(input, str):
            messages.append({
                "role": "user", 
                "content": input
            })
        else:
            # Convert complex input items to messages
            converted = Converter.items_to_messages(input, model=self.chat_model)
            messages.extend(converted)
        
        return messages

    def _detect_content_type(self, messages: list[dict[str, Any]]) -> str:
        """
        Detect requested content type from conversation context.
        
        Analyzes the last user message for content generation keywords.
        """
        if not messages:
            return "chat"
        
        last_message = messages[-1]
        content = str(last_message.get("content", "")).lower()
        
        # Image generation keywords
        image_keywords = [
            "generate an image", "create an image", "make an image", "draw", "paint",
            "generate a picture", "create a picture", "make a picture", "visualize",
            "generate artwork", "create artwork", "design a", "illustrate",
            "show me", "create visual", "generate visual"
        ]
        
        # Video generation keywords  
        video_keywords = [
            "generate a video", "create a video", "make a video", "animate",
            "generate animation", "create animation", "make animation", 
            "video of", "moving image", "generate clip", "create clip"
        ]
        
        # Audio generation keywords
        audio_keywords = [
            "generate audio", "create audio", "make audio", "speak this",
            "say this", "voice this", "read aloud", "text to speech", "tts",
            "generate speech", "create speech", "make speech", "narrate"
        ]
        
        # Check for content type indicators
        for keyword in video_keywords:
            if keyword in content:
                return "video"
                
        for keyword in image_keywords:
            if keyword in content:
                return "image"
                
        for keyword in audio_keywords:
            if keyword in content:
                return "audio"
        
        return "chat"

    async def _chat_completion(
        self,
        messages: list[dict[str, Any]], 
        model_settings: ModelSettings,
        tools: list[Tool],
        handoffs: list[Handoff]
    ) -> dict[str, Any]:
        """Call ModelsLab uncensored chat API (OpenAI-compatible)."""
        
        # Convert tools to OpenAI format
        openai_tools = []
        for tool in tools:
            openai_tools.append(Converter.tool_to_openai(tool))
        
        for handoff in handoffs:
            openai_tools.append(Converter.convert_handoff_tool(handoff))
        
        payload = {
            "model": self.chat_model,
            "messages": messages,
            "temperature": model_settings.temperature or 0.7,
            "max_tokens": model_settings.max_tokens or 2048,
            "tools": openai_tools if openai_tools else None,
            "stream": False,
        }
        
        response = await self.client.post(
            f"{self.base_url}/uncensored-chat/v1/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {self.api_key}"}
        )
        response.raise_for_status()
        
        data = response.json()
        
        # Extract the first choice message
        if data.get("choices") and len(data["choices"]) > 0:
            choice = data["choices"][0]
            return {
                "content": choice["message"]["content"],
                "role": "assistant",
                "tool_calls": choice["message"].get("tool_calls"),
                "usage": data.get("usage"),
                "id": data.get("id"),
            }
        
        raise ModelBehaviorError("No response choices returned from ModelsLab chat API")

    async def _generate_image(self, message: dict[str, Any]) -> dict[str, Any]:
        """Generate image using ModelsLab image generation API."""
        
        content = str(message.get("content", ""))
        
        # Extract prompt from content (remove generation request words)
        prompt = content
        for phrase in ["generate an image of", "create an image of", "make an image of", "draw", "show me"]:
            if phrase in prompt.lower():
                prompt = prompt.lower().replace(phrase, "").strip()
                break
        
        payload = {
            "key": self.api_key,
            "prompt": prompt,
            "model_id": "flux",
            "width": 1024,
            "height": 1024,
            "samples": 1,
            "num_inference_steps": 20,
            "guidance_scale": 7.5,
            "safety_checker": False,
            "enhance_prompt": True,
        }
        
        response = await self.client.post(
            f"{self.base_url}/v6/images/text2img",
            json=payload
        )
        response.raise_for_status()
        
        data = response.json()
        
        # Handle async processing
        if data.get("status") == "processing":
            task_id = data.get("id")
            data = await self._poll_async_task(task_id)
        
        if data.get("status") == "success" and data.get("output"):
            image_urls = data["output"]
            return {
                "content": f"Here's the generated image:\n{image_urls[0]}",
                "role": "assistant", 
                "image_urls": image_urls,
                "generation_type": "image",
                "id": data.get("id"),
            }
        
        raise ModelBehaviorError(f"Image generation failed: {data.get('message', 'Unknown error')}")

    async def _generate_video(self, message: dict[str, Any]) -> dict[str, Any]:
        """Generate video using ModelsLab video generation API."""
        
        content = str(message.get("content", ""))
        
        # Extract prompt from content
        prompt = content
        for phrase in ["generate a video of", "create a video of", "make a video of", "animate"]:
            if phrase in prompt.lower():
                prompt = prompt.lower().replace(phrase, "").strip()
                break
        
        payload = {
            "key": self.api_key,
            "prompt": prompt,
            "model": "cogvideo",
            "width": 720,
            "height": 480,
            "num_frames": 48,  # ~6 seconds at 8 FPS
            "num_inference_steps": 50,
            "guidance_scale": 6.0,
        }
        
        response = await self.client.post(
            f"{self.base_url}/v6/video/text2video",
            json=payload
        )
        response.raise_for_status()
        
        data = response.json()
        
        # Video generation is always async
        if data.get("status") == "processing":
            task_id = data.get("id")
            data = await self._poll_async_task(task_id)
        
        if data.get("status") == "success" and data.get("output"):
            video_urls = data["output"]
            return {
                "content": f"Here's the generated video:\n{video_urls[0]}",
                "role": "assistant",
                "video_urls": video_urls,
                "generation_type": "video", 
                "id": data.get("id"),
            }
        
        raise ModelBehaviorError(f"Video generation failed: {data.get('message', 'Unknown error')}")

    async def _generate_audio(self, message: dict[str, Any]) -> dict[str, Any]:
        """Generate audio/speech using ModelsLab TTS API."""
        
        content = str(message.get("content", ""))
        
        # Extract text to speak
        text = content
        for phrase in ["generate audio of", "speak this:", "say this:", "read aloud:"]:
            if phrase in text.lower():
                text = text.lower().replace(phrase, "").strip()
                break
        
        # Remove quotes if present
        text = text.strip('"').strip("'")
        
        payload = {
            "key": self.api_key,
            "text": text,
            "voice_id": "default",
            "language": "en",
            "speed": 1.0,
            "emotion": "neutral",
        }
        
        response = await self.client.post(
            f"{self.base_url}/v6/voice/text_to_speech",
            json=payload
        )
        response.raise_for_status()
        
        data = response.json()
        
        # Handle async processing if needed
        if data.get("status") == "processing":
            task_id = data.get("id")
            data = await self._poll_async_task(task_id)
        
        if data.get("status") == "success" and data.get("output"):
            audio_urls = data["output"]
            return {
                "content": f"Here's the generated audio:\n{audio_urls[0]}",
                "role": "assistant",
                "audio_urls": audio_urls,
                "generation_type": "audio",
                "id": data.get("id"),
            }
        
        raise ModelBehaviorError(f"Audio generation failed: {data.get('message', 'Unknown error')}")

    async def _poll_async_task(self, task_id: str) -> dict[str, Any]:
        """Poll async task until completion."""
        
        max_attempts = 60  # 5 minutes with 5-second intervals
        attempt = 0
        
        while attempt < max_attempts:
            payload = {"key": self.api_key}
            
            response = await self.client.post(
                f"{self.base_url}/v6/fetch/{task_id}",
                json=payload
            )
            response.raise_for_status()
            
            data = response.json()
            
            if data.get("status") == "success":
                return data
            elif data.get("status") == "failed":
                raise ModelBehaviorError(f"Task failed: {data.get('message')}")
            elif data.get("status") == "processing":
                await asyncio.sleep(5)  # Wait 5 seconds before next poll
                attempt += 1
            else:
                raise ModelBehaviorError(f"Unknown task status: {data.get('status')}")
        
        raise ModelBehaviorError("Task polling timeout - generation took too long")

    async def _stream_chat_completion(
        self,
        messages: list[dict[str, Any]],
        model_settings: ModelSettings, 
        tools: list[Tool],
        handoffs: list[Handoff]
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream chat completion (basic implementation)."""
        
        # Get non-streaming response and simulate streaming
        response_data = await self._chat_completion(messages, model_settings, tools, handoffs)
        
        # Split content into chunks for streaming simulation
        content = response_data.get("content", "")
        
        # Yield content in chunks
        chunk_size = 50
        for i in range(0, len(content), chunk_size):
            chunk = content[i:i + chunk_size]
            yield {
                "type": "response.content.delta",
                "delta": {"content": chunk}
            }
            await asyncio.sleep(0.05)  # Small delay for streaming effect
        
        # Yield completion
        yield {
            "type": "response.completed",
            "usage": response_data.get("usage")
        }

    def _convert_response_to_message(
        self, response_data: dict[str, Any], content_type: str
    ) -> ChatCompletionMessage:
        """Convert ModelsLab response to OpenAI ChatCompletionMessage format."""
        
        content = response_data.get("content", "")
        tool_calls = response_data.get("tool_calls")
        
        # Convert tool calls to proper format if present
        openai_tool_calls = None
        if tool_calls:
            openai_tool_calls = []
            for tool_call in tool_calls:
                openai_tool_calls.append(
                    ChatCompletionMessageFunctionToolCall(
                        id=tool_call["id"],
                        type="function",
                        function=Function(
                            name=tool_call["function"]["name"],
                            arguments=tool_call["function"]["arguments"]
                        )
                    )
                )
        
        return ChatCompletionMessage(
            content=content,
            role="assistant",
            tool_calls=openai_tool_calls,
        )

    def _calculate_usage(
        self, response_data: dict[str, Any], content_type: str
    ) -> Usage:
        """Calculate usage statistics."""
        
        # Use actual usage if available (from chat API)
        if response_data.get("usage"):
            usage_data = response_data["usage"]
            return Usage(
                requests=1,
                input_tokens=usage_data.get("prompt_tokens", 0),
                output_tokens=usage_data.get("completion_tokens", 0),
                total_tokens=usage_data.get("total_tokens", 0),
            )
        
        # Estimate usage for non-chat endpoints
        content = response_data.get("content", "")
        estimated_tokens = len(content.split()) * 1.3  # Rough token estimation
        
        return Usage(
            requests=1,
            input_tokens=0,  # Input tokens hard to estimate for multi-modal
            output_tokens=int(estimated_tokens),
            total_tokens=int(estimated_tokens),
        )
    
    async def __aenter__(self):
        """Async context manager entry."""
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.client.aclose()