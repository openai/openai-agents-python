from typing import Literal

from ...models.default_models import get_default_model
from ...models.interface import Model, ModelProvider
from .any_llm_model import AnyLLMModel

DEFAULT_MODEL: str = f"openai/{get_default_model()}"


class AnyLLMProvider(ModelProvider):
    """A ModelProvider that routes model calls through any-llm.

    API keys are typically sourced from the provider-specific environment variables expected by
    any-llm, such as `OPENAI_API_KEY` or `OPENROUTER_API_KEY`. For custom wiring or explicit
    credentials, instantiate `AnyLLMModel` directly.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        api: Literal["responses", "chat_completions"] | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.api = api

    def get_model(self, model_name: str | None) -> Model:
        return AnyLLMModel(
            model=model_name or DEFAULT_MODEL,
            api_key=self.api_key,
            base_url=self.base_url,
            api=self.api,
        )
