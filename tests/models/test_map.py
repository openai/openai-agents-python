from agents import Agent, MultiProvider, OpenAIResponsesModel, OpenAIResponsesWSModel, RunConfig
from agents.extensions.models.litellm_model import LitellmModel
from agents.run_internal.run_loop import get_model


def test_no_prefix_is_openai():
    agent = Agent(model="gpt-4o", instructions="", name="test")
    model = get_model(agent, RunConfig())
    assert isinstance(model, OpenAIResponsesModel)


def test_openai_prefix_is_openai():
    agent = Agent(model="openai/gpt-4o", instructions="", name="test")
    model = get_model(agent, RunConfig())
    assert isinstance(model, OpenAIResponsesModel)


def test_litellm_prefix_is_litellm():
    agent = Agent(model="litellm/foo/bar", instructions="", name="test")
    model = get_model(agent, RunConfig())
    assert isinstance(model, LitellmModel)


def test_no_prefix_can_use_openai_responses_websocket():
    agent = Agent(model="gpt-4o", instructions="", name="test")
    model = get_model(
        agent,
        RunConfig(model_provider=MultiProvider(openai_use_responses_websocket=True)),
    )
    assert isinstance(model, OpenAIResponsesWSModel)


def test_openai_prefix_can_use_openai_responses_websocket():
    agent = Agent(model="openai/gpt-4o", instructions="", name="test")
    model = get_model(
        agent,
        RunConfig(model_provider=MultiProvider(openai_use_responses_websocket=True)),
    )
    assert isinstance(model, OpenAIResponsesWSModel)


def test_multi_provider_passes_websocket_base_url_to_openai_provider(monkeypatch):
    captured_kwargs = {}

    class FakeOpenAIProvider:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)

        def get_model(self, model_name):
            raise AssertionError("This test only verifies constructor passthrough.")

    monkeypatch.setattr("agents.models.multi_provider.OpenAIProvider", FakeOpenAIProvider)

    MultiProvider(openai_websocket_base_url="wss://proxy.example.test/v1")
    assert captured_kwargs["websocket_base_url"] == "wss://proxy.example.test/v1"
