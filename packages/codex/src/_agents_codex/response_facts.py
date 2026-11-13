"""Private received facts, separate from replayable model input.

Records retain original native identities. Refusal and token content belongs to
private history or explicit observations; it never describes terminal status.
"""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any, Literal, TypeAlias, cast

from typing_extensions import NotRequired, TypedDict

from ._public import preserve_module_identity as _preserve_module_identity


class ProbabilityAlternative(TypedDict):
    token: str
    bytes: NotRequired[list[int]]
    logprob: float


class ProbabilityToken(ProbabilityAlternative):
    top_logprobs: NotRequired[list[ProbabilityAlternative]]


class RefusalFact(TypedDict):
    kind: Literal["refusal"]
    item_id: str | None
    content_index: int | None
    snapshot: bool
    text: str
    capture_complete: bool


class TextProbabilitiesFact(TypedDict):
    kind: Literal["text_probabilities"]
    item_id: str | None
    output_index: int | None
    content_index: int | None
    snapshot: bool
    delta: str | None
    tokens: list[ProbabilityToken]
    capture_complete: bool


class HostedAgentAttributionFact(TypedDict):
    kind: Literal["hosted_agent_attribution"]
    item_fingerprint: str
    agent_name: str
    phase: str | None


class HostedAgentAttributionSummary(TypedDict):
    kind: Literal["hosted_agent_attribution"]


class CaptureTruncatedFact(TypedDict):
    kind: Literal["capture_truncated"]


class MalformedProbabilitiesFact(TypedDict):
    kind: Literal["malformed_probabilities"]


ResponseFact: TypeAlias = (
    RefusalFact
    | TextProbabilitiesFact
    | HostedAgentAttributionFact
    | CaptureTruncatedFact
    | MalformedProbabilitiesFact
)


class RecordedResponseFact(TypedDict):
    id: str
    thread_id: str
    turn_id: str
    request_id: str | None
    response_id: str | None
    upstream_request_id: NotRequired[str]
    fact: ResponseFact


class ResponseFactContentSummary(TypedDict):
    kind: Literal["refusal", "text_probabilities"]
    capture_complete: bool


ResponseFactSummary: TypeAlias = (
    ResponseFactContentSummary
    | HostedAgentAttributionSummary
    | CaptureTruncatedFact
    | MalformedProbabilitiesFact
)


class RecordedResponseFactSummary(TypedDict):
    id: str
    thread_id: str
    turn_id: str
    request_id: str | None
    response_id: str | None
    upstream_request_id: NotRequired[str]
    fact: ResponseFactSummary


def _summarize_response_fact(
    record: RecordedResponseFact | RecordedResponseFactSummary,
) -> RecordedResponseFactSummary:
    result: dict[str, Any] = deepcopy(cast(dict[str, Any], record))
    fact = result["fact"]
    if fact["kind"] in ("refusal", "text_probabilities"):
        result["fact"] = {"kind": fact["kind"], "capture_complete": fact["capture_complete"]}
    elif fact["kind"] == "hosted_agent_attribution":
        result["fact"] = {"kind": fact["kind"]}
    return cast(RecordedResponseFactSummary, result)


def _alternative(value: Any) -> ProbabilityAlternative | None:
    if not isinstance(value, dict) or not isinstance(value.get("token"), str):
        return None
    logprob = value.get("logprob")
    if (
        isinstance(logprob, bool)
        or not isinstance(logprob, (int, float))
        or abs(logprob) > 1.7976931348623157e308
        or not math.isfinite(logprob)
    ):
        return None
    result: ProbabilityAlternative = {"token": value["token"], "logprob": logprob}
    if value.get("bytes") is not None:
        values = value["bytes"]
        if not isinstance(values, list) or any(
            type(item) is not int or not 0 <= item <= 255 for item in values
        ):
            return None
        result["bytes"] = values.copy()
    return result


def _tokens(values: list[Any]) -> list[ProbabilityToken] | None:
    result: list[ProbabilityToken] = []
    for value in values:
        item = _alternative(value)
        if item is None:
            return None
        token = cast(ProbabilityToken, item)
        if value.get("top_logprobs") is not None:
            alternatives = value["top_logprobs"]
            if not isinstance(alternatives, list):
                return None
            projected = [_alternative(alt) for alt in alternatives]
            if any(alt is None for alt in projected):
                return None
            token["top_logprobs"] = cast(list[ProbabilityAlternative], projected)
        result.append(token)
    return result


def _response_fact_record(payload: Any) -> RecordedResponseFact | None:
    """Copy the typed native record; never project arbitrary event fields."""
    if not isinstance(payload, dict) or any(
        not isinstance(payload.get(key), str) or not payload[key]
        for key in ("id", "thread_id", "turn_id")
    ):
        return None
    if any(
        payload.get(key) is not None and not isinstance(payload[key], str)
        for key in ("request_id", "response_id")
    ):
        return None
    fact = payload.get("fact")
    if not isinstance(fact, dict):
        return None
    kind = fact.get("kind")
    fields: tuple[str, ...]
    if (
        kind == "refusal"
        and isinstance(fact.get("text"), str)
        and type(fact.get("snapshot")) is bool
        and type(fact.get("capture_complete")) is bool
    ):
        fields = ("kind", "item_id", "content_index", "snapshot", "text", "capture_complete")
    elif (
        kind == "text_probabilities"
        and isinstance(fact.get("tokens"), list)
        and type(fact.get("snapshot")) is bool
        and type(fact.get("capture_complete")) is bool
    ):
        fields = (
            "kind",
            "item_id",
            "output_index",
            "content_index",
            "snapshot",
            "delta",
            "tokens",
            "capture_complete",
        )
    elif (
        kind == "hosted_agent_attribution"
        and isinstance(fact.get("item_fingerprint"), str)
        and len(fact["item_fingerprint"]) == 64
        and all(c in "0123456789abcdef" for c in fact["item_fingerprint"])
        and isinstance(fact.get("agent_name"), str)
        and fact["agent_name"]
        and (fact.get("phase") is None or isinstance(fact["phase"], str))
    ):
        fields = ("kind", "item_fingerprint", "agent_name", "phase")
    elif kind in ("capture_truncated", "malformed_probabilities"):
        fields = ("kind",)
    else:
        return None
    # Native serde owns token/number validation. Re-project its exact fields so
    # unrelated archive extensions cannot acquire a public observation route.
    projected = {key: deepcopy(fact.get(key)) for key in fields}
    if kind in ("refusal", "text_probabilities"):
        if fact.get("item_id") is not None and not isinstance(fact["item_id"], str):
            return None
        for key in ("content_index", "output_index"):
            if (
                key in fields
                and fact.get(key) is not None
                and (type(fact[key]) is not int or fact[key] < 0)
            ):
                return None
    if kind == "text_probabilities":
        if fact.get("delta") is not None and not isinstance(fact["delta"], str):
            return None
        tokens = _tokens(fact["tokens"])
        if tokens is None:
            return None
        projected["tokens"] = tokens
    result = {
        "id": payload["id"],
        "thread_id": payload["thread_id"],
        "turn_id": payload["turn_id"],
        "request_id": payload.get("request_id"),
        "response_id": payload.get("response_id"),
        "fact": projected,
    }
    if isinstance(payload.get("upstream_request_id"), str):
        result["upstream_request_id"] = payload["upstream_request_id"]
    return cast(RecordedResponseFact, result)


_preserve_module_identity(globals(), "agents.history")
