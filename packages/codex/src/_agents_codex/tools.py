"""Captured Python function declarations and native result conversion."""

from __future__ import annotations

import inspect as inspect
import json as json
from collections.abc import Awaitable as Awaitable, Callable as Callable
from dataclasses import dataclass as dataclass, field as field
from typing import TYPE_CHECKING as TYPE_CHECKING, Any as Any, Literal as Literal

from pydantic import (
    BaseModel as BaseModel,
    ConfigDict as ConfigDict,
    TypeAdapter as TypeAdapter,
    create_model as create_model,
)

from ._events import FunctionCallResponse as FunctionCallResponse
from ._public import preserve_module_identity as _preserve_module_identity
from .manual import _CapturedManual as _CapturedManual

if TYPE_CHECKING:
    from ._function_tools import ToolContext
    from .native_interception import Allow, Deny, ReplaceArguments

_TOOL_RESPONSE = TypeAdapter(FunctionCallResponse)


@dataclass(frozen=True)
class Checklist:
    """Enable Core's update_plan for this Session at open.

    Put Checklist() in Agent.tools. Operation and step selections may retain
    that same declaration or its ToolRef, or omit it to disable update_plan.
    Session.checklist() reads the native list, including on read-only restore
    without this declaration. Saved list data grants no tool access.
    """


@dataclass(frozen=True, kw_only=True)
class WebSearch:
    """Allow the native web search tool for this Session.

    Put WebSearch() in Agent.tools. Omission disables both
    native search and provider-hosted search. Cached uses indexed content; live
    permits native Core to request external web access. The route selects the
    corresponding native provider protocol, which the current provider must
    support. It grants no other tools. An operation/step cannot add or change
    its binding. A selection may omit it to disable search for that step.
    """

    mode: Literal["cached", "live"] = "cached"
    route: Literal["standalone", "hosted"] = "standalone"


@dataclass(frozen=True)
class NativeGoals:
    """Allow Core's create_goal, get_goal and update_goal in LocalStorage.

    This is an opening capability in Agent.tools. The destination must
    declare it again on local reopen. Operation and step selections may omit
    it to disable goal tools. Without LocalStorage, opening fails.
    This does not enable Checklist or save an application callback.
    """


@dataclass(frozen=True)
class ToolRef:
    """The native identity of one admitted tool; bare Python tools have no namespace."""

    name: str
    namespace: str | None = None


@dataclass(frozen=True)
class ToolCallInfo:
    """Observed identity, never authority to execute or answer a tool call.

    Calls made in generated code can lack a provider call ID. Their outer
    call, cell and nested runtime IDs remain separate. For Python function
    tools called by generated code, Core provides the cell and nested IDs
    but not the initiating outer provider call ID: both call_id and
    outer_call_id are None. Unknown native role or model attribution stays
    None. Reply IDs and grants remain on their original owners.
    """

    tool: ToolRef
    session_id: str
    turn_id: str
    call_id: str | None
    child_id: str | None
    child_incarnation: str | None
    role: str | None
    model: str | None
    route: Literal["direct", "code"]
    outer_call_id: str | None = None
    cell_id: str | None = None
    nested_call_id: str | None = None


@dataclass(frozen=True)
class ToolOutputText:
    """Text returned to the native model and history."""

    text: str


@dataclass(frozen=True)
class ToolOutputImage:
    """An inline base64 image data URL, decoded and processed by Core."""

    image_url: str

    def __post_init__(self) -> None:
        if not isinstance(self.image_url, str):
            raise TypeError("ToolOutputImage requires a base64 image data URL")
        header, separator, data = self.image_url.partition(",")
        if not (
            header.startswith("data:image/") and header.endswith(";base64") and separator and data
        ):
            raise ValueError(
                "ToolOutputImage requires a base64 image data URL; remote URLs, "
                "file IDs and local paths are unsupported"
            )


