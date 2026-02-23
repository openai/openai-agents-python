import asyncio
import gc
import os
import weakref

import openai
import pytest

from agents import (
    set_default_openai_api,
    set_default_openai_client,
    set_default_openai_key,
    set_default_openai_responses_transport,
)
from agents.models import _openai_shared
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from agents.models.openai_provider import OpenAIProvider
from agents.models.openai_responses import OpenAIResponsesModel, OpenAIResponsesWSModel


def test_cc_no_default_key_errors(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(openai.OpenAIError):
        OpenAIProvider(use_responses=False).get_model("gpt-4")


def test_cc_set_default_openai_key():
    set_default_openai_key("test_key")
    chat_model = OpenAIProvider(use_responses=False).get_model("gpt-4")
    assert chat_model._client.api_key == "test_key"  # type: ignore


def test_cc_set_default_openai_client():
    client = openai.AsyncOpenAI(api_key="test_key")
    set_default_openai_client(client)
    chat_model = OpenAIProvider(use_responses=False).get_model("gpt-4")
    assert chat_model._client.api_key == "test_key"  # type: ignore


def test_resp_no_default_key_errors(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert os.getenv("OPENAI_API_KEY") is None
    with pytest.raises(openai.OpenAIError):
        OpenAIProvider(use_responses=True).get_model("gpt-4")


def test_resp_set_default_openai_key():
    set_default_openai_key("test_key")
    resp_model = OpenAIProvider(use_responses=True).get_model("gpt-4")
    assert resp_model._client.api_key == "test_key"  # type: ignore


def test_resp_set_default_openai_client():
    client = openai.AsyncOpenAI(api_key="test_key")
    set_default_openai_client(client)
    resp_model = OpenAIProvider(use_responses=True).get_model("gpt-4")
    assert resp_model._client.api_key == "test_key"  # type: ignore


def test_set_default_openai_api():
    assert isinstance(OpenAIProvider().get_model("gpt-4"), OpenAIResponsesModel), (
        "Default should be responses"
    )

    set_default_openai_api("chat_completions")
    assert isinstance(OpenAIProvider().get_model("gpt-4"), OpenAIChatCompletionsModel), (
        "Should be chat completions model"
    )

    set_default_openai_api("responses")
    assert isinstance(OpenAIProvider().get_model("gpt-4"), OpenAIResponsesModel), (
        "Should be responses model"
    )


def test_set_default_openai_responses_transport():
    set_default_openai_api("responses")

    assert isinstance(OpenAIProvider().get_model("gpt-4"), OpenAIResponsesModel), (
        "Default responses transport should be HTTP"
    )

    set_default_openai_responses_transport("websocket")
    assert isinstance(OpenAIProvider().get_model("gpt-4"), OpenAIResponsesWSModel), (
        "Should be websocket responses model"
    )

    set_default_openai_responses_transport("http")
    assert isinstance(OpenAIProvider().get_model("gpt-4"), OpenAIResponsesModel), (
        "Should switch back to HTTP responses model"
    )


def test_set_default_openai_responses_transport_rejects_invalid_value():
    with pytest.raises(ValueError, match="Expected one of: 'http', 'websocket'"):
        set_default_openai_responses_transport("ws")  # type: ignore[arg-type]


def test_openai_provider_transport_override_beats_default():
    set_default_openai_api("responses")
    set_default_openai_responses_transport("websocket")

    assert isinstance(
        OpenAIProvider(use_responses=True, use_responses_websocket=False).get_model("gpt-4"),
        OpenAIResponsesModel,
    )
    assert isinstance(
        OpenAIProvider(use_responses=True, use_responses_websocket=True).get_model("gpt-4"),
        OpenAIResponsesWSModel,
    )


def test_legacy_websocket_default_flag_syncs_transport_getter():
    _openai_shared._use_responses_websocket_by_default = True
    assert _openai_shared.get_default_openai_responses_transport() == "websocket"

    _openai_shared._use_responses_websocket_by_default = False
    assert _openai_shared.get_default_openai_responses_transport() == "http"


def test_openai_provider_uses_base_urls_from_env(monkeypatch):
    captured_kwargs: dict[str, object] = {}

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)
            self.api_key = kwargs.get("api_key")
            self.base_url = kwargs.get("base_url")
            self.websocket_base_url = kwargs.get("websocket_base_url")

    monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy.example.test/v1")
    monkeypatch.setenv("OPENAI_WEBSOCKET_BASE_URL", "wss://proxy.example.test/v1")
    monkeypatch.setattr("agents.models.openai_provider.AsyncOpenAI", FakeAsyncOpenAI)

    model = OpenAIProvider(use_responses=True).get_model("gpt-4")
    assert isinstance(model, OpenAIResponsesModel)
    assert captured_kwargs["base_url"] == "https://proxy.example.test/v1"
    assert captured_kwargs["websocket_base_url"] == "wss://proxy.example.test/v1"


def test_openai_provider_websocket_base_url_arg_overrides_env(monkeypatch):
    captured_kwargs: dict[str, object] = {}

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)
            self.api_key = kwargs.get("api_key")
            self.base_url = kwargs.get("base_url")
            self.websocket_base_url = kwargs.get("websocket_base_url")

    monkeypatch.setenv("OPENAI_WEBSOCKET_BASE_URL", "wss://env.example.test/v1")
    monkeypatch.setattr("agents.models.openai_provider.AsyncOpenAI", FakeAsyncOpenAI)

    model = OpenAIProvider(
        use_responses=True,
        websocket_base_url="wss://explicit.example.test/v1",
    ).get_model("gpt-4")
    assert isinstance(model, OpenAIResponsesModel)
    assert captured_kwargs["websocket_base_url"] == "wss://explicit.example.test/v1"


