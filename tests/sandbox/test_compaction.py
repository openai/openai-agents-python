import pytest

from agents.sandbox.capabilities import CompactionModelInfo


@pytest.mark.parametrize(
    ("model", "context_window"),
    [
        ("gpt-5.4", 1_047_576),
        ("gpt-5.4-pro", 1_047_576),
        ("gpt-5.5", 1_047_576),
        ("gpt-5.3-codex", 400_000),
        ("gpt-5.4-mini", 400_000),
        ("gpt-4.1", 1_047_576),
        ("o3", 200_000),
        ("gpt-4o", 128_000),
        ("openai/gpt-5.4", 1_047_576),
        ("openai/gpt-5.5", 1_047_576),
        ("gpt-5-2", 400_000),
        ("gpt-5-4", 1_047_576),
        ("gpt-5-5", 1_047_576),
        ("openai/gpt-5-4-mini", 400_000),
        ("gpt-4-1-mini", 1_047_576),
    ],
)
def test_compaction_model_info_for_model_returns_context_window(
    model: str,
    context_window: int,
) -> None:
    assert CompactionModelInfo.for_model(model).context_window == context_window


def test_compaction_model_info_for_model_rejects_unknown_model() -> None:
    with pytest.raises(ValueError, match="Unknown context window for model"):
        CompactionModelInfo.for_model("not-a-model")


def test_compaction_model_info_maybe_for_model_returns_none_for_unknown_model() -> None:
    assert CompactionModelInfo.maybe_for_model("not-a-model") is None
