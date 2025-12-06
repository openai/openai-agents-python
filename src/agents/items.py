from __future__ import annotations

import abc
import json
import weakref
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Generic, Literal, TypeVar, Union, cast

import pydantic
from openai.types.responses import (
    Response,
    ResponseComputerToolCall,
    ResponseFileSearchToolCall,
    ResponseFunctionToolCall,
    ResponseFunctionWebSearch,
    ResponseInputItemParam,
    ResponseOutputItem,
    ResponseOutputMessage,
    ResponseOutputRefusal,
    ResponseOutputText,
    ResponseStreamEvent,
)
from openai.types.responses.response_code_interpreter_tool_call import (
    ResponseCodeInterpreterToolCall,
)
from openai.types.responses.response_function_call_output_item_list_param import (
    ResponseFunctionCallOutputItemListParam,
    ResponseFunctionCallOutputItemParam,
)
from openai.types.responses.response_input_file_content_param import ResponseInputFileContentParam
from openai.types.responses.response_input_image_content_param import ResponseInputImageContentParam
from openai.types.responses.response_input_item_param import (
    ComputerCallOutput,
    FunctionCallOutput,
    LocalShellCallOutput,
    McpApprovalResponse,
)
from openai.types.responses.response_output_item import (
    ImageGenerationCall,
    LocalShellCall,
    McpApprovalRequest,
    McpCall,
    McpListTools,
)
from openai.types.responses.response_reasoning_item import ResponseReasoningItem
from pydantic import BaseModel
from typing_extensions import TypeAlias, assert_never

from .exceptions import AgentsException, ModelBehaviorError
from .logger import logger
from .tool import (
    ToolOutputFileContent,
    ToolOutputImage,
    ToolOutputText,
    ValidToolOutputPydanticModels,
    ValidToolOutputPydanticModelsTypeAdapter,
)
from .usage import Usage