@pytest.mark.asyncio
async def test_openai_provider_reuses_websocket_model_instance_for_same_model_name():
    provider = OpenAIProvider(use_responses=True, use_responses_websocket=True)

    model1 = provider.get_model("gpt-4")
    model2 = provider.get_model("gpt-4")

    assert isinstance(model1, OpenAIResponsesWSModel)
    assert model1 is model2


def test_openai_provider_does_not_reuse_non_websocket_model_instances():
    provider = OpenAIProvider(use_responses=True, use_responses_websocket=False)

    model1 = provider.get_model("gpt-4")
    model2 = provider.get_model("gpt-4")

    assert isinstance(model1, OpenAIResponsesModel)
    assert isinstance(model2, OpenAIResponsesModel)
    assert model1 is not model2


def test_openai_provider_does_not_reuse_websocket_model_without_running_loop():
    class DummyAsyncOpenAI:
        pass

    provider = OpenAIProvider(
        use_responses=True,
        use_responses_websocket=True,
        openai_client=DummyAsyncOpenAI(),  # type: ignore[arg-type]
    )

    model1 = provider.get_model("gpt-4")
    model2 = provider.get_model("gpt-4")

    assert isinstance(model1, OpenAIResponsesWSModel)
    assert isinstance(model2, OpenAIResponsesWSModel)
    assert model1 is not model2


def test_openai_provider_scopes_websocket_model_cache_to_running_loop():
    class DummyAsyncOpenAI:
        pass

    provider = OpenAIProvider(
        use_responses=True,
        use_responses_websocket=True,
        openai_client=DummyAsyncOpenAI(),  # type: ignore[arg-type]
    )

    async def get_model():
        return provider.get_model("gpt-4")

    loop1 = asyncio.new_event_loop()
    loop2 = asyncio.new_event_loop()
    try:
        model1 = loop1.run_until_complete(get_model())
        model1_again = loop1.run_until_complete(get_model())
        model2 = loop2.run_until_complete(get_model())
    finally:
        loop1.close()
        loop2.close()
        asyncio.set_event_loop(None)

    assert isinstance(model1, OpenAIResponsesWSModel)
    assert model1 is model1_again
    assert model2 is not model1


def test_openai_provider_websocket_loop_cache_does_not_keep_closed_loop_alive(monkeypatch):
    class DummyAsyncOpenAI:
        pass

    class DummyWSConnection:
        async def close(self) -> None:
            return None

    provider = OpenAIProvider(
        use_responses=True,
        use_responses_websocket=True,
        openai_client=DummyAsyncOpenAI(),  # type: ignore[arg-type]
    )

    async def create_and_warm_model() -> OpenAIResponsesWSModel:
        model = provider.get_model("gpt-4")
        assert isinstance(model, OpenAIResponsesWSModel)

        async def fake_open(
            ws_url: str, headers: dict[str, str], *, connect_timeout: float | None = None
        ) -> DummyWSConnection:
            return DummyWSConnection()

        monkeypatch.setattr(model, "_open_websocket_connection", fake_open)
        model._get_ws_request_lock()
        await model._ensure_websocket_connection(
            "wss://example.test/v1/responses",
            {},
            connect_timeout=None,
        )
        return model

    loop = asyncio.new_event_loop()
    try:
        model = loop.run_until_complete(create_and_warm_model())
        loop_ref = weakref.ref(loop)
    finally:
        loop.close()
        asyncio.set_event_loop(None)

    del loop
    gc.collect()

    assert loop_ref() is None
    assert list(provider._ws_model_cache_by_loop.items()) == []
    # Keep a live reference to the model to ensure cache cleanup doesn't depend on model GC.
    assert isinstance(model, OpenAIResponsesWSModel)


