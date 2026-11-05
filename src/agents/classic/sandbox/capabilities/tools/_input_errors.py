from pydantic import BaseModel, ValidationError


def format_invalid_arguments(error: ValidationError, model: type[BaseModel]) -> str:
    """Identify invalid declared fields without exposing input values or exception text."""
    locations = {
        item["loc"][0]
        for item in error.errors(include_input=False, include_context=False, include_url=False)
        if item["loc"]
    }
    fields = ", ".join(name for name in model.model_fields if name in locations)
    target = f" for {fields}" if fields else ""
    return f"Invalid tool arguments{target}. Check the tool schema and try again."
