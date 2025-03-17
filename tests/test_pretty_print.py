import json

import pytest
from inline_snapshot import snapshot
from pydantic import BaseModel

from agents import Agent, Runner
from agents.agent_output import _WRAPPER_DICT_KEY
from agents.util._pretty_print import pretty_print_result, pretty_print_run_result_streaming
from tests.fake_model import FakeModel

from .test_responses import get_final_output_message, get_text_message


@pytest.mark.asyncio
async def test_pretty_result():
    model = FakeModel()
    model.set_next_output([get_text_message("Hi there")])

    agent = Agent(name="test_agent", model=model)
    result = await Runner.run(agent, input="Hello")

    assert pretty_print_result(result) == snapshot("""\
RunResult:
- Last agent: Agent(name="test_agent", ...)
- Final output (str):
    Hi there
- 1 new item(s)
- 1 raw response(s)
- 0 input guardrail result(s)
- 0 output guardrail result(s)
(See `RunResult` for more details)\
""")


@pytest.mark.asyncio
async def test_pretty_run_result_streaming():
    model = FakeModel()
    model.set_next_output([get_text_message("Hi there")])

    agent = Agent(name="test_agent", model=model)
    result = Runner.run_streamed(agent, input="Hello")
    async for _ in result.stream_events():
        pass

    assert pretty_print_run_result_streaming(result) == snapshot("""\
RunResultStreaming:
- Current agent: Agent(name="test_agent", ...)
- Current turn: 1
- Max turns: 10
- Is complete: True
- Final output (str):
    Hi there
- 1 new item(s)
- 1 raw response(s)
- 0 input guardrail result(s)
- 0 output guardrail result(s)
(See `RunResultStreaming` for more details)\
""")


class Foo(BaseModel):
    bar: str


@pytest.mark.asyncio
async def test_pretty_run_result_structured_output():
    model = FakeModel()
    model.set_next_output(
        [
            get_text_message("Test"),
            get_final_output_message(Foo(bar="Hi there").model_dump_json()),
        ]
    )

    agent = Agent(name="test_agent", model=model, output_type=Foo)
    result = await Runner.run(agent, input="Hello")

    assert pretty_print_result(result) == snapshot("""\
RunResult:
- Last agent: Agent(name="test_agent", ...)
- Final output (Foo):
    {
      "bar": "Hi there"
    }
- 2 new item(s)
- 1 raw response(s)
- 0 input guardrail result(s)
- 0 output guardrail result(s)
(See `RunResult` for more details)\
""")


@pytest.mark.asyncio
async def test_pretty_run_result_streaming_structured_output():
    model = FakeModel()
    model.set_next_output(
        [
            get_text_message("Test"),
            get_final_output_message(Foo(bar="Hi there").model_dump_json()),
        ]
    )

    agent = Agent(name="test_agent", model=model, output_type=Foo)
    result = Runner.run_streamed(agent, input="Hello")

    async for _ in result.stream_events():
        pass

    assert pretty_print_run_result_streaming(result) == snapshot("""\
RunResultStreaming:
- Current agent: Agent(name="test_agent", ...)
- Current turn: 1
- Max turns: 10
- Is complete: True
- Final output (Foo):
    {
      "bar": "Hi there"
    }
- 2 new item(s)
- 1 raw response(s)
- 0 input guardrail result(s)
- 0 output guardrail result(s)
(See `RunResultStreaming` for more details)\
""")


@pytest.mark.asyncio
async def test_pretty_run_result_list_structured_output():
    model = FakeModel()
    model.set_next_output(
        [
            get_text_message("Test"),
            get_final_output_message(
                json.dumps(
                    {
                        _WRAPPER_DICT_KEY: [
                            Foo(bar="Hi there").model_dump(),
                            Foo(bar="Hi there 2").model_dump(),
                        ]
                    }
                )
            ),
        ]
    )

    agent = Agent(name="test_agent", model=model, output_type=list[Foo])
    result = await Runner.run(agent, input="Hello")

    assert pretty_print_result(result) == snapshot("""\
RunResult:
- Last agent: Agent(name="test_agent", ...)
- Final output (list):
    [Foo(bar='Hi there'), Foo(bar='Hi there 2')]
- 2 new item(s)
- 1 raw response(s)
- 0 input guardrail result(s)
- 0 output guardrail result(s)
(See `RunResult` for more details)\
""")


@pytest.mark.asyncio
async def test_pretty_run_result_streaming_list_structured_output():
    model = FakeModel()
    model.set_next_output(
        [
            get_text_message("Test"),
            get_final_output_message(
                json.dumps(
                    {
                        _WRAPPER_DICT_KEY: [
                            Foo(bar="Test").model_dump(),
                            Foo(bar="Test 2").model_dump(),
                        ]
                    }
                )
            ),
        ]
    )

    agent = Agent(name="test_agent", model=model, output_type=list[Foo])
    result = Runner.run_streamed(agent, input="Hello")

    async for _ in result.stream_events():
        pass

    assert pretty_print_run_result_streaming(result) == snapshot("""\
RunResultStreaming:
- Current agent: Agent(name="test_agent", ...)
- Current turn: 1
- Max turns: 10
- Is complete: True
- Final output (list):
    [Foo(bar='Test'), Foo(bar='Test 2')]
- 2 new item(s)
- 1 raw response(s)
- 0 input guardrail result(s)
- 0 output guardrail result(s)
(See `RunResultStreaming` for more details)\
""")
