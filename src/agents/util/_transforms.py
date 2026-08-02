import hashlib
import re

from .._tool_identity import MAX_FUNCTION_TOOL_NAME_LENGTH
from ..logger import logger

_HASH_SUFFIX_LENGTH = 8


def transform_string_function_style(name: str, *, warn_on_whitespace: bool = True) -> str:
    whitespace_normalized_name = re.sub(r"\s", "_", name)

    transformed_name = re.sub(r"[^a-zA-Z0-9_]", "_", whitespace_normalized_name)
    final_name = transformed_name.lower()

    if len(final_name) > MAX_FUNCTION_TOOL_NAME_LENGTH:
        # Same truncate-and-hash shape MCP tool names already use, so distinct long names stay
        # distinct instead of being rejected by the provider as over-length.
        suffix = f"_{hashlib.sha1(name.encode('utf-8')).hexdigest()[:_HASH_SUFFIX_LENGTH]}"
        stem = final_name[: MAX_FUNCTION_TOOL_NAME_LENGTH - len(suffix)].rstrip("_")
        shortened_name = f"{stem or 'tool'}{suffix}"
        logger.warning(
            "Tool name %r exceeds the %d character limit for function calling and has been "
            "shortened to %r. Pass an explicit tool name to control it.",
            name,
            MAX_FUNCTION_TOOL_NAME_LENGTH,
            shortened_name,
        )
        final_name = shortened_name

    if transformed_name != name and (
        warn_on_whitespace or transformed_name != whitespace_normalized_name
    ):
        logger.warning(
            "Tool name %r contains invalid characters for function calling and has been "
            "transformed to %r. Please use only letters, digits, and underscores to avoid "
            "potential naming conflicts.",
            name,
            final_name,
        )

    return final_name
