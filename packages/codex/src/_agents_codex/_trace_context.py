"""Explicit W3C correlation values; no ambient tracing state."""

from __future__ import annotations

import re as re
from collections.abc import Mapping as Mapping
from dataclasses import dataclass as dataclass, field as field
from typing import cast as cast

from ._public import preserve_module_identity as _preserve_module_identity

_PARENT = re.compile(r"([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})")
_KEY = re.compile(
    r"(?:[a-z][a-z0-9_*/-]{0,255}|[a-z0-9][a-z0-9_*/-]{0,240}@[a-z][a-z0-9_*/-]{0,12})"
)


@dataclass(frozen=True, kw_only=True)
class TraceContext:
    """An immutable W3C parent carrier, independent of any tracing provider.

    The traceparent has an SDK limit of 512 ASCII characters. Trace IDs and span
    IDs must be nonzero lowercase hex. Version 00 accepts defined flags 00
    through 03 (sampled and random trace ID). Future versions retain their
    opaque suffix; native execution interprets the known prefix. Tracestate
    accepts at most 32 members, each with a key and value of at most 256 ASCII
    characters. The native propagator limits the system ID after @ to 13
    characters. Optional whitespace and empty members are normalized. These
    limits define the supported native carrier profile. Put no credentials or
    private content in either header. This value does not keep a span alive,
    create spans, or propagate baggage. Header IDs carry ancestry only; they do
    not transfer SDK workflow policy or export credentials. Use an SDK-owned
    SpanContext through TraceOptions.parent to retain local workflow policy.
    """

    traceparent: str = field(repr=False)
    tracestate: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        parent = self.traceparent
        if not isinstance(parent, str):
            raise TypeError("traceparent must be a string")
        match = _PARENT.match(parent)
        if (
            match is None
            or len(parent) > 512
            or match[1] == "ff"
            or int(match[2], 16) == 0
            or int(match[3], 16) == 0
            or (match[1] == "00" and len(parent) != 55)
            or (match[1] == "00" and match[4] not in ("00", "01", "02", "03"))
            or (len(parent) > 55 and parent[55] != "-")
            or any(not 0x21 <= ord(char) <= 0x7E for char in parent)
        ):
            raise ValueError("Invalid W3C traceparent")
        state = self.tracestate
        if state is None:
            return
        if not isinstance(state, str):
            raise TypeError("tracestate must be a string or None")
        if len(state.split(",")) > 32:
            raise ValueError("Invalid W3C tracestate")
        members: dict[str, str] = {}
        for member in state.split(","):
            member = member.strip(" \t")
            if not member:
                continue
            key, separator, value = member.partition("=")
            if (
                not separator
                or _KEY.fullmatch(key) is None
                or key in members
                or not 1 <= len(value) <= 256
                or any(not 0x20 <= ord(char) <= 0x7E or char in ",=" for char in value)
            ):
                raise ValueError("Invalid W3C tracestate")
            members[key] = value
        object.__setattr__(
            self, "tracestate", ",".join(f"{k}={v}" for k, v in members.items()) or None
        )

    def to_headers(self) -> dict[str, str]:
        """Return a new lowercase header map containing only this carrier."""
        headers = {"traceparent": self.traceparent}
        if self.tracestate is not None:
            headers["tracestate"] = self.tracestate
        return headers


def _trace_context(value: TraceContext | Mapping[str, str | None] | None) -> TraceContext | None:
    """Normalize once at public admission, before runtime effects."""
    if value is None or isinstance(value, TraceContext):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("parent_trace must be TraceContext, a carrier mapping, or None")
    if "traceparent" not in value or any(key not in ("traceparent", "tracestate") for key in value):
        raise ValueError("A trace carrier requires traceparent and optional tracestate only")
    return TraceContext(
        traceparent=cast(str, value["traceparent"]), tracestate=value.get("tracestate")
    )


def _native_trace_context(value: Mapping[str, str | None] | None) -> TraceContext | None:
    # Native tracing can inherit settings outside the public carrier profile.
    # Optional correlation metadata must not turn a valid tool into a failure.
    try:
        return _trace_context(value)
    except (TypeError, ValueError):
        return None


_preserve_module_identity(globals(), "agents._trace_context")
