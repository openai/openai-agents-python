import copy
import os
import re
from typing import Literal, Optional

from openai.types.shared.reasoning import Reasoning

from agents.model_settings import ModelSettings

OPENAI_DEFAULT_MODEL_ENV_VARIABLE_NAME = "OPENAI_DEFAULT_MODEL"

GPT5DefaultReasoningEffort = Literal["none", "low", "medium"]

# discourage directly accessing these constants
# use the get_default_model and get_default_model_settings() functions instead
_GPT_5_LOW_DEFAULT_MODEL_SETTINGS: ModelSettings = ModelSettings(
    # We chose "low" instead of "minimal" because some of the built-in tools
    # (e.g., file search, image generation, etc.) do not support "minimal"
    # If you want to use "minimal" reasoning effort, you can pass your own model settings
    reasoning=Reasoning(effort="low"),
    verbosity="low",
)
_GPT_5_NONE_DEFAULT_MODEL_SETTINGS: ModelSettings = ModelSettings(
    reasoning=Reasoning(effort="none"),
    verbosity="low",
)
_GPT_5_MEDIUM_DEFAULT_MODEL_SETTINGS: ModelSettings = ModelSettings(
    reasoning=Reasoning(effort="medium"),
    verbosity="low",
)
_GPT_5_TEXT_ONLY_DEFAULT_MODEL_SETTINGS: ModelSettings = ModelSettings(
    verbosity="low",
)

_GPT_5_CHAT_MODEL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^gpt-5-chat-latest$"),
    re.compile(r"^gpt-5\.1-chat-latest$"),
    re.compile(r"^gpt-5\.2-chat-latest$"),
    re.compile(r"^gpt-5\.3-chat-latest$"),
)

_GPT_5_DEFAULT_MODEL_SETTINGS_BY_REASONING_EFFORT: dict[
    GPT5DefaultReasoningEffort, ModelSettings
] = {
    "none": _GPT_5_NONE_DEFAULT_MODEL_SETTINGS,
    "low": _GPT_5_LOW_DEFAULT_MODEL_SETTINGS,
    "medium": _GPT_5_MEDIUM_DEFAULT_MODEL_SETTINGS,
}

_GPT_5_DEFAULT_REASONING_EFFORT_PATTERNS: tuple[
    tuple[re.Pattern[str], GPT5DefaultReasoningEffort],
    ...,
] = (
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
)


def _get_default_reasoning_effort(model_name: str) -> GPT5DefaultReasoningEffort | None:
    for pattern, effort in _GPT_5_DEFAULT_REASONING_EFFORT_PATTERNS:
        if pattern.fullmatch(model_name):
            return effort
    return None


def gpt_5_reasoning_settings_required(model_name: str) -> bool:
    """
    Returns True if the model name is a GPT-5 model and reasoning settings are required.
    """
    if any(pattern.fullmatch(model_name) for pattern in _GPT_5_CHAT_MODEL_PATTERNS):
        # Chat-latest aliases do not accept reasoning.effort.
        return False
    # matches any of gpt-5 models
    return model_name.startswith("gpt-5")


def is_gpt_5_default() -> bool:
    """
    Returns True if the default model is a GPT-5 model.
    This is used to determine if the default model settings are compatible with GPT-5 models.
    If the default model is not a GPT-5 model, the model settings are compatible with other models.
    """
    return gpt_5_reasoning_settings_required(get_default_model())


def get_default_model() -> str:
    """
    Returns the default model name.
    """
    return os.getenv(OPENAI_DEFAULT_MODEL_ENV_VARIABLE_NAME, "gpt-4.1").lower()


def get_default_model_settings(model: Optional[str] = None) -> ModelSettings:
    """
    Returns the default model settings.
    If the default model is a GPT-5 model, returns the GPT-5 default model settings.
    Otherwise, returns the legacy default model settings.
    """
    _model = model if model is not None else get_default_model()
    if gpt_5_reasoning_settings_required(_model):
        effort = _get_default_reasoning_effort(_model)
        if effort is not None:
            return copy.deepcopy(_GPT_5_DEFAULT_MODEL_SETTINGS_BY_REASONING_EFFORT[effort])
        # Keep the GPT-5 verbosity default, but omit reasoning.effort for
        # variants whose supported values are not confirmed yet.
        return copy.deepcopy(_GPT_5_TEXT_ONLY_DEFAULT_MODEL_SETTINGS)
    return ModelSettings()
