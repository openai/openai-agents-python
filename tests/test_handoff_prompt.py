from agents.extensions.handoff_prompt import (
    RECOMMENDED_PROMPT_PREFIX,
    prompt_with_handoff_instructions,
)


def test_prompt_with_handoff_instructions_includes_prefix() -> None:
    prompt = "Handle the transfer smoothly."
    result = prompt_with_handoff_instructions(prompt)

    assert result.startswith(RECOMMENDED_PROMPT_PREFIX)
    assert result.endswith(prompt)
