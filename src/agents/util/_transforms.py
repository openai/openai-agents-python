import re


def transform_string_function_style(name: str) -> str:
    # Replace spaces with underscores
    name = name.replace(" ", "_")

    # Replace non-alphanumeric characters with underscores
    name = re.sub(r"[^a-zA-Z0-9]", "_", name)

    return name.lower()
