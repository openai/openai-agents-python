from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.extras


def test_requested_optional_extra_imports_from_its_standalone_environment() -> None:
    optional_extra = os.environ["OPENAI_AGENTS_INTEGRATION_EXTRA"]
    module_names = {
        "any-llm": "agents.extensions.models.any_llm_model",
        "encrypt": "agents.extensions.memory.encrypt_session",
        "litellm": "agents.extensions.models.litellm_model",
        "realtime": "agents.realtime",
        "redis": "agents.extensions.memory.redis_session",
        "s3": "boto3",
        "sqlalchemy": "agents.extensions.memory.sqlalchemy_session",
        "viz": "agents.extensions.visualization",
        "voice": "agents.voice",
    }
    module = importlib.import_module(module_names[optional_extra])

    assert module.__file__ is not None
    assert "site-packages" in Path(module.__file__).parts


@pytest.mark.parametrize(
    ("optional_extra", "package_symbol", "module_name", "module_symbol"),
    [
        (
            "encrypt",
            "EncryptedSession",
            "agents.extensions.memory.encrypt_session",
            "EncryptedSession",
        ),
        ("redis", "RedisSession", "agents.extensions.memory.redis_session", "RedisSession"),
        (
            "sqlalchemy",
            "SQLAlchemySession",
            "agents.extensions.memory.sqlalchemy_session",
            "SQLAlchemySession",
        ),
    ],
)
def test_memory_extra_lazy_exports_resolve_to_the_installed_backend(
    optional_extra: str,
    package_symbol: str,
    module_name: str,
    module_symbol: str,
) -> None:
    if os.environ["OPENAI_AGENTS_INTEGRATION_EXTRA"] != optional_extra:
        pytest.skip(f"This environment does not include the {optional_extra} extra.")

    memory = importlib.import_module("agents.extensions.memory")
    module = importlib.import_module(module_name)

    assert getattr(memory, package_symbol) is getattr(module, module_symbol)
