"""Provider-free sandbox scripts shared by the Classic and native testing surfaces."""

from collections.abc import Mapping as Mapping
from typing import Any as Any

from _agents_shared.testing_sandbox import (
    InvalidSandboxStep as InvalidSandboxStep,
    SandboxCall as SandboxCall,
    SandboxCallMatcherError as SandboxCallMatcherError,
    SandboxMatcher as SandboxMatcher,
    SandboxMethod as SandboxMethod,
    SandboxResponder as SandboxResponder,
    SandboxScriptError as SandboxScriptError,
    SandboxStepSpec as SandboxStepSpec,
    ScriptedSandboxSession as ScriptedSandboxSession,
    UnconsumedSandboxSteps as UnconsumedSandboxSteps,
    UnexpectedSandboxCall as UnexpectedSandboxCall,
    scripted_sandbox_session as scripted_sandbox_session,
)

__all__ = [
    "InvalidSandboxStep",
    "SandboxCall",
    "SandboxCallMatcherError",
    "SandboxScriptError",
    "SandboxStepSpec",
    "ScriptedSandboxSession",
    "UnconsumedSandboxSteps",
    "UnexpectedSandboxCall",
    "scripted_sandbox_session",
]
