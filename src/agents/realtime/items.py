from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class InputText(BaseModel):
    type: Literal["input_text"] = "input_text"
    text: str | None = None

    # Allow extra data
    model_config = ConfigDict(extra="allow")


class InputAudio(BaseModel):
    type: Literal["input_audio"] = "input_audio"
    audio: str | None = None
    transcript: str | None = None

    # Allow extra data
    model_config = ConfigDict(extra="allow")


class AssistantText(BaseModel):
    type: Literal["text"] = "text"
    text: str | None = None

    # Allow extra data
    model_config = ConfigDict(extra="allow")


class AssistantAudio(BaseModel):
    type: Literal["audio"] = "audio"
    audio: str | None = None
    transcript: str | None = None

    # Allow extra data
    model_config = ConfigDict(extra="allow")


class SystemMessageItem(BaseModel):
    item_id: str
    previous_item_id: str | None = None
    type: Literal["message"] = "message"
    role: Literal["system"] = "system"
    content: list[InputText]

    # Allow extra data
    model_config = ConfigDict(extra="allow")


class UserMessageItem(BaseModel):
    item_id: str
    previous_item_id: str | None = None
    type: Literal["message"] = "message"
    role: Literal["user"] = "user"
    content: list[Annotated[InputText | InputAudio, Field(discriminator="type")]]

    # Allow extra data
    model_config = ConfigDict(extra="allow")


class AssistantMessageItem(BaseModel):
    item_id: str
    previous_item_id: str | None = None
    type: Literal["message"] = "message"
    role: Literal["assistant"] = "assistant"
    status: Literal["in_progress", "completed", "incomplete"] | None = None
    content: list[Annotated[AssistantText | AssistantAudio, Field(discriminator="type")]]

    # Allow extra data
    model_config = ConfigDict(extra="allow")


RealtimeMessageItem = Annotated[
    Union[SystemMessageItem, UserMessageItem, AssistantMessageItem],
    Field(discriminator="role"),
]


class RealtimeToolCallItem(BaseModel):
    item_id: str
    previous_item_id: str | None = None
    call_id: str | None
    type: Literal["function_call"] = "function_call"
    status: Literal["in_progress", "completed"]
    arguments: str
    name: str
    output: str | None = None

    # Allow extra data
    model_config = ConfigDict(extra="allow")


RealtimeItem = Union[RealtimeMessageItem, RealtimeToolCallItem]


class RealtimeResponse(BaseModel):
    id: str
    output: list[RealtimeMessageItem]
