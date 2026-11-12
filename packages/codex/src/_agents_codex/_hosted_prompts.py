"""Captured provider prompt references on existing native declaration owners."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from copy import deepcopy
from typing import Any, cast

from .model_context import HostedPromptReference, HostedPromptSource


class _CapturedHostedPromptReference(Mapping[str, Any]):
    """Read-only declaration with detached mutable JSON values on public reads."""

    def __init__(self, reference: HostedPromptReference) -> None:
        self._reference = dict(reference)

    def __getitem__(self, key: str) -> Any:
        return deepcopy(self._reference[key])

    def __iter__(self) -> Iterator[str]:
        return iter(self._reference)

    def __len__(self) -> int:
        return len(self._reference)


def _capture_hosted_prompt_reference(value: object) -> HostedPromptReference:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise TypeError("Expected a hosted prompt reference")
    captured = json.loads(json.dumps(dict(value), allow_nan=False))
    if not isinstance(captured.get("id"), str) or not captured["id"].strip():
        raise TypeError("A hosted prompt requires a nonempty id")
    if "version" in captured and not isinstance(captured["version"], str):
        raise TypeError("Hosted prompt version must be a string")
    if "variables" in captured and not isinstance(captured["variables"], dict):
        raise TypeError("Hosted prompt variables must be a JSON object")
    if set(captured) - {"id", "version", "variables"}:
        raise TypeError("Unknown hosted prompt reference field")
    return cast(HostedPromptReference, captured)


def _capture_hosted_prompt_source(
    source: HostedPromptSource | None,
) -> HostedPromptSource | None:
    if source is None or callable(source):
        return source
    if isinstance(source, _CapturedHostedPromptReference):
        return cast(HostedPromptReference, source)
    return cast(
        HostedPromptReference,
        _CapturedHostedPromptReference(_capture_hosted_prompt_reference(source)),
    )


def _hosted_prompt_controls(
    source: HostedPromptSource | None,
    *,
    model: str | None = None,
    instructions: str | None = None,
    tools_provided: bool,
    extra_body: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    if source is None:
        return None
    if extra_body is not None and "prompt" in extra_body:
        raise ValueError("hosted_prompt conflicts with model_settings.extra_body.prompt")
    return {
        **({} if callable(source) else {"reference": _capture_hosted_prompt_reference(source)}),
        "omit_model": model is None,
        "omit_instructions": instructions is None,
        "omit_tools": not tools_provided,
    }
