"""Unified config: env loading + OpenAI client construction + Chat Completions model.

All examples can simply use `from config import MODEL` to get the model instance.  # type: ignore[import-not-found]
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI

from agents import OpenAIChatCompletionsModel, set_tracing_disabled

load_dotenv(Path(__file__).parent / ".env")

# Compat fallback: openai-agents SDK expects OPENAI_API_KEY by default
if "SECAUTH_OPENAI_API_KEY" in os.environ and "OPENAI_API_KEY" not in os.environ:
    os.environ["OPENAI_API_KEY"] = os.environ["SECAUTH_OPENAI_API_KEY"]

BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
MODEL_NAME = os.getenv("OPENAI_MODEL", "deepseek-v4-pro")

# Disable trace reporting to OpenAI platform (DeepSeek / other compatible backends have no trace endpoint)
set_tracing_disabled(disabled=True)


def get_client() -> AsyncOpenAI:
    return AsyncOpenAI(base_url=BASE_URL)


# Ready for Agent use: model=MODEL
MODEL = OpenAIChatCompletionsModel(model=MODEL_NAME, openai_client=get_client())
