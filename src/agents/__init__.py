"""Python agents with a native Codex model loop and explicit session ownership."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._function_tools import (
        FunctionTool as FunctionTool,
        Tool as Tool,
        ToolContext as ToolContext,
        function_tool as function_tool,
    )
    from .agent import (
        Agent as Agent,
        CodexAccount as CodexAccount,
        CodexLogin as CodexLogin,
        ModelSettings as ModelSettings,
        NativeProvider as NativeProvider,
    )
    from .canonical_storage import (
        CustomStorage as CustomStorage,
        SessionStore as SessionStore,
        SQLiteSessionStore as SQLiteSessionStore,
    )
    from .executor import (
        Environment as Environment,
        EnvironmentSource as EnvironmentSource,
        ExecutorContext as ExecutorContext,
        NetworkPolicy as NetworkPolicy,
    )
    from .input import (
        InputImage as InputImage,
        InputText as InputText,
        UserInput as UserInput,
        UserMessage as UserMessage,
    )
    from .manual import (
        ActionReply as ActionReply,
        ActionRequested as ActionRequested,
        ApprovalRequest as ApprovalRequest,
        ElicitationRequest as ElicitationRequest,
        Interaction as Interaction,
        VerificationProof as VerificationProof,
        VerificationRequest as VerificationRequest,
    )
    from .mcp import (
        ChatGPTAppsMCP as ChatGPTAppsMCP,
        HostedVerificationMCP as HostedVerificationMCP,
        HttpMCP as HttpMCP,
        MCPCallContext as MCPCallContext,
        MCPResultContext as MCPResultContext,
        StdioMCP as StdioMCP,
    )
    from .native_agents import (
        ChildInvocation as ChildInvocation,
        NativeAgents as NativeAgents,
        NativeRole as NativeRole,
    )
    from .native_interception import (
        Allow as Allow,
        Deny as Deny,
        NativeToolCall as NativeToolCall,
        ReplaceArguments as ReplaceArguments,
        StopTurn as StopTurn,
    )
    from .rate_limits import (
        ApiRateLimits as ApiRateLimits,
        ApiRateLimitWindow as ApiRateLimitWindow,
    )
    from .recovery import (
        LocalRecoveryRequired as LocalRecoveryRequired,
        ReconciledCall as ReconciledCall,
        UncertainCall as UncertainCall,
    )
    from .schema import (
        NativeOutput as NativeOutput,
        OutputSpec as OutputSpec,
        OutputValidationError as OutputValidationError,
        PromptedOutput as PromptedOutput,
    )
    from .session import (
        Capabilities as Capabilities,
        CapabilityError as CapabilityError,
        Completed as Completed,
        FeatureName as FeatureName,
        FeatureSupport as FeatureSupport,
        InteractionExpiredError as InteractionExpiredError,
        InteractionRequired as InteractionRequired,
        NativeError as NativeError,
        ResponseIntegrityReason as ResponseIntegrityReason,
        Session as Session,
        SessionBusyError as SessionBusyError,
        StaleTurnError as StaleTurnError,
        TurnStoppedError as TurnStoppedError,
    )
    from .state import (
        ChecklistState as ChecklistState,
        ChecklistStep as ChecklistStep,
        CheckpointStatus as CheckpointStatus,
        LocalStorage as LocalStorage,
        NativeGoal as NativeGoal,
        SessionState as SessionState,
    )
    from .subagents import Subagent as Subagent
    from .tools import (
        Checklist as Checklist,
        NativeGoals as NativeGoals,
        ToolCallInfo as ToolCallInfo,
        ToolOutput as ToolOutput,
        ToolOutputImage as ToolOutputImage,
        ToolOutputText as ToolOutputText,
        ToolRef as ToolRef,
        WebSearch as WebSearch,
    )
    from .turn import (
        PendingRequests as PendingRequests,
        SteeringReceipt as SteeringReceipt,
        Turn as Turn,
        TurnNeedsAttention as TurnNeedsAttention,
    )
    from .turn_receipt import (
        ExecutorNetworkReceipt as ExecutorNetworkReceipt,
        ExecutorPolicyReceipt as ExecutorPolicyReceipt,
        InstructionFileReceipt as InstructionFileReceipt,
        TurnReceipt as TurnReceipt,
    )

_MODULES = {
    "Allow": ".native_interception",
    "Deny": ".native_interception",
    "ReplaceArguments": ".native_interception",
    "NativeToolCall": ".native_interception",
    "StopTurn": ".native_interception",
    "ExecutorContext": ".executor",
    "ChildInvocation": ".native_agents",
    "NativeAgents": ".native_agents",
    "NativeRole": ".native_agents",
    "Environment": ".executor",
    "EnvironmentSource": ".executor",
    "NetworkPolicy": ".executor",
    "Checklist": ".tools",
    "NativeGoals": ".tools",
    "NativeGoal": ".state",
    "WebSearch": ".tools",
    "SessionState": ".state",
    "ChecklistState": ".state",
    "ChecklistStep": ".state",
    "ActionReply": ".manual",
    "ActionRequested": ".manual",
    "ApprovalRequest": ".manual",
    "ElicitationRequest": ".manual",
    "VerificationRequest": ".manual",
    "VerificationProof": ".manual",
    "Interaction": ".manual",
    "Agent": ".agent",
    "CodexAccount": ".agent",
    "CodexLogin": ".agent",
    "ModelSettings": ".agent",
    "NativeProvider": ".agent",
    "NativeOutput": ".schema",
    "PromptedOutput": ".schema",
    "OutputSpec": ".schema",
    "OutputValidationError": ".schema",
    "CheckpointStatus": ".state",
    "LocalStorage": ".state",
    "CustomStorage": ".canonical_storage",
    "SessionStore": ".canonical_storage",
    "SQLiteSessionStore": ".canonical_storage",
    "LocalRecoveryRequired": ".recovery",
    "ReconciledCall": ".recovery",
    "UncertainCall": ".recovery",
    "ApiRateLimits": ".rate_limits",
    "ApiRateLimitWindow": ".rate_limits",
    "InputImage": ".input",
    "InputText": ".input",
    "UserMessage": ".input",
    "UserInput": ".input",
    "MCPResultContext": ".mcp",
    "StdioMCP": ".mcp",
    "HttpMCP": ".mcp",
    "HostedVerificationMCP": ".mcp",
    "ChatGPTAppsMCP": ".mcp",
    "FunctionTool": "._function_tools",
    "Tool": "._function_tools",
    "ToolRef": ".tools",
    "ToolOutput": ".tools",
    "ToolOutputText": ".tools",
    "ToolOutputImage": ".tools",
    "ToolContext": "._function_tools",
    "ToolCallInfo": ".tools",
    "MCPCallContext": ".mcp",
    "function_tool": "._function_tools",
    "Completed": ".session",
    "Capabilities": ".session",
    "CapabilityError": ".session",
    "FeatureName": ".session",
    "FeatureSupport": ".session",
    "InteractionExpiredError": ".session",
    "StaleTurnError": ".session",
    "TurnStoppedError": ".session",
    "InteractionRequired": ".session",
    "NativeError": ".session",
    "ResponseIntegrityReason": ".session",
    "Session": ".session",
    "SessionBusyError": ".session",
    "Turn": ".turn",
    "PendingRequests": ".turn",
    "SteeringReceipt": ".turn",
    "TurnNeedsAttention": ".turn",
    "ExecutorNetworkReceipt": ".turn_receipt",
    "ExecutorPolicyReceipt": ".turn_receipt",
    "InstructionFileReceipt": ".turn_receipt",
    "TurnReceipt": ".turn_receipt",
    "Subagent": ".subagents",
}

__all__ = list(_MODULES)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


def __getattr__(name: str) -> Any:
    module = _MODULES.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value
