from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

from agents.exceptions import UserError


@dataclass(frozen=True)
class CodexOptions:
    # Optional absolute path to the codex CLI binary.
    codex_path_override: str | None = None
    # Override OpenAI base URL for the Codex CLI process.
    base_url: str | None = None
    # API key passed to the Codex CLI (CODEX_API_KEY).
    api_key: str | None = None
    # Environment variables for the Codex CLI process (do not inherit os.environ).
    env: Mapping[str, str] | None = None


def coerce_codex_options(
    options: CodexOptions | Mapping[str, Any] | None,
) -> CodexOptions | None:
    if options is None or isinstance(options, CodexOptions):
        return options
    if not isinstance(options, Mapping):
        raise UserError("CodexOptions must be a CodexOptions or a mapping.")

    allowed = {field.name for field in fields(CodexOptions)}
    unknown = set(options.keys()) - allowed
    if unknown:
        raise UserError(f"Unknown CodexOptions field(s): {sorted(unknown)}")

    return CodexOptions(**dict(options))
