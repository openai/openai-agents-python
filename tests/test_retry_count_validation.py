from __future__ import annotations

import pytest
from pydantic import ValidationError

from agents.retry import ModelRetrySettings


def test_model_retry_settings_rejects_negative_max_retries() -> None:
    with pytest.raises(ValidationError):
        ModelRetrySettings(max_retries=-1)


def test_model_retry_settings_allows_zero_max_retries() -> None:
    settings = ModelRetrySettings(max_retries=0)
    assert settings.max_retries == 0
