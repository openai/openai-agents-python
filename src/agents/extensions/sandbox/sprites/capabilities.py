"""Sprites-specific agent capabilities."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from pydantic import PrivateAttr

from ....run_context import RunContextWrapper
from ....sandbox.capabilities.capability import Capability
from ....sandbox.manifest import Manifest
from ....sandbox.session.base_sandbox_session import BaseSandboxSession
from ....tool import Tool, function_tool

DEFAULT_SPRITES_CONTEXT_PATH = "/.sprite/llm.txt"

UrlVisibility = Literal["public", "sprite"]
"""Sprites URL visibility values. ``"sprite"`` restricts the URL to organization
members (the platform's default); ``"public"`` opens it to the internet."""


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


def _resolve_sprite_handle(session: BaseSandboxSession | None) -> Any | None:
    """Return the underlying ``sprites.Sprite`` from a SpritesSandboxSession, or None.

    Capabilities are bound to the runtime ``SandboxSession`` wrapper, not the
    inner backend session — so we dig through ``_inner`` to reach the
    SpritesSandboxSession's ``_sprite`` attribute.
    """

    if session is None:
        return None
    inner = getattr(session, "_inner", session)
    sprite = getattr(inner, "_sprite", None)
    return sprite


class SpritesUrlAccess(Capability):
    """Expose a tool that lets the agent toggle the sprite's public URL visibility.

    Sprite URL access is a *host-platform* setting, not something the in-VM
    ``sprite-env`` CLI can change — the in-VM API socket only exposes
    services/checkpoints. Without this capability, an agent asked to "make the
    URL public" tends to thrash between unauthenticated commands. This
    capability wraps ``Sprite.update_url_settings`` (which already has the
    application's API token via ``SpritesSandboxClient``) so the model can
    flip visibility in one call.

    Going ``public`` is gated by ``allow_public`` (default ``False``). The
    application must explicitly opt in to expose that option to the agent;
    otherwise the tool only accepts ``"sprite"`` (org-members-only).

    Example:

        agent = SandboxAgent(
            ...,
            capabilities=[
                WorkspaceShellCapability(),
                Filesystem(),
                SpritesPlatformContext(),
                SpritesUrlAccess(allow_public=True),
            ],
        )
    """

    type: Literal["sprites_url_access"] = "sprites_url_access"
    allow_public: bool = False
    """When ``False`` (default), the tool refuses ``visibility="public"``."""

    def tools(self) -> list[Tool]:
        capability = self
        allow_public = self.allow_public
        if allow_public:
            allowed_doc = (
                "Pass 'public' to make the sprite reachable from the open internet, "
                "or 'sprite' to restrict it to organization members."
            )
        else:
            allowed_doc = (
                "Pass 'sprite' to restrict the sprite URL to organization members. "
                "(The 'public' option has been disabled by application policy.)"
            )

        @function_tool(name_override="set_sprite_url_visibility")
        async def set_sprite_url_visibility(
            ctx: RunContextWrapper[Any],
            visibility: UrlVisibility,
        ) -> str:
            """Change the sprite's public URL access mode."""

            _ = ctx
            return await capability._apply_visibility(visibility)

        # Stash a docstring fragment for tools that introspect descriptions.
        setattr(set_sprite_url_visibility, "_allowed_doc", allowed_doc)  # noqa: B010
        return [set_sprite_url_visibility]

    async def _apply_visibility(self, visibility: str) -> str:
        if visibility not in ("public", "sprite"):
            return f"error: visibility must be 'public' or 'sprite', got {visibility!r}"
        if visibility == "public" and not self.allow_public:
            return (
                "error: setting URL to 'public' is disabled by application policy. "
                "Use visibility='sprite' to keep it private to org members."
            )

        sprite = _resolve_sprite_handle(self.session)
        if sprite is None:
            return "error: sprite handle not available (session not started?)"
        try:
            from sprites.types import URLSettings

            await asyncio.to_thread(sprite.update_url_settings, URLSettings(auth=visibility))
        except Exception as exc:  # noqa: BLE001
            return f"error updating URL settings: {exc!r}"
        return f"sprite URL visibility is now {visibility!r}"


class SpritesCheckpoints(Capability):
    """Expose tools to create, list, and (optionally) restore native sprite checkpoints.

    Sprite checkpoints are point-in-time snapshots of the writable filesystem
    overlay. They're a Sprites-specific feature — most other sandbox providers
    don't have anything equivalent at this granularity. This capability lets
    the agent take a checkpoint before risky multi-file work and (when
    explicitly enabled) roll back to it.

    Restore is destructive — it replaces the entire workspace. Gate it
    deliberately with ``allow_restore``. Default ``False``: the agent can save
    checkpoints freely but cannot roll back without application opt-in.

    Example:

        agent = SandboxAgent(
            ...,
            capabilities=[
                ...,
                SpritesCheckpoints(allow_restore=True),
            ],
        )
    """

    type: Literal["sprites_checkpoints"] = "sprites_checkpoints"
    allow_restore: bool = False
    """When ``False`` (default), the restore tool is omitted entirely."""

    def tools(self) -> list[Tool]:
        capability = self

        @function_tool(name_override="create_sprite_checkpoint")
        async def create_sprite_checkpoint(
            ctx: RunContextWrapper[Any],
            comment: str = "",
        ) -> str:
            """Create a sprite filesystem checkpoint and return its id and metadata."""

            _ = ctx
            return await capability._create(comment)

        @function_tool(name_override="list_sprite_checkpoints")
        async def list_sprite_checkpoints(
            ctx: RunContextWrapper[Any],
        ) -> str:
            """List all sprite checkpoints (most recent first)."""

            _ = ctx
            return await capability._list()

        tools_list: list[Tool] = [create_sprite_checkpoint, list_sprite_checkpoints]

        if self.allow_restore:

            @function_tool(name_override="restore_sprite_checkpoint")
            async def restore_sprite_checkpoint(
                ctx: RunContextWrapper[Any],
                checkpoint_id: str,
            ) -> str:
                """Restore the sprite filesystem to a previously-created checkpoint.

                DESTRUCTIVE: replaces the entire workspace with the checkpoint state.
                Any uncommitted changes since the checkpoint are lost.
                """

                _ = ctx
                return await capability._restore(checkpoint_id)

            tools_list.append(restore_sprite_checkpoint)

        return tools_list

    async def _create(self, comment: str) -> str:
        sprite = _resolve_sprite_handle(self.session)
        if sprite is None:
            return "error: sprite handle not available (session not started?)"

        def _do_create() -> dict[str, Any]:
            # ``Sprite.create_checkpoint`` returns an iterator of ``StreamMessage``
            # (no checkpoint id in the stream itself), so consume it and then
            # pull the most-recent saved checkpoint from ``list_checkpoints``.
            stream = sprite.create_checkpoint(comment)
            errors: list[str] = []
            for msg in stream:
                if getattr(msg, "type", "") == "error":
                    err = getattr(msg, "error", None) or getattr(msg, "data", None)
                    if err:
                        errors.append(str(err))
            if errors:
                raise RuntimeError("; ".join(errors))
            existing = sprite.list_checkpoints()
            # ``Current`` is the platform's live-state pointer that always
            # appears at the top of the list; skip it so we report the actual
            # saved snapshot we just made.
            saved = [c for c in existing if str(getattr(c, "id", "")).lower() != "current"]
            if not saved:
                return {}
            saved.sort(key=lambda c: c.create_time, reverse=True)
            latest = saved[0]
            return {
                "id": latest.id,
                "comment": latest.comment or "",
                "created_at": latest.create_time.isoformat(),
            }

        try:
            result = await asyncio.to_thread(_do_create)
        except Exception as exc:  # noqa: BLE001
            return f"error creating checkpoint: {exc!r}"
        if not result:
            return "checkpoint creation completed but no checkpoint was found"
        return (
            f"checkpoint created: id={result['id']!r}, "
            f"comment={result['comment']!r}, created_at={result['created_at']!r}"
        )

    async def _list(self) -> str:
        sprite = _resolve_sprite_handle(self.session)
        if sprite is None:
            return "error: sprite handle not available (session not started?)"
        try:
            checkpoints = await asyncio.to_thread(sprite.list_checkpoints)
        except Exception as exc:  # noqa: BLE001
            return f"error listing checkpoints: {exc!r}"
        if not checkpoints:
            return "no checkpoints"
        rows = [
            f"- {c.id} (created {c.create_time.isoformat()})"
            + (f": {c.comment}" if c.comment else "")
            for c in checkpoints
        ]
        return "\n".join(rows)

    async def _restore(self, checkpoint_id: str) -> str:
        if not self.allow_restore:
            return "error: restore is disabled by application policy"
        sprite = _resolve_sprite_handle(self.session)
        if sprite is None:
            return "error: sprite handle not available (session not started?)"

        def _do_restore() -> list[str]:
            stream = sprite.restore_checkpoint(checkpoint_id)
            errors: list[str] = []
            for msg in stream:
                if getattr(msg, "type", "") == "error":
                    err = getattr(msg, "error", None) or getattr(msg, "data", None)
                    if err:
                        errors.append(str(err))
            return errors

        try:
            errors = await asyncio.to_thread(_do_restore)
        except Exception as exc:  # noqa: BLE001
            return f"error restoring checkpoint: {exc!r}"
        if errors:
            return f"restore completed with errors: {'; '.join(errors)}"
        return f"restored checkpoint {checkpoint_id!r}"


__all__ = [
    "DEFAULT_SPRITES_CONTEXT_PATH",
    "SpritesCheckpoints",
    "SpritesPlatformContext",
    "SpritesUrlAccess",
    "UrlVisibility",
]