def normalize_function_call_output_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Ensure function_call_output payloads conform to Responses API expectations."""

    payload_type = payload.get("type")
    if payload_type not in {"function_call_output", "function_call_result"}:
        return payload

    output_value = payload.get("output")

    if output_value is None:
        payload["output"] = ""
        return payload

    if isinstance(output_value, list):
        if all(
            isinstance(entry, dict) and entry.get("type") in _ALLOWED_FUNCTION_CALL_OUTPUT_TYPES
            for entry in output_value
        ):
            return payload
        payload["output"] = json.dumps(output_value)
        return payload

    if isinstance(output_value, dict):
        entry_type = output_value.get("type")
        if entry_type in _ALLOWED_FUNCTION_CALL_OUTPUT_TYPES:
            payload["output"] = [output_value]
        else:
            payload["output"] = json.dumps(output_value)
        return payload

    if isinstance(output_value, str):
        return payload

    payload["output"] = json.dumps(output_value)
    return payload


if TYPE_CHECKING:
    from .agent import Agent

TResponse = Response
"""A type alias for the Response type from the OpenAI SDK."""

TResponseInputItem = ResponseInputItemParam
"""A type alias for the ResponseInputItemParam type from the OpenAI SDK."""

TResponseOutputItem = ResponseOutputItem
"""A type alias for the ResponseOutputItem type from the OpenAI SDK."""

TResponseStreamEvent = ResponseStreamEvent
"""A type alias for the ResponseStreamEvent type from the OpenAI SDK."""

T = TypeVar("T", bound=Union[TResponseOutputItem, TResponseInputItem])

# Distinguish a missing dict entry from an explicit None value.
_MISSING_ATTR_SENTINEL = object()
_ALLOWED_FUNCTION_CALL_OUTPUT_TYPES: set[str] = {
    "input_text",
    "input_image",
    "output_text",
    "refusal",
    "input_file",
    "computer_screenshot",
    "summary_text",
}


@dataclass
class RunItemBase(Generic[T], abc.ABC):
    agent: Agent[Any]
    """The agent whose run caused this item to be generated."""

    raw_item: T
    """The raw Responses item from the run. This will always be either an output item (i.e.
    `openai.types.responses.ResponseOutputItem` or an input item
    (i.e. `openai.types.responses.ResponseInputItemParam`).
    """

    _agent_ref: weakref.ReferenceType[Agent[Any]] | None = field(
        init=False,
        repr=False,
        default=None,
    )

    def __post_init__(self) -> None:
        # Store a weak reference so we can release the strong reference later if desired.
        self._agent_ref = weakref.ref(self.agent)

    def __getattribute__(self, name: str) -> Any:
        if name == "agent":
            return self._get_agent_via_weakref("agent", "_agent_ref")
        return super().__getattribute__(name)

    def release_agent(self) -> None:
        """Release the strong reference to the agent while keeping a weak reference."""
        if "agent" not in self.__dict__:
            return
        agent = self.__dict__["agent"]
        if agent is None:
            return
        self._agent_ref = weakref.ref(agent) if agent is not None else None
        # Set to None instead of deleting so dataclass repr/asdict keep working.
        self.__dict__["agent"] = None

    def _get_agent_via_weakref(self, attr_name: str, ref_name: str) -> Any:
        # Preserve the dataclass field so repr/asdict still read it, but lazily resolve the weakref
        # when the stored value is None (meaning release_agent already dropped the strong ref).
        # If the attribute was never overridden we fall back to the default descriptor chain.
        data = object.__getattribute__(self, "__dict__")
        value = data.get(attr_name, _MISSING_ATTR_SENTINEL)
        if value is _MISSING_ATTR_SENTINEL:
            return object.__getattribute__(self, attr_name)
        if value is not None:
            return value
        ref = object.__getattribute__(self, ref_name)
        if ref is not None:
            agent = ref()
            if agent is not None:
                return agent
        return None

    def to_input_item(self) -> TResponseInputItem:
        """Converts this item into an input item suitable for passing to the model."""
        if isinstance(self.raw_item, dict):
            # We know that input items are dicts, so we can ignore the type error
            return self.raw_item  # type: ignore
        elif isinstance(self.raw_item, BaseModel):
            # All output items are Pydantic models that can be converted to input items.
            return self.raw_item.model_dump(exclude_unset=True)  # type: ignore
        else:
            raise AgentsException(f"Unexpected raw item type: {type(self.raw_item)}")


@dataclass
class MessageOutputItem(RunItemBase[ResponseOutputMessage]):
    """Represents a message from the LLM."""

    raw_item: ResponseOutputMessage
    """The raw response output message."""

    type: Literal["message_output_item"] = "message_output_item"


@dataclass
class HandoffCallItem(RunItemBase[ResponseFunctionToolCall]):
    """Represents a tool call for a handoff from one agent to another."""

    raw_item: ResponseFunctionToolCall
    """The raw response function tool call that represents the handoff."""

    type: Literal["handoff_call_item"] = "handoff_call_item"


@dataclass
class HandoffOutputItem(RunItemBase[TResponseInputItem]):
    """Represents the output of a handoff."""

    raw_item: TResponseInputItem
    """The raw input item that represents the handoff taking place."""

    source_agent: Agent[Any]
    """The agent that made the handoff."""

    target_agent: Agent[Any]
    """The agent that is being handed off to."""

    type: Literal["handoff_output_item"] = "handoff_output_item"

    _source_agent_ref: weakref.ReferenceType[Agent[Any]] | None = field(
        init=False,
        repr=False,
        default=None,
    )
    _target_agent_ref: weakref.ReferenceType[Agent[Any]] | None = field(
        init=False,
        repr=False,
        default=None,
    )

    def __post_init__(self) -> None:
        super().__post_init__()
        # Maintain weak references so downstream code can release the strong references when safe.
        self._source_agent_ref = weakref.ref(self.source_agent)
        self._target_agent_ref = weakref.ref(self.target_agent)

    def __getattribute__(self, name: str) -> Any:
        if name == "source_agent":
            # Provide lazy weakref access like the base `agent` field so HandoffOutputItem
            # callers keep seeing the original agent until GC occurs.
            return self._get_agent_via_weakref("source_agent", "_source_agent_ref")
        if name == "target_agent":
            # Same as above but for the target of the handoff.
            return self._get_agent_via_weakref("target_agent", "_target_agent_ref")
        return super().__getattribute__(name)

    def release_agent(self) -> None:
        super().release_agent()
        if "source_agent" in self.__dict__:
            source_agent = self.__dict__["source_agent"]
            if source_agent is not None:
                self._source_agent_ref = weakref.ref(source_agent)
            # Preserve dataclass fields for repr/asdict while dropping strong refs.
            self.__dict__["source_agent"] = None
        if "target_agent" in self.__dict__:
            target_agent = self.__dict__["target_agent"]
            if target_agent is not None:
                self._target_agent_ref = weakref.ref(target_agent)
            # Preserve dataclass fields for repr/asdict while dropping strong refs.
            self.__dict__["target_agent"] = None

    def to_input_item(self) -> TResponseInputItem:
        """Convert handoff output into the API format expected by the model."""

        if isinstance(self.raw_item, dict):
            payload = dict(self.raw_item)
            if payload.get("type") == "function_call_result":
                payload["type"] = "function_call_output"
                payload.pop("name", None)
                payload.pop("status", None)

            payload = normalize_function_call_output_payload(payload)
            return cast(TResponseInputItem, payload)

        return super().to_input_item()


ToolCallItemTypes: TypeAlias = Union[
    ResponseFunctionToolCall,
    ResponseComputerToolCall,
    ResponseFileSearchToolCall,
    ResponseFunctionWebSearch,
    McpCall,
    ResponseCodeInterpreterToolCall,
    ImageGenerationCall,
    LocalShellCall,
    dict[str, Any],
]
"""A type that represents a tool call item."""


@dataclass
class ToolCallItem(RunItemBase[Any]):
    """Represents a tool call e.g. a function call or computer action call."""

    raw_item: ToolCallItemTypes
    """The raw tool call item."""

    type: Literal["tool_call_item"] = "tool_call_item"


ToolCallOutputTypes: TypeAlias = Union[
    FunctionCallOutput,
    ComputerCallOutput,
    LocalShellCallOutput,
    dict[str, Any],
]


@dataclass
class ToolCallOutputItem(RunItemBase[Any]):
    """Represents the output of a tool call."""

    raw_item: ToolCallOutputTypes
    """The raw item from the model."""

    output: Any
    """The output of the tool call. This is whatever the tool call returned; the `raw_item`
    contains a string representation of the output.
    """

    type: Literal["tool_call_output_item"] = "tool_call_output_item"

    def to_input_item(self) -> TResponseInputItem:
        """Converts the tool output into an input item for the next model turn.

        Hosted tool outputs (e.g. shell/apply_patch) carry a `status` field for the SDK's
        book-keeping, but the Responses API does not yet accept that parameter. Strip it from the
        payload we send back to the model while keeping the original raw item intact.

        Also converts protocol format (function_call_result) to API format (function_call_output).
        """

        if isinstance(self.raw_item, dict):
            payload = dict(self.raw_item)
            payload_type = payload.get("type")
            # Convert protocol format to API format
            # Protocol uses function_call_result, API expects function_call_output
            if payload_type == "function_call_result":
                payload["type"] = "function_call_output"
                # Remove fields that are in protocol format but not in API format
                payload.pop("name", None)
                payload.pop("status", None)
            elif payload_type == "shell_call_output":
                payload.pop("status", None)
                payload.pop("shell_output", None)
                payload.pop("provider_data", None)
            payload = normalize_function_call_output_payload(payload)
            return cast(TResponseInputItem, payload)

        return super().to_input_item()


@dataclass
class ReasoningItem(RunItemBase[ResponseReasoningItem]):
    """Represents a reasoning item."""

    raw_item: ResponseReasoningItem
    """The raw reasoning item."""

    type: Literal["reasoning_item"] = "reasoning_item"


@dataclass
class MCPListToolsItem(RunItemBase[McpListTools]):
    """Represents a call to an MCP server to list tools."""

    raw_item: McpListTools
    """The raw MCP list tools call."""

    type: Literal["mcp_list_tools_item"] = "mcp_list_tools_item"


@dataclass
class MCPApprovalRequestItem(RunItemBase[McpApprovalRequest]):
    """Represents a request for MCP approval."""

    raw_item: McpApprovalRequest
    """The raw MCP approval request."""

    type: Literal["mcp_approval_request_item"] = "mcp_approval_request_item"


@dataclass
class MCPApprovalResponseItem(RunItemBase[McpApprovalResponse]):
    """Represents a response to an MCP approval request."""

    raw_item: McpApprovalResponse
    """The raw MCP approval response."""

    type: Literal["mcp_approval_response_item"] = "mcp_approval_response_item"


# Union type for tool approval raw items - supports function tools, hosted tools, shell tools, etc.
ToolApprovalRawItem: TypeAlias = Union[
    ResponseFunctionToolCall,
    McpCall,
    LocalShellCall,
    dict[str, Any],  # For flexibility with other tool types
]


@dataclass
class ToolApprovalItem(RunItemBase[Any]):
    """Represents a tool call that requires approval before execution.

    When a tool has `needs_approval=True`, the run will be interrupted and this item will be
    added to the interruptions list. You can then approve or reject the tool call using
    RunState.approve() or RunState.reject() and resume the run.
    """

    raw_item: ToolApprovalRawItem
    """The raw tool call that requires approval. Can be a function tool call, hosted tool call,
    shell call, or other tool type.
    """

    tool_name: str | None = None
    """Explicit tool name to use for approval tracking when not present on the raw item.
    If not provided, falls back to raw_item.name.
    """

    type: Literal["tool_approval_item"] = "tool_approval_item"

    def __post_init__(self) -> None:
        """Set tool_name from raw_item.name if not explicitly provided."""
        if self.tool_name is None:
            # Extract name from raw_item - handle different types
            if isinstance(self.raw_item, dict):
                self.tool_name = self.raw_item.get("name")
            elif hasattr(self.raw_item, "name"):
                self.tool_name = self.raw_item.name
            else:
                self.tool_name = None

    @property
    def name(self) -> str | None:
        """Returns the tool name if available on the raw item or provided explicitly.

        Kept for backwards compatibility with code that previously relied on raw_item.name.
        """
        return self.tool_name or (
            getattr(self.raw_item, "name", None)
            if not isinstance(self.raw_item, dict)
            else self.raw_item.get("name")
        )

    @property
    def arguments(self) -> str | None:
        """Returns the arguments if the raw item has an arguments property, otherwise None.

        This provides a safe way to access tool call arguments regardless of the raw_item type.
        """
        if isinstance(self.raw_item, dict):
            return self.raw_item.get("arguments")
        elif hasattr(self.raw_item, "arguments"):
            return self.raw_item.arguments
        return None

    def to_input_item(self) -> TResponseInputItem:
        """ToolApprovalItem should never be converted to input items.

        These items represent pending approvals and should be filtered out before
        preparing input for the API. This method raises an error to prevent accidental usage.
        """
        raise AgentsException(
            "ToolApprovalItem cannot be converted to an input item. "
            "These items should be filtered out before preparing input for the API."
        )


RunItem: TypeAlias = Union[
    MessageOutputItem,
    HandoffCallItem,
    HandoffOutputItem,
    ToolCallItem,
    ToolCallOutputItem,
    ReasoningItem,
    MCPListToolsItem,
    MCPApprovalRequestItem,
    MCPApprovalResponseItem,
    ToolApprovalItem,
]
"""An item generated by an agent."""


@pydantic.dataclasses.dataclass
class ModelResponse:
    output: list[TResponseOutputItem]
    """A list of outputs (messages, tool calls, etc) generated by the model"""

    usage: Usage
    """The usage information for the response."""

    response_id: str | None
    """An ID for the response which can be used to refer to the response in subsequent calls to the
    model. Not supported by all model providers.
    If using OpenAI models via the Responses API, this is the `response_id` parameter, and it can
    be passed to `Runner.run`.
    """

    def to_input_items(self) -> list[TResponseInputItem]:
        """Convert the output into a list of input items suitable for passing to the model."""
        # We happen to know that the shape of the Pydantic output items are the same as the
        # equivalent TypedDict input items, so we can just convert each one.
        # This is also tested via unit tests.
        return [it.model_dump(exclude_unset=True) for it in self.output]  # type: ignore


class ItemHelpers:
    @classmethod
    def extract_last_content(cls, message: TResponseOutputItem) -> str:
        """Extracts the last text content or refusal from a message."""
        if not isinstance(message, ResponseOutputMessage):
            return ""

        if not message.content:
            return ""
        last_content = message.content[-1]
        if isinstance(last_content, ResponseOutputText):
            return last_content.text
        elif isinstance(last_content, ResponseOutputRefusal):
            return last_content.refusal
        else:
            raise ModelBehaviorError(f"Unexpected content type: {type(last_content)}")

    @classmethod
    def extract_last_text(cls, message: TResponseOutputItem) -> str | None:
        """Extracts the last text content from a message, if any. Ignores refusals."""
        if isinstance(message, ResponseOutputMessage):
            if not message.content:
                return None
            last_content = message.content[-1]
            if isinstance(last_content, ResponseOutputText):
                return last_content.text

        return None

    @classmethod
    def input_to_new_input_list(
        cls, input: str | list[TResponseInputItem]
    ) -> list[TResponseInputItem]:
        """Converts a string or list of input items into a list of input items."""
        if isinstance(input, str):
            return [
                {
                    "content": input,
                    "role": "user",
                }
            ]
        return input.copy()

    @classmethod
    def text_message_outputs(cls, items: list[RunItem]) -> str:
        """Concatenates all the text content from a list of message output items."""
        text = ""
        for item in items:
            if isinstance(item, MessageOutputItem):
                text += cls.text_message_output(item)
        return text

    @classmethod
    def text_message_output(cls, message: MessageOutputItem) -> str:
        """Extracts all the text content from a single message output item."""
        text = ""
        for item in message.raw_item.content:
            if isinstance(item, ResponseOutputText):
                text += item.text
        return text

    @classmethod
    def tool_call_output_item(
        cls, tool_call: ResponseFunctionToolCall, output: Any
    ) -> FunctionCallOutput:
        """Creates a tool call output item from a tool call and its output.

        Accepts either plain values (stringified) or structured outputs using
        input_text/input_image/input_file shapes. Structured outputs may be
        provided as Pydantic models or dicts, or an iterable of such items.
        """

        converted_output = cls._convert_tool_output(output)

        return {
            "call_id": tool_call.call_id,
            "output": converted_output,
            "type": "function_call_output",
        }

    @classmethod
    def _convert_tool_output(cls, output: Any) -> str | ResponseFunctionCallOutputItemListParam:
        """Converts a tool return value into an output acceptable by the Responses API."""

        # If the output is either a single or list of the known structured output types, convert to
        # ResponseFunctionCallOutputItemListParam. Else, just stringify.
        if isinstance(output, (list, tuple)):
            maybe_converted_output_list = [
                cls._maybe_get_output_as_structured_function_output(item) for item in output
            ]
            if all(maybe_converted_output_list):
                return [
                    cls._convert_single_tool_output_pydantic_model(item)
                    for item in maybe_converted_output_list
                    if item is not None
                ]
            else:
                return str(output)
        else:
            maybe_converted_output = cls._maybe_get_output_as_structured_function_output(output)
            if maybe_converted_output:
                return [cls._convert_single_tool_output_pydantic_model(maybe_converted_output)]
            else:
                return str(output)

    @classmethod
    def _maybe_get_output_as_structured_function_output(
        cls, output: Any
    ) -> ValidToolOutputPydanticModels | None:
        if isinstance(output, (ToolOutputText, ToolOutputImage, ToolOutputFileContent)):
            return output
        elif isinstance(output, dict):
            # Require explicit 'type' field in dict to be considered a structured output
            if "type" not in output:
                return None
            try:
                return ValidToolOutputPydanticModelsTypeAdapter.validate_python(output)
            except pydantic.ValidationError:
                logger.debug("dict was not a valid tool output pydantic model")
                return None

        return None

    @classmethod
    def _convert_single_tool_output_pydantic_model(
        cls, output: ValidToolOutputPydanticModels
    ) -> ResponseFunctionCallOutputItemParam:
        if isinstance(output, ToolOutputText):
            return {"type": "input_text", "text": output.text}
        elif isinstance(output, ToolOutputImage):
            # Forward all provided optional fields so the Responses API receives
            # the correct identifiers and settings for the image resource.
            result: ResponseInputImageContentParam = {"type": "input_image"}
            if output.image_url is not None:
                result["image_url"] = output.image_url
            if output.file_id is not None:
                result["file_id"] = output.file_id
            if output.detail is not None:
                result["detail"] = output.detail
            return result
        elif isinstance(output, ToolOutputFileContent):
            # Forward all provided optional fields so the Responses API receives
            # the correct identifiers and metadata for the file resource.
            result_file: ResponseInputFileContentParam = {"type": "input_file"}
            if output.file_data is not None:
                result_file["file_data"] = output.file_data
            if output.file_url is not None:
                result_file["file_url"] = output.file_url
            if output.file_id is not None:
                result_file["file_id"] = output.file_id
            if output.filename is not None:
                result_file["filename"] = output.filename
            return result_file
        else:
            assert_never(output)
            raise ValueError(f"Unexpected tool output type: {output}")
