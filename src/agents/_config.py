from openai import AsyncOpenAI
from typing_extensions import Literal

from .models import _openai_shared
from .tracing import set_tracing_export_api_key


def set_default_openai_key(key: str, use_for_tracing: bool) -> None:
    _openai_shared.set_default_openai_key(key)

    if use_for_tracing:
        set_tracing_export_api_key(key)


def set_default_openai_client(client: AsyncOpenAI, use_for_tracing: bool) -> None:
    _openai_shared.set_default_openai_client(client)

    if use_for_tracing:
        set_tracing_export_api_key(client.api_key)


def set_default_openai_api(api: Literal["chat_completions", "responses"]) -> None:
    if api == "chat_completions":
        _openai_shared.set_use_responses_by_default(False)
    else:
        _openai_shared.set_use_responses_by_default(True)
