import os
import re
from dataclasses import replace
from typing import Literal

from openai.types.shared.reasoning import Reasoning

from agents.model_settings import ModelSettings

OPENAI_DEFAULT_MODEL_ENV_VARIABLE_NAME = "OPENAI_DEFAULT_MODEL"

GPT5DefaultReasoningEffort = Literal["none", "low", "medium"]

_GPT_5_CHAT_MODELS = {
    "gpt-5-chat-latest",
    "gpt-5.1-chat-latest",
    "gpt-5.2-chat-latest",
    "gpt-5.3-chat-latest",
}

# Match a major version of at least five, an optional minor version, and a model-name boundary.
_GPT_5_OR_NEWER_MODEL_PATTERN = re.compile(r"gpt-0*(?:[5-9]|[1-9][0-9]+)(?:\.[0-9]+)?(?:-|\Z)")

_DEFAULT_REASONING_EFFORT_PATTERNS: tuple[
    tuple[re.Pattern[str], GPT5DefaultReasoningEffort],
    ...,
] = (
    (re.compile(r"^gpt-6-astra$"), "low"),
    # Some GPT-5 built-in tools do not support minimal effort.
    (re.compile(r"^gpt-5(?:-\d{4}-\d{2}-\d{2})?$"), "low"),
    (re.compile(r"^gpt-5\.1(?:-\d{4}-\d{2}-\d{2})?$"), "none"),
    (re.compile(r"^gpt-5\.2(?:-\d{4}-\d{2}-\d{2})?$"), "none"),
    (re.compile(r"^gpt-5\.2-pro(?:-\d{4}-\d{2}-\d{2})?$"), "medium"),
    (re.compile(r"^gpt-5\.2-codex$"), "low"),
    (re.compile(r"^gpt-5\.3-codex$"), "none"),
    (re.compile(r"^gpt-5\.4(?:-\d{4}-\d{2}-\d{2})?$"), "none"),
    (re.compile(r"^gpt-5\.4-pro(?:-\d{4}-\d{2}-\d{2})?$"), "medium"),
    (re.compile(r"^gpt-5\.4-mini(?:-\d{4}-\d{2}-\d{2})?$"), "none"),
    (re.compile(r"^gpt-5\.4-nano(?:-\d{4}-\d{2}-\d{2})?$"), "none"),
    (re.compile(r"^gpt-5\.5(?:-\d{4}-\d{2}-\d{2})?$"), "none"),
    (re.compile(r"^gpt-5\.6(?:-\d{4}-\d{2}-\d{2})?$"), "none"),
    (re.compile(r"^gpt-5\.6-sol(?:-\d{4}-\d{2}-\d{2})?$"), "none"),
    (re.compile(r"^gpt-5\.6-terra(?:-\d{4}-\d{2}-\d{2})?$"), "none"),
    (re.compile(r"^gpt-5\.6-luna(?:-\d{4}-\d{2}-\d{2})?$"), "none"),
)


def _get_default_reasoning_effort(model_name: str) -> GPT5DefaultReasoningEffort | None:
    for pattern, effort in _DEFAULT_REASONING_EFFORT_PATTERNS:
        if pattern.fullmatch(model_name):
            return effort
    return None


def gpt_5_reasoning_settings_required(model_name: str) -> bool:
    """
    Returns True if the model name is a GPT-5 model and reasoning settings are required.
    """
    return model_name.startswith("gpt-5") and model_name not in _GPT_5_CHAT_MODELS


def is_gpt_5_default() -> bool:
    """Return whether the default model uses the GPT-5-specific reasoning settings."""
    return gpt_5_reasoning_settings_required(get_default_model())


def get_default_model() -> str:
    """
    Returns the default model name.
    """
    return os.getenv(OPENAI_DEFAULT_MODEL_ENV_VARIABLE_NAME, "gpt-5.6-luna").lower()


def get_default_model_settings(model: str | None = None) -> ModelSettings:
    """Return low verbosity and any known reasoning effort for GPT-5 and newer reasoning models."""
    model_name = model if model is not None else get_default_model()
    if model_name in _GPT_5_CHAT_MODELS or not _GPT_5_OR_NEWER_MODEL_PATTERN.match(model_name):
        return ModelSettings()
    effort = _get_default_reasoning_effort(model_name)
    return ModelSettings(
        reasoning=Reasoning(effort=effort) if effort is not None else None,
        verbosity="low",
    )


def _omit_model_defaults(
    settings: ModelSettings,
    implicit_fields: set[str],
    *,
    reasoning: bool = False,
    verbosity: bool = False,
) -> ModelSettings:
    changes = {}  # type: dict[str, None]
    if reasoning and "reasoning" in implicit_fields:
        changes["reasoning"] = None
    if verbosity and "verbosity" in implicit_fields:
        changes["verbosity"] = None
    return replace(settings, **changes) if changes else settings
