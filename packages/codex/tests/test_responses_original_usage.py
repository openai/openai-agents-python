"""Capture original adapter usage only when its native invocation has opted in."""

import json
from types import SimpleNamespace

import pytest

from agents import ModelRequest, ResponsesModel


@pytest.mark.asyncio
async def test_responses_original_usage_has_no_default_serialization() -> None:
    calls = []
    usage = {
        "input_tokens": 3,
        "output_tokens": 2,
        "total_tokens": 5,
        "provider_extension": {"enabled": False, "values": [0, None, "original"]},
    }

    class OriginalUsage:
        def model_dump(self, **kwargs):
            calls.append(kwargs)
            return usage

    async def create(**kwargs):
        return SimpleNamespace(
            usage=OriginalUsage(),
            _request_id="req_original",
            model_dump=lambda: {
                "id": "resp_original",
                "status": "completed",
                "output": [],
                "usage": usage,
            },
        )

    response = await ResponsesModel(
        SimpleNamespace(responses=SimpleNamespace(create=create))
    ).complete(
        ModelRequest(
            body={"model": "synthetic-routing-hint"},
            context=None,
            session_id="session",
            turn_id="turn",
        )
    )
    assert calls == []
    assert "original_usage" not in json.loads(response._capture())
    assert calls == []
    assert json.loads(response._capture(capture_raw_usage=True))["original_usage"] == {
        "response_id": "resp_original",
        "upstream_request_id": "req_original",
        "usage": usage,
    }
    assert calls == [{"mode": "json", "exclude_unset": True}]
