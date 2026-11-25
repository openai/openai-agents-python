"""Deterministic test doubles for Agents SDK workflows."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

from .model import (
    InvalidModelStep,
    ModelCall,
    ModelScriptError,
    ModelStep,
    ModelStepSpec,
    ScriptedModel,
    UnconsumedModelSteps,
    UnexpectedModelCall,
    assistant_message,
    function_call,
)

if TYPE_CHECKING:
    from .sandbox import (
        InvalidSandboxStep,
        SandboxCall,
        SandboxCallMatcherError,
        SandboxScriptError,
        SandboxStepSpec,
        ScriptedSandboxSession,
        UnconsumedSandboxSteps,
        UnexpectedSandboxCall,
        scripted_sandbox_session,
    )

_LAZY_EXPORTS = {
    "InvalidSandboxStep": ".sandbox",
    "SandboxCall": ".sandbox",
    "SandboxCallMatcherError": ".sandbox",
    "SandboxScriptError": ".sandbox",
    "SandboxStepSpec": ".sandbox",
    "ScriptedSandboxSession": ".sandbox",
    "UnconsumedSandboxSteps": ".sandbox",
    "UnexpectedSandboxCall": ".sandbox",
    "scripted_sandbox_session": ".sandbox",
}


__all__ = [
    "InvalidModelStep",
    "ModelCall",
    "ModelScriptError",
    "ModelStep",
    "ModelStepSpec",
    "InvalidSandboxStep",
    "SandboxCall",
    "SandboxCallMatcherError",
    "SandboxScriptError",
    "SandboxStepSpec",
    "ScriptedSandboxSession",
    "ScriptedModel",
    "UnconsumedModelSteps",
    "UnexpectedModelCall",
    "UnconsumedSandboxSteps",
    "UnexpectedSandboxCall",
    "assistant_message",
    "function_call",
    "scripted_sandbox_session",
]


def __getattr__(name: str) -> Any:
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
