"""Private capture and validation of native model overrides."""

from __future__ import annotations

import json
import math
import sys
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, TypeAlias, cast

from typing_extensions import Required, TypedDict

from .retry import ModelRetrySettings
from .tools import ToolRef

if TYPE_CHECKING:
    from openai import Omit as Omit

    from .agent import ModelSettings
else:
    Omit = getattr(sys.modules.get("openai"), "Omit", type(None))


class _StaticProviderCompaction(TypedDict, closed=True):
    type: Literal["static"]
    threshold: int


class _DynamicProviderCompaction(TypedDict, total=False, closed=True):
    type: Required[Literal["dynamic"]]
    threshold_ratio: float
    fallback_threshold: int


ProviderCompactionPolicy: TypeAlias = _StaticProviderCompaction | _DynamicProviderCompaction
_MAX_TOKEN_THRESHOLD = 9_007_199_254_740_991


def _capture_provider_compaction(value: Any) -> ProviderCompactionPolicy:
    if not isinstance(value, Mapping):
        raise TypeError("provider_compaction must be a policy mapping")
    if value.get("type") == "static" and value.keys() == {"type", "threshold"}:
        threshold = value["threshold"]
        if type(threshold) is not int or not 0 <= threshold <= _MAX_TOKEN_THRESHOLD:
            raise ValueError("provider_compaction threshold must be a nonnegative safe integer")
        return {"type": "static", "threshold": threshold}
    if value.get("type") == "dynamic" and not value.keys() - {
        "type",
        "threshold_ratio",
        "fallback_threshold",
    }:
        ratio = value.get("threshold_ratio", 0.9)
        fallback = value.get("fallback_threshold", 240_000)
        if type(ratio) not in (int, float) or not 0 <= ratio <= 1:
            raise ValueError("provider_compaction threshold_ratio must be between 0 and 1")
        if type(fallback) is not int or not 0 <= fallback <= _MAX_TOKEN_THRESHOLD:
            raise ValueError(
                "provider_compaction fallback_threshold must be a nonnegative safe integer"
            )
        return {
            "type": "dynamic",
            "threshold_ratio": ratio,
            "fallback_threshold": fallback,
        }
    raise ValueError("Unsupported provider_compaction policy")


def _capture_hosted_tool_choice(value: Mapping[str, Any]) -> Mapping[str, Any]:
    kind = value.get("type")
    supported = {
        "web_search",
        "file_search",
        "code_interpreter",
        "image_generation",
        "computer",
        "computer_use_preview",
        "shell",
        "apply_patch",
        "tool_search",
        "programmatic_tool_calling",
        "mcp",
    }
    if (
        not isinstance(kind, str)
        or kind not in supported
        or value.keys() - ({"type", "server_label", "name"} if kind == "mcp" else {"type"})
    ):
        raise ValueError("hosted tool choice needs a supported provider selector")
    copied = dict(value)
    if kind == "mcp" and (
        not isinstance(copied.get("server_label"), str)
        or not copied["server_label"].strip()
        or (
            "name" in copied and (not isinstance(copied["name"], str) or not copied["name"].strip())
        )
    ):
        raise ValueError("hosted MCP choice needs a captured server and tool identity")
    return MappingProxyType(copied)


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
        elif key == "provider_compaction":
            captured[key] = _capture_provider_compaction(value)
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
        elif key in (
            "verbosity",
            "prompt_cache_retention",
            "prompt_cache_ttl",
            "prompt_cache_mode",
        ):
            allowed = {
                "verbosity": ("low", "medium", "high"),
                "prompt_cache_retention": ("in_memory", "24h"),
                "prompt_cache_ttl": ("30m", "12h"),
                "prompt_cache_mode": ("explicit",),
            }[key]
            if value not in allowed:
                raise ValueError(f"Unsupported {key}")
        elif key == "extra_headers":
            # A caller can import the optional client after native declarations load.
            openai = sys.modules.get("openai")
            omit_type = getattr(openai, "Omit", type(None))
            omit = getattr(openai, "omit", None)
            if not isinstance(value, Mapping) or any(
                not isinstance(name, str)
                or (item is not None and not isinstance(item, (str, omit_type)))
                for name, item in value.items()
            ):
                raise TypeError("extra_headers must map strings to strings, None, or Omit")
            captured[key] = MappingProxyType(
                {
                    name: item if item is None or isinstance(item, str) else omit
                    for name, item in value.items()
                }
            )
        elif key == "extra_query":
            if not isinstance(value, Mapping) or any(not isinstance(name, str) for name in value):
                raise TypeError("extra_query must be a JSON object")
            captured[key] = json.loads(json.dumps(dict(value), allow_nan=False))
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
            elif isinstance(value, Mapping):
                captured["tool_choice"] = _capture_hosted_tool_choice(value)
            elif value not in ("auto", "none", "required"):
                raise ValueError("tool_choice must be an outer mode or ToolRef")
    return cast("ModelSettings", captured)
