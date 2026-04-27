"""Sprites-specific agent capabilities."""

from __future__ import annotations

from typing import Literal

from pydantic import PrivateAttr

from ....sandbox.capabilities.capability import Capability
from ....sandbox.manifest import Manifest
from ....sandbox.session.base_sandbox_session import BaseSandboxSession

DEFAULT_SPRITES_CONTEXT_PATH = "/.sprite/llm.txt"


class SpritesPlatformContext(Capability):
    """Inject the sprite's ``/.sprite/llm.txt`` platform-context file into the agent's instructions.

    Sprites bundle an LLM-facing document at ``/.sprite/llm.txt`` describing
    available CLI commands (``sprite-env services``, ``sprite-env checkpoints``),
    platform behavior (URL routing, idle pause, network policy), and security
    rules (e.g. "HTTP services may become PUBLIC — never expose secrets").

    Adding this capability to a ``SandboxAgent`` reads the file once per
    session and appends its contents to the system prompt so the model can
    use the platform's own primitives correctly without the application
    embedding sprite-specific guidance into its instructions.

    Example:

        agent = SandboxAgent(
            ...,
            capabilities=[
                WorkspaceShellCapability(),
                Filesystem(),
                SpritesPlatformContext(),
            ],
        )

    The file is read via ``session.exec("cat", path)``, which bypasses the
    workspace path-validation that would otherwise reject paths outside the
    manifest root.
    """

    type: Literal["sprites_platform_context"] = "sprites_platform_context"
    path: str = DEFAULT_SPRITES_CONTEXT_PATH
    """Sprite-side path of the context file. Defaults to ``/.sprite/llm.txt``."""

    timeout_s: float = 5.0
    """Timeout for the ``cat`` exec call."""

    _cached_text: str | None = PrivateAttr(default=None)

    def bind(self, session: BaseSandboxSession) -> None:
        super().bind(session)
        # Reset the cache on rebind so a different session re-reads its own file.
        self._cached_text = None

    async def instructions(self, manifest: Manifest) -> str | None:
        _ = manifest
        if self._cached_text is not None:
            return self._cached_text
        if self.session is None:
            return None

        try:
            result = await self.session.exec(
                "cat", "--", self.path, shell=False, timeout=self.timeout_s
            )
        except Exception:
            return None
        if not result.ok():
            return None

        text = result.stdout.decode("utf-8", errors="replace").strip()
        if not text:
            return None

        framed = (
            "The following is platform context for the Sprites sandbox you are running "
            "in. It describes available CLI commands (e.g. `sprite-env services`, "
            "`sprite-env checkpoints`), platform behavior, and security rules. Treat "
            "it as authoritative when choosing how to interact with the sandbox.\n\n"
            "<sprites-platform-context>\n"
            f"{text}\n"
            "</sprites-platform-context>"
        )
        self._cached_text = framed
        return framed


__all__ = [
    "DEFAULT_SPRITES_CONTEXT_PATH",
    "SpritesPlatformContext",
]
