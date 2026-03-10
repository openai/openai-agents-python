from __future__ import annotations

import abc
import json
import weakref
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Generic, Literal, TypeVar, Union, cast

import pydantic
from openai.types.responses import (
    Response,
    ResponseComputerToolCall,
    ResponseFileSearchToolCall,
    ResponseFunctionShellToolCallOutput,
    ResponseFunctionToolCall,
    ResponseFunctionWebSearch,
    ResponseInputItemParam,
    ResponseOutputItem,
    ResponseOutputMessage,
    ResponseOutputRefusal,
    ResponseOutputText,
    ResponseStreamEvent,
    ResponseToolSearchCall,
    ResponseToolSearchOutputItem,
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

from ._tool_identity import FunctionToolLookupKey, get_function_tool_lookup_key, tool_trace_name
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
from .util._json import _to_dump_compatible

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

T = TypeVar("T", bound=Union[TResponseOutputItem, TResponseInputItem, dict[str, Any]])
ToolSearchCallRawItem: TypeAlias = ResponseToolSearchCall | dict[str, Any]
ToolSearchOutputRawItem: TypeAlias = ResponseToolSearchOutputItem | dict[str, Any]

# Distinguish a missing dict entry from an explicit None value.
_MISSING_ATTR_SENTINEL = object()


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
class ToolSearchCallItem(RunItemBase[ToolSearchCallRawItem]):
    """Represents a Responses API tool search request emitted by the model."""

    raw_item: ToolSearchCallRawItem
    """The raw tool search call item, preserving partial dict snapshots when needed."""

    type: Literal["tool_search_call_item"] = "tool_search_call_item"

    def to_input_item(self) -> TResponseInputItem:
        """Convert the tool search call into a replayable Responses input item."""
        return _tool_search_item_to_input_item(self.raw_item)


@dataclass
class ToolSearchOutputItem(RunItemBase[ToolSearchOutputRawItem]):
    """Represents the output of a Responses API tool search."""

    raw_item: ToolSearchOutputRawItem
    """The raw tool search output item, preserving partial dict snapshots when needed."""

    type: Literal["tool_search_output_item"] = "tool_search_output_item"

    def to_input_item(self) -> TResponseInputItem:
        """Convert the tool search output into a replayable Responses input item."""
        return _tool_search_item_to_input_item(self.raw_item)


def _tool_search_item_to_input_item(
    raw_item: ToolSearchCallRawItem | ToolSearchOutputRawItem,
) -> TResponseInputItem:
    """Strip output-only tool_search fields before replaying items back to the API."""
    if isinstance(raw_item, dict):
        payload = dict(raw_item)
    elif isinstance(raw_item, BaseModel):
        payload = raw_item.model_dump(exclude_unset=True)
    else:
        raise AgentsException(f"Unexpected raw item type: {type(raw_item)}")

    payload.pop("created_by", None)
    return cast(TResponseInputItem, payload)


def _output_item_to_input_item(raw_item: Any) -> TResponseInputItem:
    """Convert an output item into replayable input, normalizing tool_search items."""
    item_type = (
        raw_item.get("type") if isinstance(raw_item, dict) else getattr(raw_item, "type", None)
    )
    if item_type in {"tool_search_call", "tool_search_output"}:
        return _tool_search_item_to_input_item(raw_item)

    if isinstance(raw_item, dict):
        return cast(TResponseInputItem, dict(raw_item))
    if isinstance(raw_item, BaseModel):
        return cast(TResponseInputItem, raw_item.model_dump(exclude_unset=True))

    raise AgentsException(f"Unexpected raw item type: {type(raw_item)}")


def _copy_tool_search_mapping(raw_item: Mapping[str, Any]) -> dict[str, Any]:
    copied = dict(raw_item)
    copied_type = copied.get("type")
    if isinstance(copied_type, str):
        copied["type"] = copied_type
    return copied


def coerce_tool_search_call_raw_item(raw_item: Any) -> ToolSearchCallRawItem:
    """Prefer the typed SDK tool_search call model while tolerating partial snapshots."""
    if isinstance(raw_item, ResponseToolSearchCall):
        return raw_item
    if isinstance(raw_item, Mapping):
        copied = _copy_tool_search_mapping(raw_item)
        if copied.get("type") != "tool_search_call":
            raise AgentsException(f"Unexpected tool search call item type: {copied.get('type')!r}")
        try:
            return ResponseToolSearchCall.model_validate(copied)
        except pydantic.ValidationError:
            return copied
    raise AgentsException(f"Unexpected tool search call item type: {type(raw_item)}")


def coerce_tool_search_output_raw_item(raw_item: Any) -> ToolSearchOutputRawItem:
    """Prefer the typed SDK tool_search output model while tolerating partial snapshots."""
    if isinstance(raw_item, ResponseToolSearchOutputItem):
        return raw_item
    if isinstance(raw_item, Mapping):
        copied = _copy_tool_search_mapping(raw_item)
        if copied.get("type") != "tool_search_output":
            raise AgentsException(
                f"Unexpected tool search output item type: {copied.get('type')!r}"
            )
        try:
            return ResponseToolSearchOutputItem.model_validate(copied)
        except pydantic.ValidationError:
            return copied
    raise AgentsException(f"Unexpected tool search output item type: {type(raw_item)}")


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

    description: str | None = None
    """Optional tool description if known at item creation time."""

    title: str | None = None
    """Optional short display label if known at item creation time."""


ToolCallOutputTypes: TypeAlias = Union[
    FunctionCallOutput,
    ComputerCallOutput,
    LocalShellCallOutput,
    ResponseFunctionShellToolCallOutput,
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
        """

        if isinstance(self.raw_item, dict):
            payload = dict(self.raw_item)
            payload_type = payload.get("type")
            if payload_type == "shell_call_output":
                payload = dict(payload)
                payload.pop("status", None)
                payload.pop("shell_output", None)
                payload.pop("provider_data", None)
                outputs = payload.get("output")
                if isinstance(outputs, list):
                    for entry in outputs:
                        if not isinstance(entry, dict):
                            continue
                        outcome = entry.get("outcome")
                        if isinstance(outcome, dict):
                            if outcome.get("type") == "exit":
                                entry["outcome"] = outcome
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


@dataclass
class CompactionItem(RunItemBase[TResponseInputItem]):
    """Represents a compaction item from responses.compact."""

    type: Literal["compaction_item"] = "compaction_item"

    def to_input_item(self) -> TResponseInputItem:
        """Converts this item into an input item suitable for passing to the model."""
        return self.raw_item


# Union type for tool approval raw items - supports function tools, hosted tools, shell tools, etc.
ToolApprovalRawItem: TypeAlias = Union[
    ResponseFunctionToolCall,
    McpCall,
    McpApprovalRequest,
    LocalShellCall,
    dict[str, Any],  # For flexibility with other tool types
]


@dataclass
class ToolApprovalItem(RunItemBase[Any]):
    """Tool call that requires approval before execution."""

    raw_item: ToolApprovalRawItem
    """Raw tool call awaiting approval (function, hosted, shell, etc.)."""

    tool_name: str | None = None
    """Tool name for approval tracking; falls back to raw_item.name when absent."""

    _allow_bare_name_alias: bool = field(default=False, kw_only=True, repr=False)
    """Whether permanent approval decisions should also be recorded under the bare tool name."""

    # Keep `type` ahead of `tool_namespace` to preserve the historical 4-argument positional
    # constructor shape: `(agent, raw_item, tool_name, type)`.
    type: Literal["tool_approval_item"] = "tool_approval_item"

    tool_namespace: str | None = None
    """Optional Responses API namespace for function-tool approvals."""

    tool_lookup_key: FunctionToolLookupKey | None = field(
        default=None,
        kw_only=True,
        repr=False,
    )
    """Canonical function-tool lookup metadata when the approval targets a function tool."""

    def __post_init__(self) -> None:
        """Populate tool_name from the raw item if not provided."""
        if self.tool_name is None:
            # Extract name from raw_item - handle different types
            if isinstance(self.raw_item, dict):
                self.tool_name = self.raw_item.get("name")
            elif hasattr(self.raw_item, "name"):
                self.tool_name = self.raw_item.name
            else:
                self.tool_name = None
        if self.tool_namespace is None:
            if isinstance(self.raw_item, dict):
                namespace = self.raw_item.get("namespace")
            else:
                namespace = getattr(self.raw_item, "namespace", None)
            self.tool_namespace = namespace if isinstance(namespace, str) else None
        if self.tool_lookup_key is None:
            if isinstance(self.raw_item, dict):
                raw_type = self.raw_item.get("type")
            else:
                raw_type = getattr(self.raw_item, "type", None)
            if (
                raw_type == "function_call"
                and self.tool_name is not None
                and (self.tool_namespace is None or self.tool_namespace != self.tool_name)
            ):
                self.tool_lookup_key = get_function_tool_lookup_key(
                    self.tool_name,
                    self.tool_namespace,
                )

    def __hash__(self) -> int:
        """Hash by object identity to keep distinct approvals separate."""
        return object.__hash__(self)

    def __eq__(self, other: object) -> bool:
        """Equality is based on object identity."""
        return self is other

    @property
    def name(self) -> str | None:
        """Return the tool name from tool_name or raw_item (backwards compatible)."""
        if self.tool_name:
            return self.tool_name
        if isinstance(self.raw_item, dict):
            candidate = self.raw_item.get("name") or self.raw_item.get("tool_name")
        else:
            candidate = getattr(self.raw_item, "name", None) or getattr(
                self.raw_item, "tool_name", None
            )
        return str(candidate) if candidate is not None else None

    @property
    def qualified_name(self) -> str | None:
        """Return a display-friendly tool name, collapsing synthetic deferred namespaces."""
        if self.tool_name is None:
            return None
        return tool_trace_name(self.tool_name, self.tool_namespace) or self.tool_name

    @property
    def arguments(self) -> str | None:
        """Return tool call arguments if present on the raw item."""
        candidate: Any | None = None
        if isinstance(self.raw_item, dict):
            candidate = self.raw_item.get("arguments")
            if candidate is None:
                candidate = self.raw_item.get("params") or self.raw_item.get("input")
        elif hasattr(self.raw_item, "arguments"):
            candidate = self.raw_item.arguments
        elif hasattr(self.raw_item, "params") or hasattr(self.raw_item, "input"):
            candidate = getattr(self.raw_item, "params", None) or getattr(
                self.raw_item, "input", None
            )
        if candidate is None:
            return None
        if isinstance(candidate, str):
            return candidate
        try:
            return json.dumps(candidate)
        except (TypeError, ValueError):
            return str(candidate)

    def _extract_call_id(self) -> str | None:
        """Return call identifier from the raw item."""
        if isinstance(self.raw_item, dict):
            return self.raw_item.get("call_id") or self.raw_item.get("id")
        return getattr(self.raw_item, "call_id", None) or getattr(self.raw_item, "id", None)

    @property
    def call_id(self) -> str | None:
        """Return call identifier from the raw item."""
        return self._extract_call_id()

    def to_input_item(self) -> TResponseInputItem:
        """ToolApprovalItem should never be sent as input; raise to surface misuse."""
        raise AgentsException(
            "ToolApprovalItem cannot be converted to an input item. "
            "These items should be filtered out before preparing input for the API."
        )


RunItem: TypeAlias = Union[
    MessageOutputItem,
    ToolSearchCallItem,
    ToolSearchOutputItem,
    HandoffCallItem,
    HandoffOutputItem,
    ToolCallItem,
    ToolCallOutputItem,
    CompactionItem,
    ReasoningItem,
    MCPListToolsItem,
    MCPApprovalRequestItem,
    MCPApprovalResponseItem,
    CompactionItem,
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

    request_id: str | None = None
    """The transport request ID for this model call, if provided by the model SDK."""

    def to_input_items(self) -> list[TResponseInputItem]:
        """Convert the output into a list of input items suitable for passing to the model."""
        # Most output items can be replayed via a direct model_dump. Tool-search items carry
        # output-only metadata such as `created_by`, so they must go through the same replay
        # sanitizer used elsewhere in the runtime.
        return [_output_item_to_input_item(it) for it in self.output]


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
        return cast(list[TResponseInputItem], _to_dump_compatible(input))

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
