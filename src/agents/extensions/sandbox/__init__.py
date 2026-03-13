try:
    from .sandboxes import (
        E2BSandboxClient as E2BSandboxClient,
        E2BSandboxClientOptions as E2BSandboxClientOptions,
        E2BSandboxSession as E2BSandboxSession,
        E2BSandboxSessionState as E2BSandboxSessionState,
        E2BSandboxTimeouts as E2BSandboxTimeouts,
        E2BSandboxType as E2BSandboxType,
    )

    _HAS_E2B = True
except Exception:  # pragma: no cover
    _HAS_E2B = False

try:
    from .sandboxes import (
        ModalSandboxClient as ModalSandboxClient,
        ModalSandboxClientOptions as ModalSandboxClientOptions,
        ModalSandboxSession as ModalSandboxSession,
        ModalSandboxSessionState as ModalSandboxSessionState,
    )

    _HAS_MODAL = True
except Exception:  # pragma: no cover
    _HAS_MODAL = False

__all__: list[str] = []

if _HAS_E2B:
    __all__.extend(
        [
            "E2BSandboxClient",
            "E2BSandboxClientOptions",
            "E2BSandboxSession",
            "E2BSandboxSessionState",
            "E2BSandboxTimeouts",
            "E2BSandboxType",
        ]
    )

if _HAS_MODAL:
    __all__.extend(
        [
            "ModalSandboxClient",
            "ModalSandboxClientOptions",
            "ModalSandboxSession",
            "ModalSandboxSessionState",
        ]
    )
