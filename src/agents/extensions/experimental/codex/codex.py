from __future__ import annotations

from collections.abc import Mapping
from typing import Any, overload

from agents.exceptions import UserError

from .codex_options import CodexOptions, coerce_codex_options
from .exec import CodexExec
from .thread import Thread
from .thread_options import ThreadOptions, coerce_thread_options


class _UnsetType:
    pass


_UNSET = _UnsetType()


class Codex:
    @overload
    def __init__(self, options: CodexOptions | Mapping[str, Any] | None = None) -> None: ...

    @overload
    def __init__(
        self,
        *,
        codex_path_override: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None: ...

    def __init__(
        self,
        options: CodexOptions | Mapping[str, Any] | None = None,
        *,
        codex_path_override: str | None | _UnsetType = _UNSET,
        base_url: str | None | _UnsetType = _UNSET,
        api_key: str | None | _UnsetType = _UNSET,
        env: Mapping[str, str] | None | _UnsetType = _UNSET,
    ) -> None:
        kw_values = {
            "codex_path_override": codex_path_override,
            "base_url": base_url,
            "api_key": api_key,
            "env": env,
        }
        has_kwargs = any(value is not _UNSET for value in kw_values.values())
        if options is not None and has_kwargs:
            raise UserError(
                "Codex options must be provided as a CodexOptions/mapping or keyword arguments, "
                "not both."
            )
        if has_kwargs:
            options = {key: value for key, value in kw_values.items() if value is not _UNSET}
        resolved_options = coerce_codex_options(options) or CodexOptions()
        self._exec = CodexExec(
            executable_path=resolved_options.codex_path_override,
            env=_normalize_env(resolved_options),
        )
        self._options = resolved_options

    def start_thread(self, options: ThreadOptions | Mapping[str, Any] | None = None) -> Thread:
        resolved_options = coerce_thread_options(options) or ThreadOptions()
        return Thread(
            exec_client=self._exec,
            options=self._options,
            thread_options=resolved_options,
        )

    def resume_thread(
        self, thread_id: str, options: ThreadOptions | Mapping[str, Any] | None = None
    ) -> Thread:
        resolved_options = coerce_thread_options(options) or ThreadOptions()
        return Thread(
            exec_client=self._exec,
            options=self._options,
            thread_options=resolved_options,
            thread_id=thread_id,
        )


def _normalize_env(options: CodexOptions) -> dict[str, str] | None:
    if options.env is None:
        return None
    # Normalize mapping values to strings for subprocess environment.
    return {str(key): str(value) for key, value in options.env.items()}
