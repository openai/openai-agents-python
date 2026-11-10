"""Validate and atomically publish application-selected JSON artifacts."""

from __future__ import annotations

import json as json_module
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated, Any

from pydantic import Field, TypeAdapter, ValidationError

from ._function_tools import FunctionTool, function_tool
from ._public import preserve_module_identity
from .tools import ToolOutput

__all__ = ["json_artifact_tool"]


def json_artifact_tool(
    *, name: str, path: str | Path, schema: type[Any]
) -> FunctionTool[Any, Any, Any]:
    """Create a native tool that checks and publishes a named JSON artifact.

    The model supplies a ``json`` string. Expected JSON/schema failures, including
    non-finite numbers and invalid Unicode, return diagnostics for correction.
    Other exceptions fail the turn.
    The application chooses a trusted destination and Pydantic-compatible schema.
    Candidates use Pydantic's JSON validation. Files contain the checked value's
    JSON serialization with serialization aliases; schemas can define different
    input and output forms.
    Its parent directory must exist. A rejected candidate never replaces an
    existing file; successful concurrent calls use the last atomic replacement.
    Atomic replacement does not guarantee durable storage after a machine crash.
    """
    destination = Path(path).absolute()
    adapter = TypeAdapter(schema)

    def invalid_constant(value: str) -> None:
        raise ValueError(f"{value} is not a JSON number")

    @function_tool(
        name=name,
        description=f"Validate JSON and publish artifact {destination.name}. Repair rejected JSON.",
    )
    def publish(candidate_json: Annotated[str, Field(alias="json")]) -> Any:
        try:
            json_module.loads(candidate_json, parse_constant=invalid_constant)
        except (json_module.JSONDecodeError, ValueError) as error:
            return ToolOutput.error(
                (
                    f"Artifact {destination.name}: JSON syntax check failed: {str(error)[:400]}. "
                    f"Fix the JSON and call {name} again."
                )[:2048]
            )
        try:
            checked = adapter.validate_json(candidate_json)
        except ValidationError as error:
            problems = []
            for issue in error.errors(include_input=False, include_url=False)[:5]:
                location = ".".join(str(part) for part in issue["loc"]) or "$"
                problems.append(f"{location[:160]}: {issue['msg'][:200]}")
            return ToolOutput.error(
                (
                    f"Artifact {destination.name}: schema check failed: {'; '.join(problems)}. "
                    f"Fix the JSON and call {name} again."
                )[:2048]
            )
        value = adapter.dump_python(checked, mode="json", by_alias=True)
        try:
            content = json_module.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
            content.encode("utf-8")
        except ValueError as error:
            return ToolOutput.error(
                (
                    f"Artifact {destination.name}: JSON value check failed: {str(error)[:400]}. "
                    f"Fix the JSON and call {name} again."
                )[:2048]
            )
        temporary: Path | None = None
        try:
            with NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=destination.parent,
                prefix=".agents-artifact-",
                delete=False,
            ) as output:
                temporary = Path(output.name)
                output.write(content)
            os.replace(temporary, destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return ToolOutput.text(f"Published validated JSON artifact {name}: {destination.name}")

    return publish


preserve_module_identity(globals(), "agents.artifacts")
