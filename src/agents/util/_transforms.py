import re

from ..logger import logger


def transform_string_function_style(name: str, *, warn_on_whitespace: bool = True) -> str:
    whitespace_normalized_name = re.sub(r"\s", "_", name)

    transformed_name = re.sub(r"[^a-zA-Z0-9_]", "_", whitespace_normalized_name)
    final_name = transformed_name.lower()

    if transformed_name != name and (
        warn_on_whitespace or transformed_name != whitespace_normalized_name
    ):
        logger.warning(
            f"Tool name {name!r} contains invalid characters for function calling and has been "
            f"transformed to {final_name!r}. Please use only letters, digits, and underscores "
            "to avoid potential naming conflicts."
        )

    return final_name
