from typing import Literal

from openai import AsyncOpenAI

from .models import _openai_shared
from .models.openai_agent_registration import (
    OpenAIAgentRegistrationConfig,
    set_default_openai_agent_registration_config,
)
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


def set_default_openai_responses_transport(transport: Literal["http", "websocket"]) -> None:
    if transport not in {"http", "websocket"}:
        raise ValueError(
            "Invalid OpenAI Responses transport. Expected one of: 'http', 'websocket'."
        )
    _openai_shared.set_default_openai_responses_transport(transport)


def set_default_openai_agent_registration(
    config: OpenAIAgentRegistrationConfig | None,
) -> None:
    set_default_openai_agent_registration_config(config)


def set_default_openai_harness(harness_id: str | None) -> None:
    if harness_id is None:
        set_default_openai_agent_registration_config(None)
        return

    set_default_openai_agent_registration_config(
        OpenAIAgentRegistrationConfig(harness_id=harness_id)
    )
