"""FIFO sandbox resources for native Session callbacks, without a provider service."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from _agents_shared.testing_sandbox import (
    _HIDDEN_LIFECYCLE_METHODS,
    _SCRIPTABLE_METHODS,
    _UNSUPPORTED_OPTIONAL_METHODS,
    InvalidSandboxStep as InvalidSandboxStep,
    SandboxCall as SandboxCall,
    SandboxCallMatcherError as SandboxCallMatcherError,
    SandboxScriptError as SandboxScriptError,
    SandboxStepSpec as SandboxStepSpec,
    ScriptedSandboxSession as _ScriptedResource,
    UnconsumedSandboxSteps as UnconsumedSandboxSteps,
    UnexpectedSandboxCall as UnexpectedSandboxCall,
    scripted_sandbox_session as _scripted_resource,
)
from agents.classic.sandbox.manifest import Manifest
from agents.classic.sandbox.session.sandbox_session import SandboxSession
from agents.classic.sandbox.types import ExecResult as ExecResult

from ._public import preserve_module_identity as _preserve_module_identity

_SCRIPTED_CAPABILITIES = (
    _SCRIPTABLE_METHODS | _UNSUPPORTED_OPTIONAL_METHODS | _HIDDEN_LIFECYCLE_METHODS
)


class ScriptedSandboxSession(SandboxSession):
    """A borrowed resource with scripted command and file calls.

    Native tool callbacks can call its methods while sandbox_environment owns
    their Environment acquisition. The executor connector remains explicit;
    this resource never runs a host command or restores workspace bytes.
    """

    def __init__(
        self,
        steps: Iterable[SandboxStepSpec | Mapping[str, Any]] = (),
        *,
        manifest: Manifest | None = None,
    ) -> None:
        super().__init__(_scripted_resource(steps, manifest=manifest))

    def __getattribute__(self, name: str) -> Any:
        if name in _SCRIPTED_CAPABILITIES:
            inner = object.__getattribute__(self, "_inner")
            if not hasattr(inner, name):
                raise AttributeError(name)
        return super().__getattribute__(name)

    def __dir__(self) -> list[str]:
        return [
            name
            for name in super().__dir__()
            if name not in _SCRIPTED_CAPABILITIES or hasattr(self, name)
        ]

    @property
    def calls(self) -> tuple[SandboxCall, ...]:
        resource = self._inner
        assert isinstance(resource, _ScriptedResource)
        return resource.calls

    @property
    def remaining_steps(self) -> int:
        resource = self._inner
        assert isinstance(resource, _ScriptedResource)
        return resource.remaining_steps

    def assert_complete(self) -> None:
        resource = self._inner
        assert isinstance(resource, _ScriptedResource)
        resource.assert_complete()


def scripted_sandbox_session(
    steps: Iterable[SandboxStepSpec | Mapping[str, Any]] = (),
    *,
    manifest: Manifest | None = None,
) -> ScriptedSandboxSession:
    """Capture FIFO sandbox expectations for ordinary native tool callbacks.

    Each step supplies method and exactly one of result, responder, or error.
    A match callback receives a detached SandboxCall. The resource exposes only
    configured capabilities, with both PTY methods exposed together. It has
    no workspace persistence or hydration capability; use a real provider for
    those operations. Borrowing it does not grant native executor tools.
    """
    return ScriptedSandboxSession(steps, manifest=manifest)


_preserve_module_identity(globals(), "agents.testing")
