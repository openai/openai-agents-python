"""Re-export / registration smoke tests for the declaw sandbox extension.

100% of the adapter code lives in the declaw package; this file only
verifies that the thin re-export wiring works and that the parent
``agents.extensions.sandbox`` namespace exposes declaw the same way it
exposes the other backends.
"""

from agents.extensions.sandbox import (
    DeclawSandboxClient,
    DeclawSandboxClientOptions,
    DeclawSandboxSession,
    DeclawSandboxSessionState,
    DeclawSandboxType,
)
from agents.extensions.sandbox.declaw import (
    DeclawSandboxClient as BackendClient,
)
from agents.sandbox.manifest import Manifest
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.session.sandbox_client import (
    BaseSandboxClient,
    BaseSandboxClientOptions,
)
from agents.sandbox.session.sandbox_session_state import SandboxSessionState
from agents.sandbox.snapshot import NoopSnapshot


def test_declaw_backend_id() -> None:
    assert DeclawSandboxClient.backend_id == "declaw"


def test_declaw_client_is_base_subclass() -> None:
    assert issubclass(DeclawSandboxClient, BaseSandboxClient)


def test_declaw_session_is_base_subclass() -> None:
    assert issubclass(DeclawSandboxSession, BaseSandboxSession)


def test_declaw_options_discriminator() -> None:
    opts = DeclawSandboxClientOptions(template="base")
    assert opts.type == "declaw"
    assert isinstance(opts, BaseSandboxClientOptions)


def test_declaw_state_discriminator() -> None:
    state = DeclawSandboxSessionState(
        sandbox_id="sbx-test",
        snapshot=NoopSnapshot(id="snap-test"),
        manifest=Manifest(),
    )
    assert state.type == "declaw"
    assert isinstance(state, SandboxSessionState)


def test_parent_and_backend_module_point_at_same_class() -> None:
    """The parent-level re-export and the backend submodule must
    expose the same object, not two independent copies."""
    assert DeclawSandboxClient is BackendClient


def test_sandbox_type_enum_default() -> None:
    assert DeclawSandboxType.DEFAULT.value == "default"
