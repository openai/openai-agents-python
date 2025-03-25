from __future__ import annotations

import json
import time
import uuid
import re
from typing import Any, AsyncIterator, Dict, List, Literal, Union, cast, overload

import httpx
from openai import AsyncOpenAI, NOT_GIVEN, NotGiven
from openai.types.chat import ChatCompletion, ChatCompletionChunk, ChatCompletionMessage
from openai.types.completion_usage import CompletionUsage

from .. import _debug
from ..agent_output import AgentOutputSchema
from ..exceptions import AgentsException
from ..handoffs import Handoff
from ..items import ModelResponse, TResponseInputItem, TResponseStreamEvent
from ..logger import logger
from ..model_settings import ModelSettings
from ..tool import Tool
from ..tracing.span_data import GenerationSpanData
from ..tracing.spans import Span
from ..usage import Usage
from .interface import Model, ModelProvider, ModelTracing
from .openai_chatcompletions import OpenAIChatCompletionsModel

DEFAULT_MODEL = "llama3"

class OllamaAdapterException(AgentsException):
    """Ollama Adapter Exception"""

    def __init__(self, message: str):
        super().__init__(message)
    
    def __str__(self) -> str:
        return str(self.args[0])  # Use args[0] instead of message
    

class OllamaAsyncClient:
    """Adapts Ollama API to behave like OpenAI's AsyncOpenAI client"""
    
    def __init__(self, base_url: str = "http://localhost:11434"):
        self.base_url = base_url
        self.http_client = httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(60.0))
        self.chat = self.Chat(http_client=self.http_client, base_url=base_url)
    
    class Chat:
        """Simulates the chat level of OpenAI client"""
        def __init__(self, http_client: httpx.AsyncClient, base_url: str):
            self.http_client = http_client
            self.base_url = base_url
            self.completions = self.Completions(http_client=self.http_client, base_url=base_url)
        
        class Completions:
            """Simulates the chat.completions level of OpenAI client"""
            def __init__(self, http_client: httpx.AsyncClient, base_url: str):
                self.http_client = http_client
                self.base_url = base_url
                
            def _clean_not_given(self, obj: Any) -> Any:
                """Recursively clean NotGiven values"""
                if obj is NOT_GIVEN or isinstance(obj, NotGiven):
                    return None
                elif isinstance(obj, dict):
                    return {k: self._clean_not_given(v) for k, v in obj.items() if v is not NOT_GIVEN}
                elif isinstance(obj, list):
                    return [self._clean_not_given(item) for item in obj if item is not NOT_GIVEN]
                return obj

            def _extract_json_from_text(self, text: str, schema: dict = None) -> str:
                """Extract JSON content from text"""
                json_block_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', text)
                if json_block_match:
                    json_str = json_block_match.group(1).strip()
                    try:
                        json.loads(json_str)
                        return json_str
                    except json.JSONDecodeError:
                        logger.debug(f"JSON block parsing failed: {json_str[:100]}...")
                
                json_match = re.search(r'(\{[\s\S]*?\})', text)
                if json_match:
                    json_str = json_match.group(1).strip()
                    try:
                        json.loads(json_str)
                        return json_str
                    except json.JSONDecodeError:
                        logger.debug(f"JSON object parsing failed: {json_str[:100]}...")
                
                if schema and "properties" in schema:
                    properties = schema["properties"]
                    json_obj = {}
                    for prop_name, prop_info in properties.items():
                        prop_type = prop_info.get("type")
                        if prop_type in ("integer", "number"):
                            match = re.search(fr'(?:{prop_name}|{prop_name.title()})[^\d]*(\d+)', text)
                            if match:
                                try:
                                    json_obj[prop_name] = int(match.group(1)) if prop_type == "integer" else float(match.group(1))
                                except ValueError:
                                    logger.debug(f"Number conversion failed: {match.group(1)}")
                        elif prop_type == "boolean":
                            if re.search(fr'(?:{prop_name}|{prop_name.title()}).*?(?:true|True|yes|Yes)', text):
                                json_obj[prop_name] = True
                            elif re.search(fr'(?:{prop_name}|{prop_name.title()}).*?(?:false|False|no|No)', text):
                                json_obj[prop_name] = False
                        elif prop_type == "string":
                            match = re.search(fr'(?:{prop_name}|{prop_name.title()})[^\"\']*([\"\'])(.*?)\1', text)
                            if match:
                                json_obj[prop_name] = match.group(2)
                    if json_obj:
                        return json.dumps(json_obj)
                
                logger.debug(f"Unable to extract JSON, using original text: {text[:100]}...")
                return text

                
            async def create(self, **kwargs) -> Union[ChatCompletion, AsyncIterator[ChatCompletionChunk]]:
                """Simulates OpenAI's chat.completions.create method, supporting tool calls"""
                cleaned_kwargs = self._clean_not_given(kwargs)
                model = cleaned_kwargs.get("model", DEFAULT_MODEL)
                messages = cleaned_kwargs.get("messages", [])
                tools = cleaned_kwargs.get("tools", [])  # Get tools parameter
                stream = cleaned_kwargs.get("stream", False)
                temperature = cleaned_kwargs.get("temperature", 0.7)
                max_tokens = cleaned_kwargs.get("max_tokens", 2048)
                response_format = cleaned_kwargs.get("response_format", {})

                needs_json = response_format and response_format.get("type") == "json_schema"
                json_schema = response_format.get("json_schema", {}).get("schema") if needs_json else None
                # Check if any handoff tools exist (by description starting with "Handoff to")
                has_handoff_tools = any(
                    tool.get("type") == "function" and 
                    tool.get("function", {}).get("description", "").startswith("Handoff to")
                    for tool in tools
                )
                if has_handoff_tools and tools:
                    handoff_instruction = (
                        "\n\nYou may use the provided handoff tools to delegate the conversation to another specialized agent. "
                        "When a task requires delegation, please use a tool function whose name starts with 'Handoff to' and supply the required parameters."
                    )
                    for msg in messages:
                        if msg.get("role") == "system":
                            msg["content"] += handoff_instruction
                            break
                    else:
                        messages.insert(0, {"role": "system", "content": f"Please process the user request. {handoff_instruction}"})

                # Construct payload, including tools
                payload = {
                    "model": model,
                    "messages": messages,
                    "options": {"temperature": temperature, "num_predict": max_tokens},
                    "tools": tools
                }
                if needs_json:
                    payload["options"]["format"] = "json"

                if stream:
                    return self._create_stream(payload, needs_json, json_schema)

                url = f"{self.base_url}/v1/chat/completions"
                try:
                    response = await self.http_client.post(url, json=payload, timeout=60.0)
                    response.raise_for_status()
                    data = response.json()

                    # Handle JSON format
                    if needs_json and "choices" in data and data["choices"]:
                        content = data["choices"][0]["message"]["content"]
                        json_content = self._extract_json_from_text(content, json_schema)
                        try:
                            parsed_json = json.loads(json_content)
                            data["choices"][0]["message"]["content"] = json.dumps(parsed_json)
                        except json.JSONDecodeError:
                            logger.debug(f"Non-streaming response JSON parsing failed: {content[:100]}...")
                    return ChatCompletion.model_validate(data)
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 404:
                        url = f"{self.base_url}/api/chat"
                        response = await self.http_client.post(url, json=payload, timeout=60.0)
                        response.raise_for_status()
                        buffer = ""
                        last_message = None
                        for line in response.text.strip().split('\n'):
                            if not line.strip():
                                continue
                            try:
                                data = json.loads(line)
                                if "message" in data and "content" in data["message"]:
                                    buffer += data["message"]["content"]
                                    last_message = data
                            except json.JSONDecodeError:
                                logger.warning(f"Non-streaming response line parsing failed: {line[:100]}...")
                        if not last_message:
                            raise OllamaAdapterException("No valid message found")
                        final_data = last_message.copy()
                        final_data["message"]["content"] = self._extract_json_from_text(buffer, json_schema) if needs_json else buffer
                        return self._convert_to_chat_completion(final_data)
                    else:
                        raise OllamaAdapterException(f"API error: {str(e)}") from e


            def _convert_to_chat_completion(self, ollama_response: Dict[str, Any]) -> ChatCompletion:
                """Convert Ollama response to ChatCompletion format"""
                message = ollama_response.get("message", {"content": ollama_response.get("response", "")})
                response_text = message.get("content", "")
                prompt_text = str(ollama_response.get("prompt", ""))
                prompt_tokens = len(prompt_text.split())
                completion_tokens = len(response_text.split())
                return ChatCompletion(
                    id=str(uuid.uuid4()),
                    choices=[{"finish_reason": "stop", "index": 0, "message": ChatCompletionMessage(
                        content=response_text, role="assistant", function_call=None, tool_calls=None
                    ), "logprobs": None}],
                    created=int(time.time()),
                    model=ollama_response.get("model", ""),
                    object="chat.completion",
                    system_fingerprint=None,
                    usage=CompletionUsage(
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        total_tokens=prompt_tokens + completion_tokens
                    )
                )
            
            async def _create_stream(self, payload: dict, needs_json: bool = False, json_schema: dict = None) -> AsyncIterator[ChatCompletionChunk]:
                """Create a streaming response, supporting tool calls"""
                url = f"{self.base_url}/v1/chat/completions"
                stream_payload = payload.copy()
                stream_payload["stream"] = True
                try:
                    response = await self.http_client.post(url, json=stream_payload, timeout=60.0)
                    if response.status_code == 404:
                        url = f"{self.base_url}/api/chat"
                    else:
                        response.raise_for_status()
                        function_calls = {}  # Store streaming tool calls
                        async for line in response.aiter_lines():
                            if not line.strip() or line.strip() == "data: [DONE]":
                                continue
                            data = line[len("data: "):] if line.startswith("data: ") else line
                            try:
                                chunk = ChatCompletionChunk.model_validate(json.loads(data))
                                yield chunk

                                # Handle tool call increments
                                if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.tool_calls:
                                    for tc_delta in chunk.choices[0].delta.tool_calls:
                                        index = tc_delta.index
                                        if index not in function_calls:
                                            function_calls[index] = {
                                                "id": tc_delta.id,
                                                "type": "function",
                                                "function": {"name": "", "arguments": ""}
                                            }
                                        if tc_delta.function:
                                            function_calls[index]["function"]["name"] += tc_delta.function.name or ""
                                            function_calls[index]["function"]["arguments"] += tc_delta.function.arguments or ""
                            except json.JSONDecodeError:
                                logger.warning(f"Streaming response chunk parsing failed: {data[:100]}...")
                except httpx.HTTPStatusError as e:
                    if e.response.status_code != 404:
                        raise OllamaAdapterException(f"Streaming API error: {str(e)}") from e

                async with self.http_client.stream("POST", url, json=payload, timeout=60.0) as http_response:
                    http_response.raise_for_status()
                    buffer = ""
                    async for chunk in http_response.aiter_text():
                        if not chunk.strip():
                            continue
                        for line in chunk.strip().split('\n'):
                            if not line.strip():
                                continue
                            try:
                                data = json.loads(line)
                                content = data.get("message", {}).get("content", "") or data.get("response", "")
                                if content:
                                    buffer += content
                                    yield ChatCompletionChunk(
                                        id=str(uuid.uuid4()),
                                        choices=[{"delta": {"content": content, "role": "assistant"}, "finish_reason": None, "index": 0, "logprobs": None}],
                                        created=int(time.time()),
                                        model=data.get("model", ""),
                                        object="chat.completion.chunk",
                                        system_fingerprint=None,
                                        usage=None
                                    )
                                if data.get("done", False):
                                    if needs_json and buffer:
                                        json_content = self._extract_json_from_text(buffer, json_schema)
                                        try:
                                            json.loads(json_content)
                                        except json.JSONDecodeError:
                                            json_content = f'{{"result": {json.dumps(buffer.strip())}}}'
                                        yield ChatCompletionChunk(
                                            id=str(uuid.uuid4()),
                                            choices=[{"delta": {"content": json_content, "role": "assistant"}, "finish_reason": None, "index": 0, "logprobs": None}],
                                            created=int(time.time()),
                                            model=data.get("model", ""),
                                            object="chat.completion.chunk",
                                            system_fingerprint=None,
                                            usage=None
                                        )
                                    prompt_tokens = len(str(data.get("prompt", "")).split())
                                    completion_tokens = len(buffer.split())
                                    yield ChatCompletionChunk(
                                        id=str(uuid.uuid4()),
                                        choices=[{"delta": {}, "finish_reason": "stop", "index": 0, "logprobs": None}],
                                        created=int(time.time()),
                                        model=data.get("model", ""),
                                        object="chat.completion.chunk",
                                        system_fingerprint=None,
                                        usage=CompletionUsage(
                                            prompt_tokens=prompt_tokens,
                                            completion_tokens=completion_tokens,
                                            total_tokens=prompt_tokens + completion_tokens
                                        )
                                    )
                            except json.JSONDecodeError:
                                logger.warning(f"Streaming response chunk parsing failed: {line[:100]}...")

class OllamaProvider(ModelProvider):
    def __init__(self, *, base_url: str = "http://localhost:11434", default_model: str = DEFAULT_MODEL):
        self.base_url = base_url
        self.default_model = default_model
        
    def get_model(self, model: str | Model) -> Model:
        if isinstance(model, Model):
            return model
        ollama_client = OllamaAsyncClient(base_url=self.base_url)
        return OpenAIChatCompletionsModel(model=model or self.default_model, openai_client=ollama_client)
        
    async def check_health(self) -> bool:
        try:
            client = httpx.AsyncClient(timeout=httpx.Timeout(5.0))
            response = await client.get(f"{self.base_url}/api/tags")
            response.raise_for_status()
            return bool(response.json().get("models"))
        except Exception:
            return False