from __future__ import annotations

import inspect
import textwrap
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from functools import lru_cache
from importlib.resources import files
from typing import cast

from .._public_agent import get_public_agent, set_public_agent
from ..agent import Agent
from ..exceptions import UserError
from ..items import TResponseInputItem
from ..models.default_models import get_default_model
from ..models.interface import Model
from ..run_context import RunContextWrapper, TContext
from .capabilities import Capability
from .manifest import Manifest
from .manifest_render import render_manifest_description
from .remote_mount_policy import build_remote_mount_policy_instructions
from .sandbox_agent import SandboxAgent
from .session.base_sandbox_session import BaseSandboxSession
from .util.deep_merge import deep_merge


@lru_cache(maxsize=1)
def get_default_sandbox_instructions() -> str | None:
    try:
        return (
            files("agents.sandbox")
            .joinpath("instructions")
            .joinpath("prompt.md")
            .read_text(encoding="utf-8")
            .strip()
        )
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return None


def clone_capabilities(capabilities: Sequence[Capability]) -> list[Capability]:
    return [capability.clone() for capability in capabilities]


def _filesystem_instructions(manifest: Manifest) -> str:
    header = textwrap.dedent(
        """
        # Filesystem
        You have access to a container with a filesystem. The filesystem layout is:
        """
    ).strip()
    tree = render_manifest_description(
        root=manifest.root,
        entries=manifest.validated_entries(),
        coerce_rel_path=manifest._coerce_rel_path,
        depth=3,
    ).strip()
    return f"{header}\n\n{tree}"


def prepare_sandbox_agent(
    *,
    agent: SandboxAgent[TContext],
    session: BaseSandboxSession,
    capabilities: Sequence[Capability],
    run_config_model: str | Model | None = None,
) -> Agent[TContext]:
    manifest = session.state.manifest

    available_capability_types = {capability.type for capability in capabilities}
    for capability in capabilities:
        required_capability_types = capability.required_capability_types()
        missing_capability_types = required_capability_types - available_capability_types
        if missing_capability_types:
            missing = ", ".join(sorted(missing_capability_types))
            raise UserError(f"{type(capability).__name__} requires missing capabilities: {missing}")

    capability_tools = [tool for capability in capabilities for tool in capability.tools()]
    model_settings = agent.model_settings
    extra_args = dict(model_settings.extra_args or {})
    resolved_model_name = resolve_sandbox_model_name(
        agent=agent,
        run_config_model=run_config_model,
    )
    for capability in capabilities:
        capability_sampling_params = dict(extra_args)
        if resolved_model_name is not None:
            capability_sampling_params["model"] = resolved_model_name
        extra_args = deep_merge(extra_args, capability.sampling_params(capability_sampling_params))

    prepared_agent = agent.clone(
        instructions=build_sandbox_instructions(
            base_instructions=agent.base_instructions,
            additional_instructions=agent.instructions,
            capabilities=capabilities,
            manifest=manifest,
        ),
        model_settings=replace(
            model_settings,
            extra_args=extra_args if extra_args else None,
        ),
        tools=[*agent.tools, *capability_tools],
        capabilities=capabilities,
    )
    set_public_agent(prepared_agent, agent)
    return prepared_agent


def resolve_sandbox_model_name(
    *,
    agent: SandboxAgent[TContext],
    run_config_model: str | Model | None = None,
) -> str | None:
    if run_config_model is not None:
        return _model_name_from_model(run_config_model)
    if agent.model is None:
        return get_default_model()
    return _model_name_from_model(agent.model)


def _model_name_from_model(model: str | Model) -> str | None:
    if isinstance(model, str):
        return model

    model_name = getattr(model, "model", None)
    if isinstance(model_name, str):
        return model_name
    return None


def prepare_sandbox_input(
    capabilities: Sequence[Capability],
    current_input: str | list[TResponseInputItem],
) -> str | list[TResponseInputItem]:
    if isinstance(current_input, str):
        return current_input

    processed_input = current_input
    for capability in capabilities:
        processed_input = capability.process_context(processed_input)
    return processed_input


def build_sandbox_instructions(
    *,
    base_instructions: str
    | Callable[[RunContextWrapper[TContext], Agent[TContext]], Awaitable[str | None] | str | None]
    | None,
    additional_instructions: str
    | Callable[[RunContextWrapper[TContext], Agent[TContext]], Awaitable[str | None] | str | None]
    | None,
    capabilities: Sequence[Capability],
    manifest: Manifest,
) -> Callable[[RunContextWrapper[TContext], Agent[TContext]], Awaitable[str | None]]:
    async def _instructions(
        run_context: RunContextWrapper[TContext],
        current_agent: Agent[TContext],
    ) -> str | None:
        parts: list[str] = []
        public_agent = cast(Agent[TContext], get_public_agent(current_agent))
        base: str | None

        if base_instructions is None:
            base = get_default_sandbox_instructions()
        else:
            base = await resolve_instructions(
                instructions=base_instructions,
                run_context=run_context,
                agent=public_agent,
            )
        if base:
            parts.append(base)

        if additional_instructions is not None:
            additional = await resolve_instructions(
                instructions=additional_instructions,
                run_context=run_context,
                agent=public_agent,
            )
            if additional:
                parts.append(additional)

        for capability in capabilities:
            fragment = await capability.instructions(manifest)
            if fragment:
                parts.append(fragment)

        if remote_mount_policy := build_remote_mount_policy_instructions(manifest):
            parts.append(remote_mount_policy)

        parts.append(_filesystem_instructions(manifest))

        return "\n\n".join(parts) if parts else None

    return _instructions


async def resolve_instructions(
    *,
    instructions: str
    | Callable[[RunContextWrapper[TContext], Agent[TContext]], Awaitable[str | None] | str | None]
    | None,
    run_context: RunContextWrapper[TContext],
    agent: Agent[TContext],
) -> str | None:
    if isinstance(instructions, str):
        return instructions
    if callable(instructions):
        result = instructions(run_context, agent)
        if inspect.isawaitable(result):
            return await result
        return result
    return None