@dataclass
class ToolOutput:
    """A mutable native response of content items, with optional explicit failure."""

    content_items: list[dict[str, Any] | ToolOutputText | ToolOutputImage] | list[dict[str, Any]]
    success: bool = True

    @classmethod
    def text(cls, value: str) -> ToolOutput:
        items: list[dict[str, Any] | ToolOutputText | ToolOutputImage] = [ToolOutputText(value)]
        return cls(items)

    @classmethod
    def image(cls, image_url: str) -> ToolOutput:
        items: list[dict[str, Any] | ToolOutputText | ToolOutputImage] = [
            ToolOutputImage(image_url)
        ]
        return cls(items)

    @classmethod
    def error(cls, message: str) -> ToolOutput:
        items: list[dict[str, Any] | ToolOutputText | ToolOutputImage] = [ToolOutputText(message)]
        return cls(items, success=False)

    def to_native(self) -> dict[str, Any]:
        parts: list[dict[str, Any]] = []
        for part in self.content_items:
            if isinstance(part, ToolOutputText):
                parts.append({"type": "inputText", "text": part.text})
            elif isinstance(part, ToolOutputImage):
                parts.append({"type": "inputImage", "imageUrl": part.image_url})
            else:
                parts.append(part)
        return {"contentItems": parts, "success": self.success}

    def _validate(self) -> None:
        _TOOL_RESPONSE.validate_python(self.to_native(), strict=True)


@dataclass(frozen=True)
class _CapturedFunction:
    """One immutable snapshot for schema publication and callback execution."""

    name: str
    spec_json: str
    model: type[BaseModel]
    field_names: tuple[str, ...]
    output_adapter: TypeAdapter[Any] = field(repr=False)
    callback: Callable[..., Any]
    context_parameter: inspect.Parameter | None
    callback_timeout: float | None
    before: (
        Callable[
            [ToolContext[Any]],
            Allow | Deny | ReplaceArguments | Awaitable[Allow | Deny | ReplaceArguments],
        ]
        | None
    )
    after: Callable[[ToolContext[Any], ToolOutput], ToolOutput | Awaitable[ToolOutput]] | None
    context: Any = field(repr=False)
    namespace: None = field(default=None, init=False)

    def _parse_arguments(self, arguments: Any) -> BaseModel:
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        return self.model.model_validate(arguments)

    def _arguments(self, parsed: BaseModel) -> dict[str, Any]:
        return {name: getattr(parsed, name) for name in self.field_names}


@dataclass(frozen=True)
class _ToolCatalog:
    generation: int
    tools: tuple[_CapturedFunction | _CapturedManual, ...]

    def manual_slots(self) -> list[int]:
        return [slot for slot, tool in enumerate(self.tools) if isinstance(tool, _CapturedManual)]

    def native_specs(self) -> str:
        return json.dumps([json.loads(tool.spec_json) for tool in self.tools])


def _function_parameters(
    callback: Callable[..., Any], *, name: str, hints: dict[str, Any], skip_first: bool = False
) -> type[BaseModel]:
    """Build the one argument model shared by native function declarations."""
    fields: dict[str, Any] = {}
    parameters = list(inspect.signature(callback).parameters.items())
    for parameter_name, parameter in parameters[1 if skip_first else 0 :]:
        if parameter.kind not in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            raise TypeError(f"Tool parameter {parameter_name!r} must accept a keyword argument")
        if parameter_name not in hints:
            raise TypeError(f"Tool parameter {parameter_name!r} needs a type annotation")
        default = ... if parameter.default is inspect.Parameter.empty else parameter.default
        fields[parameter_name] = (hints[parameter_name], default)
    model: type[BaseModel] = create_model(
        f"{name}_arguments", __config__=ConfigDict(extra="forbid"), **fields
    )
    omitted = fields.keys() - model.model_fields.keys()
    if omitted:
        raise TypeError(
            f"Tool parameter {min(omitted)!r} cannot be a model field; "
            "use a Python name without a leading underscore and Field(alias=...) "
            "for its JSON name"
        )
    return model


_preserve_module_identity(globals(), "agents.tools")
