from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class InputText(BaseModel):
    """Text input content for realtime messages."""

    type: Literal["input_text"] = "input_text"
    """The type identifier for text input."""

    text: str | None = None
    """The text content."""

    # Allow extra data
    model_config = ConfigDict(extra="allow")


class InputAudio(BaseModel):
    """Audio input content for realtime messages."""

    type: Literal["input_audio"] = "input_audio"
    """The type identifier for audio input."""

    audio: str | None = None
    """The base64-encoded audio data."""

    transcript: str | None = None
    """The transcript of the audio, if available."""

    # Allow extra data
    model_config = ConfigDict(extra="allow")


class AssistantText(BaseModel):
    """Text content from the assistant in realtime responses."""

    type: Literal["text"] = "text"
    """The type identifier for text content."""

    text: str | None = None
    """The text content from the assistant."""

    # Allow extra data
    model_config = ConfigDict(extra="allow")


class AssistantAudio(BaseModel):
    """Audio content from the assistant in realtime responses."""

    type: Literal["audio"] = "audio"
    """The type identifier for audio content."""

    audio: str | None = None
    """The base64-encoded audio data from the assistant."""

    transcript: str | None = None
    """The transcript of the audio response."""

    # Allow extra data
    model_config = ConfigDict(extra="allow")


class SystemMessageItem(BaseModel):
    """A system message item in realtime conversations."""

    item_id: str
    """Unique identifier for this message item."""

    previous_item_id: str | None = None
    """ID of the previous item in the conversation."""

    type: Literal["message"] = "message"
    """The type identifier for message items."""

    role: Literal["system"] = "system"
    """The role identifier for system messages."""

    content: list[InputText]
    """List of text content for the system message."""

    # Allow extra data
    model_config = ConfigDict(extra="allow")


class UserMessageItem(BaseModel):
    """A user message item in realtime conversations."""

    item_id: str
    """Unique identifier for this message item."""

    previous_item_id: str | None = None
    """ID of the previous item in the conversation."""

    type: Literal["message"] = "message"
    """The type identifier for message items."""

    role: Literal["user"] = "user"
    """The role identifier for user messages."""

    content: list[Annotated[InputText | InputAudio, Field(discriminator="type")]]
    """List of content items, can be text or audio."""

    # Allow extra data
    model_config = ConfigDict(extra="allow")


class AssistantMessageItem(BaseModel):
    """An assistant message item in realtime conversations."""

    item_id: str
    """Unique identifier for this message item."""

    previous_item_id: str | None = None
    """ID of the previous item in the conversation."""

    type: Literal["message"] = "message"
    """The type identifier for message items."""

    role: Literal["assistant"] = "assistant"
    """The role identifier for assistant messages."""

    status: Literal["in_progress", "completed", "incomplete"] | None = None
    """The status of the assistant's response."""

    content: list[Annotated[AssistantText | AssistantAudio, Field(discriminator="type")]]
    """List of content items from the assistant, can be text or audio."""

    # Allow extra data
    model_config = ConfigDict(extra="allow")


RealtimeMessageItem = Annotated[
    Union[SystemMessageItem, UserMessageItem, AssistantMessageItem],
    Field(discriminator="role"),
]
"""A message item that can be from system, user, or assistant."""


class RealtimeToolCallItem(BaseModel):
    """A tool call item in realtime conversations."""

    item_id: str
    """Unique identifier for this tool call item."""

    previous_item_id: str | None = None
    """ID of the previous item in the conversation."""

    call_id: str | None
    """The call ID for this tool invocation."""

    type: Literal["function_call"] = "function_call"
    """The type identifier for function call items."""

    status: Literal["in_progress", "completed"]
    """The status of the tool call execution."""

    arguments: str
    """The JSON string arguments passed to the tool."""

    name: str
    """The name of the tool being called."""

    output: str | None = None
    """The output result from the tool execution."""

    # Allow extra data
    model_config = ConfigDict(extra="allow")


RealtimeItem = Union[RealtimeMessageItem, RealtimeToolCallItem]
"""A realtime item that can be a message or tool call."""


class RealtimeResponse(BaseModel):
    """A response from the realtime model."""

    id: str
    """Unique identifier for this response."""

    output: list[RealtimeMessageItem]
    """List of message items in the response."""
