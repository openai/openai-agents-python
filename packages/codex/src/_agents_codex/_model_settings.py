"""Private capture and validation of native model overrides."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

from .retry import ModelRetrySettings
from .tools import ToolRef

if TYPE_CHECKING:
    from .agent import ModelSettings


def _capture_model_settings(
    settings: ModelSettings | None, settings_type: type[ModelSettings]
) -> ModelSettings:
    if settings is None:
        return {}
    if not isinstance(settings, Mapping):
        raise TypeError("model_settings must be a mapping")
    unknown = settings.keys() - settings_type.__annotations__.keys()
    if unknown:
        raise ValueError(f"Unsupported model setting(s): {sorted(unknown)}")
    for timeout_name in ("invocation_timeout_ms", "attempt_timeout_ms"):
        if timeout_name not in settings:
            continue
        timeout_ms = cast(Mapping[str, Any], settings)[timeout_name]
        if timeout_ms is not None and (
            type(timeout_ms) is not int or not 1 <= timeout_ms <= 4_294_967_295
        ):
            raise ValueError(
                f"{timeout_name} must be an integer in the range 1..=4294967295 or None"
            )

    captured: dict[str, Any] = dict(settings)
    for key, value in captured.items():
        if value is None:
            continue
        if key == "retry":
            if not isinstance(value, ModelRetrySettings):
                raise TypeError("retry must be ModelRetrySettings or None")
        elif key == "input_modalities":
            if (
                not isinstance(value, Sequence)
                or isinstance(value, (str, bytes))
                or not value
                or any(item not in ("text", "image", "audio") for item in value)
                or len(set(value)) != len(value)
            ):
                raise ValueError(
                    "input_modalities requires a nonempty sequence "
                    "of distinct text/image/audio values"
                )
            captured[key] = list(value)
        elif key == "max_output_tokens":
            if type(value) is not int or not 0 <= value <= 18_446_744_073_709_551_615:
                raise ValueError("max_output_tokens must be an unsigned 64-bit integer")
        elif key == "top_logprobs":
            if type(value) is not int or not 0 <= value <= 255:
                raise ValueError("top_logprobs must be an unsigned 8-bit integer")
        elif key in ("temperature", "top_p"):
            try:
                finite = type(value) in (int, float) and math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                raise ValueError(f"{key} must be a finite 64-bit number")
        elif key in ("parallel_tool_calls", "store", "reset_tool_choice"):
            if type(value) is not bool:
                raise TypeError(f"{key} must be bool or None")
        elif key == "truncation":
            if value not in ("auto", "disabled"):
                raise ValueError("truncation must be auto or disabled")
        elif key == "include":
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise TypeError("include must be a list of strings")
            captured[key] = list(value)
        elif key in ("context_management", "extra_body"):
            if key == "context_management":
                if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
                    raise TypeError("context_management must be a list of JSON objects")
            elif not isinstance(value, Mapping) or any(not isinstance(k, str) for k in value):
                raise TypeError("extra_body must be a JSON object")
            if key == "extra_body" and set(value) & {
                "input_modalities",
                "reset_tool_choice",
                "tool_choice",
                "client_metadata",
                "service_tier",
                "tools",
                "access_programs",
                "metadata",
                "previous_response_id",
                "stream",
                "parallel_tool_calls",
                "prompt_cache_retention",
                "model",
                "prompt_cache_key",
                "stream_options",
                "conversation",
                "temperature",
                "prompt_cache_options",
                "truncation",
                "top_p",
                "generate",
                "store",
                "context_management",
                "instructions",
                "type",
                "reasoning",
                "include",
                "input",
                "top_logprobs",
                "text",
                "max_output_tokens",
            }:
                raise ValueError(
                    "extra_body cannot replace native request fields; "
                    "use the corresponding model setting"
                )
            captured[key] = json.loads(
                json.dumps(dict(value) if isinstance(value, Mapping) else value, allow_nan=False)
            )
        elif key in ("verbosity", "prompt_cache_retention", "prompt_cache_ttl"):
            allowed = {
                "verbosity": ("low", "medium", "high"),
                "prompt_cache_retention": ("in_memory", "24h"),
                "prompt_cache_ttl": ("30m", "12h"),
            }[key]
            if value not in allowed:
                raise ValueError(f"Unsupported {key}")
        elif key == "metadata":
            if not isinstance(value, Mapping) or any(
                not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()
            ):
                raise ValueError("metadata must be a mapping of strings to strings")
            captured[key] = MappingProxyType(dict(value))
        elif key == "tool_choice":
            if isinstance(value, ToolRef):
                if (
                    not isinstance(value.name, str)
                    or not value.name.strip()
                    or (
                        value.namespace is not None
                        and (not isinstance(value.namespace, str) or not value.namespace.strip())
                    )
                ):
                    raise ValueError("tool_choice needs a nonempty captured tool identity")
                captured["tool_choice"] = ToolRef(value.name, value.namespace)
            elif value not in ("auto", "none", "required"):
                raise ValueError("tool_choice must be an outer mode or ToolRef")
    return cast("ModelSettings", captured)
