from __future__ import annotations

import pytest

from agents.models import _openai_shared
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from agents.models.openai_responses import OpenAIResponsesModel
from agents.run import set_default_agent_runner
from agents.tracing import set_trace_processors
from agents.tracing.setup import get_trace_provider

from .testing_processor import SPAN_PROCESSOR_TESTING


# This fixture will run once before any tests are executed
@pytest.fixture(scope="session", autouse=True)
def setup_span_processor():
    set_trace_processors([SPAN_PROCESSOR_TESTING])


# Ensure a default OpenAI API key is present for tests that construct clients
# without explicitly configuring a key/client. Tests that need no key use
# monkeypatch.delenv("OPENAI_API_KEY", ...) to remove it locally.
@pytest.fixture(scope="session", autouse=True)
def ensure_openai_api_key():
    import os

    if not os.environ.get("OPENAI_API_KEY"):
        os.environ["OPENAI_API_KEY"] = "test_key"


# This fixture will run before each test
@pytest.fixture(autouse=True)
def clear_span_processor():
    SPAN_PROCESSOR_TESTING.force_flush()
    SPAN_PROCESSOR_TESTING.shutdown()
    SPAN_PROCESSOR_TESTING.clear()


# This fixture will run before each test
@pytest.fixture(autouse=True)
def clear_openai_settings():
    _openai_shared._default_openai_key = None
    _openai_shared._default_openai_client = None
    _openai_shared._use_responses_by_default = True


@pytest.fixture(autouse=True)
def clear_default_runner():
    set_default_agent_runner(None)


# This fixture will run after all tests end
@pytest.fixture(autouse=True, scope="session")
def shutdown_trace_provider():
    yield
    get_trace_provider().shutdown()


@pytest.fixture(autouse=True)
def disable_real_model_clients(monkeypatch, request):
    # If the test is marked to allow the method call, don't override it.
    if request.node.get_closest_marker("allow_call_model_methods"):
        return

    def failing_version(*args, **kwargs):
        pytest.fail("Real models should not be used in tests!")

    monkeypatch.setattr(OpenAIResponsesModel, "get_response", failing_version)
    monkeypatch.setattr(OpenAIResponsesModel, "stream_response", failing_version)
    monkeypatch.setattr(OpenAIChatCompletionsModel, "get_response", failing_version)
    monkeypatch.setattr(OpenAIChatCompletionsModel, "stream_response", failing_version)
