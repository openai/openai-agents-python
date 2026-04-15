from __future__ import annotations

from ...run_config import RunConfig
from ..config import MemoryGenerateConfig
from ..sandbox_agent import SandboxAgent
from .prompts import render_memory_consolidation_prompt
from .storage import PhaseTwoInputSelection


async def run_phase_two(
    *,
    config: MemoryGenerateConfig,
    memory_root: str,
    selection: PhaseTwoInputSelection,
    run_config: RunConfig,
) -> None:
    from ...run import Runner

    if config.phase_two_model_settings is None:
        agent = SandboxAgent(
            name="sandbox-memory-phase-two",
            instructions=None,
            model=config.phase_two_model,
        )
    else:
        agent = SandboxAgent(
            name="sandbox-memory-phase-two",
            instructions=None,
            model=config.phase_two_model,
            model_settings=config.phase_two_model_settings,
        )
    prompt = render_memory_consolidation_prompt(
        memory_root=memory_root,
        selection=selection,
        extra_prompt=config.extra_prompt,
    )
    await Runner.run(agent, prompt, run_config=run_config)
