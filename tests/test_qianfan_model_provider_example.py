from __future__ import annotations

import importlib

import pytest

qianfan_provider = importlib.import_module("examples.model_providers.qianfan_provider")


def test_load_settings_uses_qianfan_defaults(monkeypatch) -> None:
    monkeypatch.delenv("QIANFAN_BASE_URL", raising=False)
    monkeypatch.delenv("QIANFAN_API_KEY", raising=False)
    monkeypatch.delenv("QIANFAN_MODEL", raising=False)

    settings = qianfan_provider.load_settings()

    assert settings.base_url == "https://qianfan.baidubce.com/v2"
    assert settings.api_key == "dummy"
    assert settings.model_name == "ernie-5.0"


@pytest.mark.asyncio
async def test_main_skips_without_api_key(monkeypatch) -> None:
    monkeypatch.delenv("QIANFAN_API_KEY", raising=False)
    monkeypatch.delenv("QIANFAN_BASE_URL", raising=False)
    monkeypatch.delenv("QIANFAN_MODEL", raising=False)

    message = await qianfan_provider.main()

    assert message == "Skipping run because no valid QIANFAN_API_KEY was provided."
