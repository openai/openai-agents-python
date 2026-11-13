"""Detached provider usage counters for the native realtime owner."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from ._public import preserve_module_identity as _preserve_module_identity


@dataclass(frozen=True)
class RealtimeUsage:
    """Reported speech-model counters, separate from backend thread usage.

    Missing counters remain unknown. Cached/audio/text/image detail counters
    are subsets of input/output totals, not additional charges. Detail mappings
    are detached snapshots. Counters must be nonnegative integers at most
    2**53 - 1; invalid counters or cumulative overflow end the owner with an
    error. Unknown vendor fields remain available only through raw observations.
    Totals include only accepted completions observed
    during this realtime owner's lifetime, not restored/backend usage.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    input_token_details: Mapping[str, Any] | None = None
    output_token_details: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class RealtimeUsageAccounting:
    """Counts of accepted response completions and reports on one connection."""

    finished_responses: int
    usage_reports: int


def _reported_usage(value: dict[str, Any]) -> dict[str, Any]:
    """Select the finite provider counter schema; unknown vendor fields stay raw-only."""

    def counters(source: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
        selected: dict[str, Any] = {}
        for key in names:
            count = source.get(key)
            if count is None:
                continue
            if type(count) is not int or not 0 <= count <= 2**53 - 1:
                raise ValueError("Realtime usage counters must be nonnegative safe integers")
            selected[key] = count
        return selected

    result = counters(value, ("input_tokens", "output_tokens", "total_tokens"))
    for field in ("input_token_details", "output_token_details"):
        details = value.get(field)
        if details is None:
            continue
        if not isinstance(details, dict):
            raise ValueError("Realtime usage details must be counter objects")
        result[field] = counters(
            details, ("text_tokens", "audio_tokens", "image_tokens", "cached_tokens")
        )
        cached = details.get("cached_tokens_details")
        if cached is not None:
            if not isinstance(cached, dict):
                raise ValueError("Realtime cached usage details must be a counter object")
            result[field]["cached_tokens_details"] = counters(
                cached, ("text_tokens", "audio_tokens", "image_tokens")
            )
    return result


def _add_usage(total: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(total)
    for key, value in current.items():
        if isinstance(value, dict):
            result[key] = _add_usage(result.get(key, {}), value)
        else:
            combined = result.get(key, 0) + value
            if combined > 2**53 - 1:
                raise ValueError("Realtime cumulative usage exceeds safe integer range")
            result[key] = combined
    return result


def _usage_snapshot(value: dict[str, Any] | None) -> RealtimeUsage | None:
    if value is None:
        return None
    copied = deepcopy(value)
    return RealtimeUsage(
        **{
            key: copied[key]
            for key in (
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "input_token_details",
                "output_token_details",
            )
            if key in copied
        }
    )


_preserve_module_identity(globals(), "agents.realtime")
