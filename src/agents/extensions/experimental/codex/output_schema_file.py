from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from typing import Any, Callable

from agents.exceptions import UserError


@dataclass
class OutputSchemaFile:
    # Holds the on-disk schema path and cleanup callback.
    schema_path: str | None
    cleanup: Callable[[], None]


def _is_plain_json_object(schema: Any) -> bool:
    return isinstance(schema, dict)


def create_output_schema_file(schema: dict[str, Any] | None) -> OutputSchemaFile:
    """Materialize a JSON schema into a temp file for the Codex CLI."""
    if schema is None:
        # No schema means there is no temp file to manage.
        return OutputSchemaFile(schema_path=None, cleanup=lambda: None)

    if not _is_plain_json_object(schema):
        raise UserError("output_schema must be a plain JSON object")

    # The Codex CLI expects a schema file path, so write to a temp directory.
    schema_dir = tempfile.mkdtemp(prefix="codex-output-schema-")
    schema_path = os.path.join(schema_dir, "schema.json")

    def cleanup() -> None:
        # Best-effort cleanup since this runs in finally blocks.
        try:
            shutil.rmtree(schema_dir, ignore_errors=True)
        except Exception:
            pass

    try:
        with open(schema_path, "w", encoding="utf-8") as handle:
            json.dump(schema, handle)
        return OutputSchemaFile(schema_path=schema_path, cleanup=cleanup)
    except Exception:
        cleanup()
        raise
