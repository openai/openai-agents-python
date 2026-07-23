from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from .._config_coercion import coerce_pydantic_config
from ..agent import Agent
from ..run_context import RunContextWrapper, TContext
from .capabilities import Capability
from .capabilities.capabilities import Capabilities
from .manifest import Manifest, _coerce_manifest
from .types import User

if TYPE_CHECKING:
    from ..agent import MCPConfig, StopAtTools, ToolsToFinalOutputFunction
    from ..agent_output import AgentOutputSchemaBase
    from ..guardrail import InputGuardrail, OutputGuardrail
    from ..handoffs import Handoff
    from ..lifecycle import AgentHooks
    from ..mcp import MCPServer
    from ..model_settings import ModelSettings
    from ..models.interface import Model
    from ..prompts import DynamicPromptFunction, Prompt
    from ..tool import Tool
    from ..util._types import MaybeAwaitable


@dataclass
class SandboxAgent(Agent[TContext]):
    """An `Agent` with sandbox-specific configuration.

    Runtime transport details such as the sandbox client, client options, and live session are
    provided at run time through `RunConfig(sandbox=...)`, not stored on the agent itself.
    """

    default_manifest: Manifest | None = None
    """Default sandbox manifest for new sessions created by `Runner` sandbox execution."""

    base_instructions: (
        str
        | Callable[
            [RunContextWrapper[TContext], Agent[TContext]], Awaitable[str | None] | str | None
        ]
        | None
    ) = None
    """Override for the SDK sandbox base prompt. Most callers should use `instructions`."""

    capabilities: Sequence[Capability] = field(default_factory=Capabilities.default)
    """Sandbox capabilities that can mutate the manifest, add instructions, and expose tools."""

    run_as: User | str | None = None
    """User identity used for model-facing sandbox tools such as shell, file reads, and patches."""

    _sandbox_concurrency_guard: object | None = field(default=None, init=False, repr=False)

    if TYPE_CHECKING:

        def __init__(
            self,
            name: str,
            handoff_description: str | None = None,
            tools: list[Tool] = ...,
            mcp_servers: list[MCPServer] = ...,
            mcp_config: MCPConfig = ...,
            instructions: (
                str
                | Callable[
                    [RunContextWrapper[TContext], Agent[TContext]],
                    MaybeAwaitable[str],
                ]
                | None
            ) = None,
            prompt: Prompt | DynamicPromptFunction | None = None,
            handoffs: list[Agent[Any] | Handoff[TContext, Any]] = ...,
            model: str | Model | None = None,
            model_settings: ModelSettings | dict[str, Any] = ...,
            input_guardrails: list[InputGuardrail[TContext]] = ...,
            output_guardrails: list[OutputGuardrail[TContext]] = ...,
            output_type: type[Any] | AgentOutputSchemaBase | None = None,
            hooks: AgentHooks[TContext] | None = None,
            tool_use_behavior: (
                Literal["run_llm_again", "stop_on_first_tool"]
                | StopAtTools
                | ToolsToFinalOutputFunction
            ) = "run_llm_again",
            reset_tool_choice: bool = True,
            default_manifest: Manifest | dict[str, Any] | None = None,
            base_instructions: (
                str
                | Callable[
                    [RunContextWrapper[TContext], Agent[TContext]],
                    Awaitable[str | None] | str | None,
                ]
                | None
            ) = None,
            capabilities: Sequence[Capability] = ...,
            run_as: User | dict[str, Any] | str | None = None,
        ) -> None: ...

    def __post_init__(self) -> None:
        super().__post_init__()
        if isinstance(self.default_manifest, dict):
            self.default_manifest = _coerce_manifest(
                self.default_manifest, parameter_name="sandbox.default_manifest"
            )
        if isinstance(self.run_as, dict):
            self.run_as = coerce_pydantic_config(self.run_as, User, parameter_name="sandbox.run_as")
        if (
            self.base_instructions is not None
            and not isinstance(self.base_instructions, str)
            and not callable(self.base_instructions)
        ):
            raise TypeError(
                f"SandboxAgent base_instructions must be a string, callable, or None, "
                f"got {type(self.base_instructions).__name__}"
            )
        if self.run_as is not None and not isinstance(self.run_as, str | User):
            raise TypeError(
                f"SandboxAgent run_as must be a string, User, or None, "
                f"got {type(self.run_as).__name__}"
            )
