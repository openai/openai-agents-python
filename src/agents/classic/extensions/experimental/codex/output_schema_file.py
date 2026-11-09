"""Compatibility alias for the shared external Codex CLI implementation."""

import sys as _sys

from agents._codex_cli import output_schema_file as _implementation
from agents._codex_cli.output_schema_file import (
    OutputSchemaFile as OutputSchemaFile,
    _is_plain_json_object as _is_plain_json_object,
    create_output_schema_file as create_output_schema_file,
)

_sys.modules[__name__] = _implementation
