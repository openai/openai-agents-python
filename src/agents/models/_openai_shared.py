from __future__ import annotations

from openai import AsyncOpenAI

_default_openai_key: str | None = None
_default_openai_client: AsyncOpenAI | None = None
_use_responses_by_default: bool = True
_user_agent_override: str | None = None


def set_default_openai_key(key: str) -> None:
    global _default_openai_key
    _default_openai_key = key


def get_default_openai_key() -> str | None:
    return _default_openai_key


def set_default_openai_client(client: AsyncOpenAI) -> None:
    global _default_openai_client
    _default_openai_client = client


def get_default_openai_client() -> AsyncOpenAI | None:
    return _default_openai_client


def set_use_responses_by_default(use_responses: bool) -> None:
    global _use_responses_by_default
    _use_responses_by_default = use_responses


def get_use_responses_by_default() -> bool:
    return _use_responses_by_default


def set_user_agent_override(user_agent: str | None) -> None:
    global _user_agent_override
    _user_agent_override = user_agent
