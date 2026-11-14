"""Wire schemas needed to validate ordinary Python tool responses."""

from typing import TYPE_CHECKING, Literal as Literal

from typing_extensions import NotRequired as NotRequired, TypedDict as TypedDict

from ._public import (
    preserve_module_identity as _preserve_module_identity,
    type_alias_constructor as _type_alias_constructor,
)

if TYPE_CHECKING:
    from typing_extensions import TypeAliasType as _PublicTypeAliasType

    from . import _events as _events
else:
    _PublicTypeAliasType = _type_alias_constructor("agents._events")


class Failure(TypedDict):
    code: str
    message: str | None
    http_status_code: NotRequired[int]


FunctionCallStatus = _PublicTypeAliasType(
    "FunctionCallStatus", Literal["in_progress", "completed", "failed", "incomplete"]
)


class InputTokensDetails(TypedDict):
    cached_tokens: int
    # Omitted when unavailable in the source record; an explicit zero is known.
    cache_write_tokens: NotRequired[int]


class OutputTokensDetails(TypedDict):
    reasoning_tokens: int


class TokenUsage(TypedDict):
    input_tokens: int
    input_tokens_details: InputTokensDetails
    output_tokens: int
    output_tokens_details: OutputTokensDetails
    total_tokens: int


class NativeInputText(TypedDict):
    type: Literal["inputText"]
    text: str


class NativeInputImage(TypedDict):
    type: Literal["inputImage"]
    imageUrl: NotRequired[str]
    fileId: NotRequired[str]
    detail: NotRequired[Literal["auto", "low", "high", "original"] | None]


class NativeInputFile(TypedDict):
    type: Literal["inputFile"]
    file_id: NotRequired[str]
    file_data: NotRequired[str]
    file_url: NotRequired[str]
    filename: NotRequired[str]


class NativeInputAudio(TypedDict):
    type: Literal["inputAudio"]
    audioUrl: str


class FunctionCallResponse(TypedDict):
    contentItems: list[NativeInputText | NativeInputImage | NativeInputFile | NativeInputAudio]
    success: bool


_preserve_module_identity(globals(), "agents._events")


def __getattr__(name: str):
    if name != "_events":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    implementation = import_module("._events", __package__)
    globals()[name] = implementation
    return implementation
