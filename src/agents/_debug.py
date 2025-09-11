import os


def _debug_flag_enabled(flag: str, default: bool = False) -> bool:
    flag_value = os.getenv(flag)
    if flag_value is None:
        return default
    else:
        return flag_value == "1" or flag_value.lower() == "true"


def _load_dont_log_model_data() -> bool:
    return _debug_flag_enabled("OPENAI_AGENTS_DONT_LOG_MODEL_DATA", default=True)


def _load_dont_log_tool_data() -> bool:
    return _debug_flag_enabled("OPENAI_AGENTS_DONT_LOG_TOOL_DATA", default=True)


DONT_LOG_MODEL_DATA = _load_dont_log_model_data()
"""By default we don't log LLM inputs/outputs, to prevent exposing sensitive information. Set this
flag to enable logging them.
"""

DONT_LOG_TOOL_DATA = _load_dont_log_tool_data()
"""By default we don't log tool call inputs/outputs, to prevent exposing sensitive information. Set
this flag to enable logging them.
"""
