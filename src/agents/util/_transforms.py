import re

from ..logger import logger


def transform_string_function_style(name: str) -> str:
    # Preserve the original name for comparison and warning message
    original_name = name

    # Replace spaces with underscores
    name = name.replace(" ", "_")

    # Replace non-alphanumeric characters with underscores
    transformed_name = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    final_name = transformed_name.lower()

    if final_name != original_name:
        logger.warning(
            f"Tool name {original_name!r} contains invalid characters for function calling and has been "
            f"transformed to {final_name!r}. Please use only letters, digits, and underscores "
            "to avoid potential naming conflicts."
        )

    return final_name
