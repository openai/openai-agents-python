"""Mocked HTTP lifecycle coverage for the standalone SIP example."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest


@pytest.fixture
def sip_example() -> ModuleType:
    path = Path(__file__).parents[3] / "examples/codex/native_realtime_sip.py"
    spec = importlib.util.spec_from_file_location("native_realtime_sip_example_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("phase", "hangup_fails"),
    [
        ("normal", False),
        ("attach", False),
        ("history", False),
        ("normal", True),
        ("attach", True),
        ("history", True),
        ("cancel", True),
        ("accept", False),
    ],
)
async def test_example_hangs_up_confirmed_calls_and_preserves_primary_failure(
    sip_example: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    hangup_fails: bool,
) -> None:
    sequence: list[str] = []
    requests: list[tuple[str, dict[str, str], Any]] = []
    primary = asyncio.CancelledError("cancel history") if phase == "cancel" else RuntimeError(phase)

    class Voice:
        async def __aenter__(self) -> Voice:
            sequence.append("attach")
            if phase == "attach":
                raise primary
            return self

        async def __aexit__(self, *args: Any) -> None:
            sequence.append("voice_closed")

        async def events(self) -> Any:
            await asyncio.Event().wait()
            yield None

        async def retrieve_conversation_history(self) -> list[dict[str, Any]]:
            sequence.append("history")
            if phase in ("history", "cancel"):
                raise primary
            return []

    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *args: Any) -> None:
            sequence.append("session_closed")

        async def build_realtime_sip_config(self, **kwargs: Any) -> dict[str, Any]:
            return {"type": "realtime"}

        def realtime(self, **kwargs: Any) -> Voice:
            return Voice()

    class Agent:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def session(self) -> Session:
            return Session()

    class Client:
        async def __aenter__(self) -> Client:
            sequence.append("client_open")
            return self

        async def __aexit__(self, *args: Any) -> None:
            sequence.append("client_closed")

        async def post(
            self, url: str, *, headers: dict[str, str], json: Any = None
        ) -> httpx.Response:
            operation = url.rsplit("/", 1)[-1]
            sequence.append(operation)
            requests.append((url, headers.copy(), json))
            status = (
                500
                if (operation == "accept" and phase == "accept")
                or (operation == "hangup" and hangup_fails)
                else 200
            )
            return httpx.Response(status, request=httpx.Request("POST", url, headers=headers))

    async def no_wait(seconds: float) -> None:
        pass

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-sip-example-key")
    monkeypatch.setattr(sip_example, "Agent", Agent)
    monkeypatch.setattr(sip_example.httpx, "AsyncClient", Client)
    monkeypatch.setattr(sip_example.asyncio, "sleep", no_wait)
    if phase in ("attach", "history", "cancel"):
        with pytest.raises(type(primary)) as caught:
            await sip_example.main("rtc/incoming?", 1, None)
        assert caught.value is primary
        assert caught.value.__context__ is None
    elif phase == "accept" or hangup_fails:
        with pytest.raises(httpx.HTTPStatusError):
            await sip_example.main("rtc/incoming?", 1, None)
    else:
        await sip_example.main("rtc/incoming?", 1, None)
    operations = [url.rsplit("/", 1)[-1] for url, _, _ in requests]
    assert operations == (["accept"] if phase == "accept" else ["accept", "hangup"])
    assert all("/rtc%2Fincoming%3F/" in url for url, _, _ in requests)
    assert all(
        headers == {"Authorization": "Bearer synthetic-sip-example-key"}
        for _, headers, _ in requests
    )
    assert sequence.count("client_open") == sequence.count("client_closed") == 1
    assert sequence[-2:] == ["client_closed", "session_closed"]
    if phase != "accept":
        assert sequence.index("hangup") < sequence.index("client_closed")
    else:
        assert "attach" not in sequence
