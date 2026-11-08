"""Python agents with a native Codex model loop and explicit session ownership."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._function_tools import (
        FunctionTool as FunctionTool,
        Tool as Tool,
        ToolContext as ToolContext,
        ToolTimeoutBehavior as ToolTimeoutBehavior,
        ToolTimeoutError as ToolTimeoutError,
        function_tool as function_tool,
    )
    from ._trace_context import TraceContext as TraceContext
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
        SessionStoreCleanup as SessionStoreCleanup,
        SQLiteSessionStore as SQLiteSessionStore,
    )
    from .compaction import CompactionContext as CompactionContext
    from .completion import CompletionContext as CompletionContext, ContinueTurn as ContinueTurn
    from .executor import (
        Environment as Environment,
        EnvironmentSource as EnvironmentSource,
        ExecutorContext as ExecutorContext,
        LocalStorageMount as LocalStorageMount,
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
        FormRequest as FormRequest,
        Interaction as Interaction,
        UrlRequest as UrlRequest,
        VerificationProof as VerificationProof,
        VerificationRequest as VerificationRequest,
    )
    from .mcp import (
        ChatGPTAppsMCP as ChatGPTAppsMCP,
        HostedVerificationMCP as HostedVerificationMCP,
        HttpMCP as HttpMCP,
        MCPBlobResourceContents as MCPBlobResourceContents,
        MCPCallContext as MCPCallContext,
        MCPListResourcesResult as MCPListResourcesResult,
        MCPListResourceTemplatesResult as MCPListResourceTemplatesResult,
        MCPReadResourceResult as MCPReadResourceResult,
        MCPResource as MCPResource,
        MCPResourceTemplate as MCPResourceTemplate,
        MCPResultContext as MCPResultContext,
        MCPTextResourceContents as MCPTextResourceContents,
        StdioMCP as StdioMCP,
    )
    from .model_context import (
        ModelContextSnapshot as ModelContextSnapshot,
        ModelRequestContext as ModelRequestContext,
    )
    from .native_agents import (
        ChildInvocation as ChildInvocation,
        ChildStepContext as ChildStepContext,
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
    from .prepared_request import (
        PreparedRequest as PreparedRequest,
        PreparedToolPresentation as PreparedToolPresentation,
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
        OutputValidationIssue as OutputValidationIssue,
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
        get_cleanup as get_cleanup,
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
        CodeInterpreter as CodeInterpreter,
        FileSearch as FileSearch,
        ImageGeneration as ImageGeneration,
        NativeGoals as NativeGoals,
        ToolCallInfo as ToolCallInfo,
        ToolOutput as ToolOutput,
        ToolOutputImage as ToolOutputImage,
        ToolOutputText as ToolOutputText,
        ToolRef as ToolRef,
        WebSearch as WebSearch,
    )
    from .turn import (
        ChildTurn as ChildTurn,
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
    from .usage import RunBudget as RunBudget

_MODULES = {
    "CompactionContext": ".compaction",
    "CompletionContext": ".completion",
    "ContinueTurn": ".completion",
    "ModelContextSnapshot": ".model_context",
    "ModelRequestContext": ".model_context",
    "RunBudget": ".usage",
    "PreparedRequest": ".prepared_request",
    "PreparedToolPresentation": ".prepared_request",
    "TraceContext": "._trace_context",
    "Allow": ".native_interception",
    "Deny": ".native_interception",
    "ReplaceArguments": ".native_interception",
    "NativeToolCall": ".native_interception",
    "StopTurn": ".native_interception",
    "ExecutorContext": ".executor",
    "ChildInvocation": ".native_agents",
    "ChildStepContext": ".native_agents",
    "NativeAgents": ".native_agents",
    "NativeRole": ".native_agents",
    "Environment": ".executor",
    "LocalStorageMount": ".executor",
    "EnvironmentSource": ".executor",
    "NetworkPolicy": ".executor",
    "Checklist": ".tools",
    "NativeGoals": ".tools",
    "NativeGoal": ".state",
    "WebSearch": ".tools",
    "FileSearch": ".tools",
    "CodeInterpreter": ".tools",
    "ImageGeneration": ".tools",
    "SessionState": ".state",
    "ChecklistState": ".state",
    "ChecklistStep": ".state",
    "ActionReply": ".manual",
    "ActionRequested": ".manual",
    "ApprovalRequest": ".manual",
    "FormRequest": ".manual",
    "UrlRequest": ".manual",
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
    "OutputValidationIssue": ".schema",
    "CheckpointStatus": ".state",
    "LocalStorage": ".state",
    "CustomStorage": ".canonical_storage",
    "SessionStore": ".canonical_storage",
    "SessionStoreCleanup": ".canonical_storage",
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
    "MCPResource": ".mcp",
    "MCPResourceTemplate": ".mcp",
    "MCPTextResourceContents": ".mcp",
    "MCPBlobResourceContents": ".mcp",
    "MCPListResourcesResult": ".mcp",
    "MCPListResourceTemplatesResult": ".mcp",
    "MCPReadResourceResult": ".mcp",
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
    "ToolTimeoutBehavior": "._function_tools",
    "ToolTimeoutError": "._function_tools",
    "ToolCallInfo": ".tools",
    "MCPCallContext": ".mcp",
    "function_tool": "._function_tools",
    "Completed": ".session",
    "get_cleanup": ".session",
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
    "ChildTurn": ".turn",
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
