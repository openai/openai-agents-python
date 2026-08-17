from __future__ import annotations

from typing import Any, cast

import pytest

from agents import _config
from agents.models import _openai_shared


def test_set_default_openai_api_rejects_invalid_runtime_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(_openai_shared, "set_use_responses_by_default", calls.append)

    with pytest.raises(
        ValueError,
        match="Invalid OpenAI API",
    ):
        _config.set_default_openai_api(cast(Any, "chat_completion"))

    assert calls == []
