import re

from ..logger import logger


def transform_string_function_style(name: str) -> str:
    # Replace spaces with underscores
    name = name.replace(" ", "_")

    # Replace non-alphanumeric characters with underscores
    transformed_name = re.sub(r"[^a-zA-Z0-9_]", "_", name)

    if transformed_name != name:
        final_name = transformed_name.lower()
        logger.warning(
            f"Tool name {name!r} contains invalid characters for function calling and has been "
            f"transformed to {final_name!r}. Please use only letters, digits, and underscores "
            "to avoid potential naming conflicts."
        )

    return transformed_name.lower()
