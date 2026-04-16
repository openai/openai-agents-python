"""
Gemma Local Model Provider for OpenAI Agents SDK

This example shows how to use a local Gemma model with the OpenAI Agents SDK,
enabling completely offline agent execution.

Requirements:
    pip install transformers torch accelerate bitsandbytes

Environment:
    export HF_TOKEN=your_huggingface_token
    export GEMMA_MODEL=google/gemma-2b-it

Usage:
    See gemma_example.py for usage example
"""

import os
from typing import Optional, AsyncIterator
from dataclasses import dataclass

import torch
from transformers import AutoTokenizer, AutoModelForCausalLM, BitsAndBytesConfig

from agents import ModelProvider
from agents.items import ModelResponse, TResponseInputItem
from agents.models.interface import Model


@dataclass
class GemmaModelSettings:
    """Settings for Gemma model"""
    model_name: str = "google/gemma-2b-it"
    device: str = "auto"
    max_tokens: int = 512
    temperature: float = 0.7
    top_p: float = 0.95
    use_4bit: bool = True


class GemmaLocalModel(Model):
    """Local Gemma model implementation"""
    
    def __init__(self, provider: "GemmaLocalProvider"):
        self.provider = provider
    
    async def get_response(self, system_instructions, input, model_settings, tools, 
                          output_schema, handoffs, tracing, *, previous_response_id=None,
                          conversation_id=None, prompt=None) -> ModelResponse:
        """Get response from Gemma"""
        # Convert input to messages
        if isinstance(input, str):
            messages = [{"role": "user", "content": input}]
        else:
            messages = input
        
        return await self.provider._do_get_response(messages)
    
    def stream_response(self, system_instructions, input, model_settings, tools,
                       output_schema, handoffs, tracing, *, previous_response_id=None,
                       conversation_id=None, prompt=None) -> AsyncIterator[str]:
        """Stream response (simplified)"""
        # For simplicity, just yield the full response
        import asyncio
        
        async def _stream():
            response = await self.get_response(
                system_instructions, input, model_settings, tools,
                output_schema, handoffs, tracing,
                previous_response_id=previous_response_id,
                conversation_id=conversation_id,
                prompt=prompt
            )
            # Extract text from response
            if response.output:
                for item in response.output:
                    if hasattr(item, 'content'):
                        for content in item.content:
                            if hasattr(content, 'text'):
                                yield content.text
        
        return _stream()


