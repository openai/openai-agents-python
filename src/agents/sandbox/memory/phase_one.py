from __future__ import annotations

import json
import re
from pathlib import Path

from ...agent import Agent
from ...agent_output import AgentOutputSchema
from ...run_config import RunConfig
from ..capabilities.compaction import CompactionModelInfo
from ..config import MemoryGenerateConfig
from ..util.token_truncation import (
    TruncationPolicy,
    approx_bytes_for_tokens,
    openai_token_count,
    truncate_text,
)
from .interface import RolloutExtractionArtifacts
from .prompts import (
    render_rollout_extraction_prompt,
    render_rollout_extraction_user_prompt,
)

_ROLLOUT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")
_ROLLOUT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PHASE_ONE_ROLLOUT_TOKEN_LIMIT = 150_000
_PHASE_ONE_INPUT_CONTEXT_RATIO = 0.7
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


def render_phase_one_prompt(
    *,
    rollout_contents: str,
    input_token_limit: int | None = None,
    input_overhead_tokens: int = 0,
    model: str | None = None,
) -> str:
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

    def render_with_rollout_policy(policy: TruncationPolicy) -> str:
        truncated_rollout_contents = truncate_text(rollout_contents, policy)
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

    if input_token_limit is None:
        return render_with_rollout_policy(TruncationPolicy.tokens(_PHASE_ONE_ROLLOUT_TOKEN_LIMIT))
    if input_token_limit <= 0:
        raise ValueError("input_token_limit must be greater than 0")
    if input_overhead_tokens < 0:
        raise ValueError("input_overhead_tokens must be greater than or equal to 0")

    # Keep the whole estimated phase-one request within the caller's token budget using token
    # accounting that never undercounts (see ``openai_token_count``: the model's own tokenizer
    # when ``tiktoken`` is available, otherwise a conservative byte-length upper bound). An
    # average bytes-per-token heuristic would let token-dense JSON or code slip past the budget
    # and overflow the model context. Rollout content is truncated by bytes, and each iteration
    # re-measures the rendered prompt so the marker and wrapper overhead are counted too. The
    # released 150,000-token rollout ceiling stays as an upper bound on rollout content.
    rollout_byte_limit = min(
        approx_bytes_for_tokens(_PHASE_ONE_ROLLOUT_TOKEN_LIMIT),
        max(0, approx_bytes_for_tokens(input_token_limit - input_overhead_tokens)),
    )
    while True:
        prompt = render_with_rollout_policy(TruncationPolicy.bytes(rollout_byte_limit))
        prompt_tokens = openai_token_count(prompt, model=model)
        estimated_input_tokens = input_overhead_tokens + prompt_tokens
        if estimated_input_tokens <= input_token_limit:
            return prompt
        if rollout_byte_limit == 0:
            raise ValueError(
                "The phase-one model context window is too small for the fixed phase-one "
                "prompt overhead."
            )
        # Shrink the rollout byte budget by the token overflow scaled to the rendered prompt's
        # measured byte-per-token density. This converges in a few iterations without
        # discarding the whole rollout for token-dense content or crawling for compressible
        # content, both of which a fixed bytes-per-token guess would cause.
        overflow_tokens = estimated_input_tokens - input_token_limit
        bytes_per_token = max(1, len(prompt.encode("utf-8")) // max(1, prompt_tokens))
        rollout_byte_limit = max(0, rollout_byte_limit - overflow_tokens * bytes_per_token)


def render_phase_one_prompt_for_config(
    *,
    config: MemoryGenerateConfig,
    rollout_contents: str,
) -> str:
    input_token_limit = _resolve_phase_one_input_token_limit(config)
    if input_token_limit is None:
        return render_phase_one_prompt(rollout_contents=rollout_contents)

    model = config.phase_one_model if isinstance(config.phase_one_model, str) else None
    instructions = render_rollout_extraction_prompt(extra_prompt=config.extra_prompt)
    output_schema = AgentOutputSchema(RolloutExtractionArtifacts)
    schema_json = json.dumps(
        output_schema.json_schema(),
        sort_keys=True,
        separators=(",", ":"),
    )
    input_overhead_tokens = openai_token_count(instructions, model=model) + openai_token_count(
        schema_json, model=model
    )
    return render_phase_one_prompt(
        rollout_contents=rollout_contents,
        input_token_limit=input_token_limit,
        input_overhead_tokens=input_overhead_tokens,
        model=model,
    )


def _resolve_phase_one_input_token_limit(config: MemoryGenerateConfig) -> int | None:
    context_window = config.phase_one_model_context_window_tokens
    if context_window is None and isinstance(config.phase_one_model, str):
        model_info = CompactionModelInfo.maybe_for_model(config.phase_one_model)
        if model_info is not None:
            context_window = model_info.context_window
    if context_window is None:
        return None
    return int(context_window * _PHASE_ONE_INPUT_CONTEXT_RATIO)


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
        agent = Agent(
            name="sandbox-memory-phase-one",
            instructions=render_rollout_extraction_prompt(extra_prompt=config.extra_prompt),
            output_type=RolloutExtractionArtifacts,
            model=config.phase_one_model,
        )
    else:
        agent = Agent(
            name="sandbox-memory-phase-one",
            instructions=render_rollout_extraction_prompt(extra_prompt=config.extra_prompt),
            output_type=RolloutExtractionArtifacts,
            model=config.phase_one_model,
            model_settings=config.phase_one_model_settings,
        )
    result = await Runner.run(agent, prompt, run_config=run_config)
    return result.final_output_as(RolloutExtractionArtifacts, raise_if_incorrect_type=True)
