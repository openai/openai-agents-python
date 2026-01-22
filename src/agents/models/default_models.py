import copy
import os
from typing import Optional

from openai.types.shared.reasoning import Reasoning

from agents.model_settings import ModelSettings

OPENAI_DEFAULT_MODEL_ENV_VARIABLE_NAME = "OPENAI_DEFAULT_MODEL"

# discourage directly accessing this constant
# use the get_default_model and get_default_model_settings() functions instead
_GPT_5_DEFAULT_MODEL_SETTINGS: ModelSettings = ModelSettings(
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

_GPT_5_NONE_EFFORT_MODELS = {"gpt-5.1", "gpt-5.2"}


def _is_gpt_5_none_effort_model(model_name: str) -> bool:
    return model_name in _GPT_5_NONE_EFFORT_MODELS


def gpt_5_reasoning_settings_required(model_name: str) -> bool:
    """
    Returns True if the model name is a GPT-5 model and reasoning settings are required.
    """
    if model_name.startswith("gpt-5-chat"):
        # gpt-5-chat-latest does not require reasoning settings
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
        if _is_gpt_5_none_effort_model(_model):
            return copy.deepcopy(_GPT_5_NONE_DEFAULT_MODEL_SETTINGS)
        return copy.deepcopy(_GPT_5_DEFAULT_MODEL_SETTINGS)
    return ModelSettings()