class GemmaLocalProvider(ModelProvider):
    """
    Local Gemma model provider for OpenAI Agents SDK.
    
    This provider loads Gemma models locally using HuggingFace transformers,
    enabling completely offline agent execution with privacy preservation.
    
    Supports:
        - GPU inference with 4-bit quantization
        - CPU fallback
        - Streaming responses
    """
    
    def __init__(self, settings: Optional[GemmaModelSettings] = None):
        """
        Initialize Gemma local provider.
        
        Args:
            settings: Gemma model settings. Uses defaults if not provided.
        """
        self.settings = settings or GemmaModelSettings()
        self._tokenizer: Optional[AutoTokenizer] = None
        self._model: Optional[AutoModelForCausalLM] = None
        self._device: str = "cpu"
        self._load_model()
    
    def get_model(self, model_name: str | None) -> Model:
        """Get the Gemma model"""
        return GemmaLocalModel(self)
    
    def _load_model(self) -> None:
        """Load Gemma model and tokenizer"""
        if self._model is not None:
            return
            
        # Get HF token
        hf_token = os.getenv("HF_TOKEN")
        if not hf_token:
            raise ValueError(
                "HF_TOKEN environment variable required. "
                "Get token from https://huggingface.co/settings/tokens"
            )
        
        # Determine device
        if self.settings.device == "auto":
            if torch.cuda.is_available():
                self._device = "cuda"
                print(f"Using GPU: {torch.cuda.get_device_name(0)}")
            else:
                self._device = "cpu"
                print("GPU not available, using CPU")
        else:
            self._device = self.settings.device
        
        print(f"Loading {self.settings.model_name}...")
        
        # Load tokenizer
        self._tokenizer = AutoTokenizer.from_pretrained(
            self.settings.model_name,
            token=hf_token,
        )
        
        # Load model
        if self._device == "cuda" and self.settings.use_4bit:
            # 4-bit quantization for GPU
            quantization_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_compute_dtype=torch.float16,
            )
            self._model = AutoModelForCausalLM.from_pretrained(
                self.settings.model_name,
                quantization_config=quantization_config,
                device_map="auto",
                token=hf_token,
            )
        else:
            # Full precision or CPU
            self._model = AutoModelForCausalLM.from_pretrained(
                self.settings.model_name,
                torch_dtype=torch.float16 if self._device == "cuda" else torch.float32,
                token=hf_token,
            )
            if self._device == "cpu":
                self._model = self._model.to("cpu")
        
        print(f"Model loaded on {self._device}")
    
    def _format_messages(self, messages: list) -> str:
        """Format messages to Gemma chat format"""
        formatted = []
        
        for msg in messages:
            if isinstance(msg, dict):
                role = msg.get("role", "user")
                content = msg.get("content", "")
            else:
                # Handle OpenAI message objects
                role = getattr(msg, "role", "user")
                content = getattr(msg, "content", "")
            
            if role == "system":
                formatted.append(f"System: {content}")
            elif role == "user":
                formatted.append(f"User: {content}")
            elif role == "assistant":
                formatted.append(f"Assistant: {content}")
        
        formatted.append("Assistant:")
        return "\n".join(formatted)
    
    async def _do_get_response(self, messages: list) -> ModelResponse:
        """Internal method to get response from Gemma"""
        import time
        from openai.types.responses import ResponseOutputMessage, ResponseOutputText
        from openai.types.responses.response_usage import Usage
        
        # Format prompt
        prompt = self._format_messages(messages)
        
        # Tokenize
        inputs = self._tokenizer(prompt, return_tensors="pt")
        if self._device == "cuda":
            inputs = {k: v.to(self._device) for k, v in inputs.items()}
        
        # Generate
        start_time = time.time()
        with torch.no_grad():
            outputs = self._model.generate(
                **inputs,
                max_new_tokens=self.settings.max_tokens,
                temperature=self.settings.temperature,
                top_p=self.settings.top_p,
                do_sample=True,
                pad_token_id=self._tokenizer.eos_token_id,
            )
        
        # Decode
        response_text = self._tokenizer.decode(
            outputs[0][inputs["input_ids"].shape[1]:],
            skip_special_tokens=True
        )
        
        # Create ModelResponse
        output_message = ResponseOutputMessage(
            id=f"gemma-{int(time.time())}",
            content=[ResponseOutputText(text=response_text, annotations=[])],
            role="assistant",
            type="message",
        )
        
        return ModelResponse(
            output=[output_message],
            usage=Usage(
                prompt_tokens=inputs["input_ids"].shape[1],
                completion_tokens=outputs.shape[1] - inputs["input_ids"].shape[1],
                total_tokens=outputs.shape[1],
            ),
            response_id=f"gemma-{int(time.time())}",
        )
    
    async def aclose(self) -> None:
        """Clean up resources"""
        if self._model is not None:
            del self._model
            self._model = None
        if self._tokenizer is not None:
            del self._tokenizer
            self._tokenizer = None
        torch.cuda.empty_cache() if torch.cuda.is_available() else None


def create_gemma_provider(
    model_name: str = "google/gemma-2b-it",
    use_4bit: bool = True,
) -> GemmaLocalProvider:
    """
    Create a Gemma local provider with default settings.
    
    Args:
        model_name: Gemma model name
        use_4bit: Whether to use 4-bit quantization
        
    Returns:
        Configured GemmaLocalProvider
    """
    settings = GemmaModelSettings(
        model_name=model_name,
        use_4bit=use_4bit,
    )
    return GemmaLocalProvider(settings)
