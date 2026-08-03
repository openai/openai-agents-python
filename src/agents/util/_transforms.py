import re

from ..logger import logger


def transform_string_function_style(name: str, *, warn_on_whitespace: bool = True) -> str:
    whitespace_normalized_name = re.sub(r"\s", "_", name)

    transformed_name = re.sub(r"[^a-zA-Z0-9_]", "_", whitespace_normalized_name)
    final_name = transformed_name.lower()

    # Warn whenever the final name differs from the original, including case-only changes.
    # Previously only character-replacement changes were warned about (transformed_name != name),
    # which silently missed names like "MyAgent" → "myagent".
    if final_name != name and (
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
