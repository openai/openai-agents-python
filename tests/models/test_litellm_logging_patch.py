from __future__ import annotations

import importlib

import pytest

pytest.importorskip("litellm")


def test_litellm_logging_patch_env_var_controls_application(monkeypatch):
    """Assert the serializer patch only applies when the env var is enabled."""
    litellm_logging = importlib.import_module("litellm.litellm_core_utils.litellm_logging")
    litellm_model = importlib.import_module("agents.extensions.models.litellm_model")

    monkeypatch.delenv("OPENAI_AGENTS_ENABLE_LITELLM_SERIALIZER_PATCH", raising=False)
    litellm_logging = importlib.reload(litellm_logging)
    importlib.reload(litellm_model)

    assert hasattr(
        litellm_logging,
        "_extract_response_obj_and_hidden_params",
    ), "LiteLLM removed _extract_response_obj_and_hidden_params; revisit warning patch."
    assert getattr(litellm_logging, "_openai_agents_patched_serializer_warnings", False) is False

    monkeypatch.setenv("OPENAI_AGENTS_ENABLE_LITELLM_SERIALIZER_PATCH", "true")
    litellm_logging = importlib.reload(litellm_logging)
    importlib.reload(litellm_model)

    assert getattr(litellm_logging, "_openai_agents_patched_serializer_warnings", False) is True
