from __future__ import annotations

import httpx
import json
from typing import Any, AsyncIterator, Literal, cast

from openai.types.responses import ResponseOutputMessage, ResponseOutputText

from ..model_settings import ModelSettings
from ..exceptions import AgentsException
from ..items import ModelResponse, TResponseInputItem, TResponseOutputItem, TResponseStreamEvent
from ..usage import Usage
from .interface import Model, ModelProvider
from .fake_id import FAKE_RESPONSES_ID

DEFAULT_MODEL: str = "llama3"


class OllamaHealthCheckError(AgentsException):
    """Ollama service health check failed exception"""


class OllamaProvider(ModelProvider):
    def __init__(
        self,
        *,
        base_url: str = "http://localhost:11434",
        default_model: str = DEFAULT_MODEL,
    ) -> None:
        """Create a new Ollama provider.

        Args:
            base_url: Base URL for Ollama API. If not provided, the default base URL will be used.
            default_model: The default model name to use. If not provided, the default model will be used.
        """
        self.base_url = base_url
        self.default_model = default_model
        self._client = None

    def get_model(self, model: str | Model) -> Model:
        """Get model instance with specified name.

        Args:
            model: Model name or model instance.

        Returns:
            Model: Model instance.
        """
        if isinstance(model, Model):
            return model

        return OllamaChatCompletionsModel(
            model_settings=ModelSettings(
                provider="ollama",
                ollama_base_url=self.base_url,
                ollama_default_model=model or self.default_model,
            )
        )

    def _get_client(self) -> httpx.AsyncClient:
        """Get HTTP client instance.

        Returns:
            httpx.AsyncClient: HTTP client instance.
        """
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(60.0)
            )
        return self._client


class OllamaChatCompletionsModel(Model):
    def __init__(self, model_settings: ModelSettings):
        """Initialize Ollama chat completion model.

        Args:
            model_settings: Model settings.
        """
        self.base_url = model_settings.ollama_base_url
        self.model = model_settings.ollama_default_model
        # Don't set base_url, use complete URL in requests instead
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0)
        )

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Any],
        output_schema: Any | None,
        handoffs: list[Any],
        tracing: Any
    ) -> ModelResponse:
        """Get model response.

        Args:
            system_instructions: System instructions.
            input: Input text or list of input items.
            model_settings: Model settings.
            tools: List of tools.
            output_schema: Output schema.
            handoffs: List of handoffs.
            tracing: Tracing settings.

        Returns:
            ModelResponse: Model response.
        """
        # Build request payload
        payload = {
            "model": self.model,
            "temperature": model_settings.temperature or 0.7,
            "max_tokens": model_settings.max_tokens or 1000,
            "messages": []
        }
        
        # Add system instructions
        if system_instructions:
            payload["messages"].append({
                "role": "system",
                "content": system_instructions
            })
        
        # Process input
        if isinstance(input, str):
            payload["messages"].append({
                "role": "user",
                "content": input
            })
        else:
            # Process complex input items
            for item in input:
                if isinstance(item, str):
                    payload["messages"].append({
                        "role": "user",
                        "content": item
                    })
                elif hasattr(item, "role") and hasattr(item, "content"):
                    payload["messages"].append({
                        "role": item.role,
                        "content": item.content
                    })
        
        # Send request to Ollama API
        try:
            # Use complete URL
            url = f"{self.base_url}/v1/chat/completions"
            print(f"Sending request to: {url}")
            print(f"Request payload: {json.dumps(payload, ensure_ascii=False)}")
            
            # Use OpenAI compatible API endpoint
            response = await self.client.post(url, json=payload)
            print(f"Response status code: {response.status_code}")
            
            response.raise_for_status()
            data = response.json()
            print(f"Response data: {json.dumps(data, ensure_ascii=False)}")
            
            # Extract response text
            response_text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            
            # Create usage statistics
            usage_data = data.get("usage", {})
            usage = Usage(
                requests=1,
                input_tokens=usage_data.get("prompt_tokens", 0),
                output_tokens=usage_data.get("completion_tokens", 0),
                total_tokens=usage_data.get("total_tokens", 0)
            )
            
            # Create response message
            message_item = ResponseOutputMessage(
                id=FAKE_RESPONSES_ID,
                content=[],
                role="assistant",
                type="message",
                status="completed",
            )
            
            # Add text content
            if response_text:
                message_item.content.append(
                    ResponseOutputText(
                        text=response_text,
                        type="output_text",
                        annotations=[]
                    )
                )
            
            # Return model response
            return ModelResponse(
                output=[message_item],
                usage=usage,
                referenceable_id=None
            )
            
        except httpx.HTTPError as e:
            print(f"HTTP error: {str(e)}")
            raise AgentsException(f"Ollama API error: {str(e)}") from e

    async def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Any],
        output_schema: Any | None,
        handoffs: list[Any],
        tracing: Any
    ) -> AsyncIterator[TResponseStreamEvent]:
        """Stream model response.

        Args:
            system_instructions: System instructions.
            input: Input text or list of input items.
            model_settings: Model settings.
            tools: List of tools.
            output_schema: Output schema.
            handoffs: List of handoffs.
            tracing: Tracing settings.

        Yields:
            TResponseStreamEvent: Response stream event.
        """
        # Will be implemented later
        raise NotImplementedError("Ollama stream_response not implemented yet")

    async def _verify_connection(self):
        """Verify connection with Ollama service.

        Raises:
            OllamaHealthCheckError: If connection verification fails.
        """
        try:
            url = f"{self.base_url}/api/tags"
            response = await self.client.get(url)
            response.raise_for_status()
            if not response.json().get("models"):
                raise OllamaHealthCheckError("No available models found")
        except (httpx.HTTPError, httpx.ConnectError) as e:
            raise OllamaHealthCheckError(f"Ollama service unavailable: {str(e)}") from e