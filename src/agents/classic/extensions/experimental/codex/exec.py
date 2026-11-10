"""Compatibility alias for the shared external Codex CLI implementation."""

import sys as _sys

from _agents_shared.codex_cli import exec as _implementation
from _agents_shared.codex_cli.exec import (
    _DEFAULT_SUBPROCESS_STREAM_LIMIT_BYTES as _DEFAULT_SUBPROCESS_STREAM_LIMIT_BYTES,
    _INTERNAL_ORIGINATOR_ENV as _INTERNAL_ORIGINATOR_ENV,
    _MAX_SUBPROCESS_STREAM_LIMIT_BYTES as _MAX_SUBPROCESS_STREAM_LIMIT_BYTES,
    _MIN_SUBPROCESS_STREAM_LIMIT_BYTES as _MIN_SUBPROCESS_STREAM_LIMIT_BYTES,
    _SUBPROCESS_STREAM_LIMIT_ENV_VAR as _SUBPROCESS_STREAM_LIMIT_ENV_VAR,
    _TYPESCRIPT_SDK_ORIGINATOR as _TYPESCRIPT_SDK_ORIGINATOR,
    CodexExec as CodexExec,
    CodexExecArgs as CodexExecArgs,
    _platform_target_triple as _platform_target_triple,
    _resolve_subprocess_stream_limit_bytes as _resolve_subprocess_stream_limit_bytes,
    _validate_subprocess_stream_limit_bytes as _validate_subprocess_stream_limit_bytes,
    _watch_signal as _watch_signal,
    find_codex_path as find_codex_path,
)

_sys.modules[__name__] = _implementation
