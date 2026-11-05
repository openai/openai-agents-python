from __future__ import annotations

import json
import re
from pathlib import Path

from ...run_config import RunConfig
from ..config import MemoryGenerateConfig
from ..sandbox_agent import SandboxAgent
from ..util.token_truncation import TruncationPolicy, truncate_text
from .interface import RolloutExtractionArtifacts
from .prompts import (
    render_rollout_extraction_prompt,
    render_rollout_extraction_user_prompt,
)

_ROLLOUT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")
_ROLLOUT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PHASE_ONE_ROLLOUT_TOKEN_LIMIT = 150_000
_PHASE_ONE_ROLLOUT_OMISSION_MARKER_TEMPLATE = (
    "\n\n"
    "[rollout content omitted: this phase-one memory prompt contains a truncated view of "
    "the saved rollout. original_chars={original_chars}; rendered_chars={rendered_chars}. "
    "Do not assume the rendered rollout below is complete.]"
    "\n\n"
)


def normalize_rollout_slug(value: str) -> str:
    slug = value.strip()
    if slug.endswith(".md"):
        slug = slug[:-3]
    if not _ROLLOUT_SLUG_RE.fullmatch(slug):
        raise ValueError(f"Invalid rollout_slug: {value!r}")
    return slug


def rollout_id_from_rollout_path(value: str) -> str:
    rollout_id = Path(Path(value).name.strip()).stem
    if not rollout_id or not _ROLLOUT_ID_RE.fullmatch(rollout_id):
        raise ValueError(f"Invalid rollout id for memory: {value!r}")
    return rollout_id


def render_phase_one_prompt(*, rollout_contents: str) -> str:
    payloads = [json.loads(line) for line in rollout_contents.splitlines() if line.strip()]
    if not payloads:
        raise ValueError("rollout_contents must contain at least one JSONL record")
    payload = payloads[-1]
    if len(payloads) == 1:
        terminal_metadata: object = payload.get("terminal_metadata", {})
    else:
        terminal_metadata = {
            "segment_count": len(payloads),
            "final_terminal_metadata": payload.get("terminal_metadata", {}),
            "terminal_states": [
                item.get("terminal_metadata", {}).get("terminal_state", "unknown")
                for item in payloads
                if isinstance(item, dict)
            ],
        }
    terminal_metadata_json = json.dumps(
        terminal_metadata,
        sort_keys=True,
        separators=(",", ":"),
        indent=2,
    )
    # TODO: Replace this fixed cap with 70% of the phase-one model's effective
    # context window once model metadata is available in the SDK.
    truncated_rollout_contents = truncate_text(
        rollout_contents,
        TruncationPolicy.tokens(_PHASE_ONE_ROLLOUT_TOKEN_LIMIT),
    )
    if truncated_rollout_contents != rollout_contents:
        marker = _PHASE_ONE_ROLLOUT_OMISSION_MARKER_TEMPLATE.format(
            original_chars=len(rollout_contents),
            rendered_chars=len(truncated_rollout_contents),
        )
        truncated_rollout_contents = marker + truncated_rollout_contents
    return render_rollout_extraction_user_prompt(
        terminal_metadata_json=terminal_metadata_json,
        rollout_contents=truncated_rollout_contents,
    )


def validate_rollout_artifacts(artifacts: RolloutExtractionArtifacts) -> bool:
    if (
        artifacts.rollout_slug.strip() == ""
        and artifacts.rollout_summary.strip() == ""
        and artifacts.raw_memory.strip() == ""
    ):
        return False
    if (
        not artifacts.rollout_slug.strip()
        or not artifacts.rollout_summary.strip()
        or not artifacts.raw_memory.strip()
    ):
        raise ValueError("Phase 1 returned partially-empty memory artifacts.")
    return True


async def run_phase_one(
    *,
    config: MemoryGenerateConfig,
    prompt: str,
    run_config: RunConfig,
) -> RolloutExtractionArtifacts:
    from ...run import Runner

    if config.phase_one_model_settings is None:
        agent = SandboxAgent(
            name="sandbox-memory-phase-one",
            instructions=render_rollout_extraction_prompt(extra_prompt=config.extra_prompt),
            output_type=RolloutExtractionArtifacts,
            model=config.phase_one_model,
        )
    else:
        agent = SandboxAgent(
            name="sandbox-memory-phase-one",
            instructions=render_rollout_extraction_prompt(extra_prompt=config.extra_prompt),
            output_type=RolloutExtractionArtifacts,
            model=config.phase_one_model,
            model_settings=config.phase_one_model_settings,
        )
    result = await Runner.run(agent, prompt, run_config=run_config)
    return result.final_output_as(RolloutExtractionArtifacts, raise_if_incorrect_type=True)
