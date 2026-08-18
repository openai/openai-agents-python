from __future__ import annotations

from typing import Any, cast

import pytest
from openai import AsyncOpenAI

from agents.exceptions import UserError
from agents.models.openai_provider import OpenAIProvider


@pytest.mark.parametrize(
    "client_option",
    [
        {"organization": "org-test"},
        {"project": "proj-test"},
    ],
)
def test_openai_provider_rejects_ignored_options_with_explicit_client(
    client_option: dict[str, str],
) -> None:
    client = cast(AsyncOpenAI, object())

    with pytest.raises(UserError, match="organization, or project"):
        OpenAIProvider(
            openai_client=client,
            **cast(dict[str, Any], client_option),
        )
