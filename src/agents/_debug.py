import os


def _debug_flag_enabled(flag: str) -> bool:
    flag_value = os.getenv(flag)
    return flag_value is not None and (flag_value == "1" or flag_value.lower() == "true")


DONT_LOG_MODEL_DATA = _debug_flag_enabled("OPENAI_AGENTS_DONT_LOG_MODEL_DATA")
"""By default we don't log LLM inputs/outputs, to prevent exposing sensitive information. Set this
flag to enable logging them.
"""

DONT_LOG_TOOL_DATA = _debug_flag_enabled("OPENAI_AGENTS_DONT_LOG_TOOL_DATA")
"""By default we don't log tool call inputs/outputs, to prevent exposing sensitive information. Set
this flag to enable logging them.
"""