def test_openai_provider_prunes_closed_loop_cache_with_live_ws_connection(monkeypatch):
    class DummyAsyncOpenAI:
        pass

    abort_calls: list[str] = []

    class DummyTransport:
        def abort(self) -> None:
            abort_calls.append("abort")

    class PinningWSConnection:
        def __init__(self, loop: asyncio.AbstractEventLoop):
            self.loop = loop
            self.transport = DummyTransport()

        async def close(self) -> None:
            raise AssertionError("Closed-loop cache pruning should not await websocket.close().")

    provider = OpenAIProvider(
        use_responses=True,
        use_responses_websocket=True,
        openai_client=DummyAsyncOpenAI(),  # type: ignore[arg-type]
    )

    async def create_and_warm_model() -> None:
        model = provider.get_model("gpt-4")
        assert isinstance(model, OpenAIResponsesWSModel)

        async def fake_open(
            ws_url: str, headers: dict[str, str], *, connect_timeout: float | None = None
        ) -> PinningWSConnection:
            return PinningWSConnection(asyncio.get_running_loop())

        monkeypatch.setattr(model, "_open_websocket_connection", fake_open)
        await model._ensure_websocket_connection(
            "wss://example.test/v1/responses",
            {},
            connect_timeout=None,
        )

    async def get_model_on_current_loop() -> OpenAIResponsesWSModel:
        model = provider.get_model("gpt-4")
        assert isinstance(model, OpenAIResponsesWSModel)
        return model

    loop1 = asyncio.new_event_loop()
    try:
        loop1.run_until_complete(create_and_warm_model())
        loop1_ref = weakref.ref(loop1)
    finally:
        loop1.close()
        asyncio.set_event_loop(None)

    del loop1
    gc.collect()

    # The cached websocket model's live connection pins the closed loop until provider cleanup runs.
    assert loop1_ref() is not None

    loop2 = asyncio.new_event_loop()
    try:
        loop2.run_until_complete(get_model_on_current_loop())
    finally:
        loop2.close()
        asyncio.set_event_loop(None)

    del loop2
    gc.collect()

    assert abort_calls == ["abort"]
    assert loop1_ref() is None
    assert all(not loop.is_closed() for loop in provider._ws_model_cache_by_loop)


def test_openai_provider_aclose_closes_websocket_models_from_other_loops(monkeypatch):
    class DummyAsyncOpenAI:
        pass

    provider = OpenAIProvider(
        use_responses=True,
        use_responses_websocket=True,
        openai_client=DummyAsyncOpenAI(),  # type: ignore[arg-type]
    )

    async def get_model():
        return provider.get_model("gpt-4")

    closed_models: list[object] = []

    async def fake_close(self):
        closed_models.append(self)

    monkeypatch.setattr(OpenAIResponsesWSModel, "close", fake_close)
    monkeypatch.setattr(
        "agents.models.openai_provider.asyncio.to_thread",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("provider.aclose() should not drive foreign loops in to_thread")
        ),
    )

    loop1 = asyncio.new_event_loop()
    loop2 = asyncio.new_event_loop()
    try:
        model1 = loop1.run_until_complete(get_model())
        model2 = loop2.run_until_complete(get_model())

        asyncio.run(provider.aclose())

        model1_new = loop1.run_until_complete(get_model())
        model2_again = loop2.run_until_complete(get_model())
    finally:
        loop1.close()
        loop2.close()
        asyncio.set_event_loop(None)

    assert closed_models == [model1, model2] or closed_models == [model2, model1]
    assert model1_new is not model1
    assert model2_again is not model2


def test_openai_provider_aclose_closes_websocket_models_when_original_loop_is_closed(monkeypatch):
    class DummyAsyncOpenAI:
        pass

    provider = OpenAIProvider(
        use_responses=True,
        use_responses_websocket=True,
        openai_client=DummyAsyncOpenAI(),  # type: ignore[arg-type]
    )

    async def get_model():
        return provider.get_model("gpt-4")

    loop = asyncio.new_event_loop()
    try:
        model = loop.run_until_complete(get_model())
    finally:
        loop.close()
        asyncio.set_event_loop(None)

    closed_models: list[object] = []

    async def fake_close(self):
        closed_models.append(self)

    monkeypatch.setattr(OpenAIResponsesWSModel, "close", fake_close)

    asyncio.run(provider.aclose())

    assert closed_models == [model]


@pytest.mark.asyncio
async def test_openai_provider_aclose_closes_cached_models(monkeypatch):
    provider = OpenAIProvider(use_responses=True, use_responses_websocket=True)
    model1 = provider.get_model("gpt-4")

    closed_models: list[object] = []

    async def fake_close(self):
        closed_models.append(self)

    monkeypatch.setattr(OpenAIResponsesWSModel, "close", fake_close)

    await provider.aclose()
    assert closed_models == [model1]
    assert provider.get_model("gpt-4") is not model1
