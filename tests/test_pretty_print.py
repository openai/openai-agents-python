import json
from dataclasses import dataclass

import pytest
from inline_snapshot import snapshot
from pydantic import BaseModel

from agents import Agent, RunContextWrapper, RunErrorDetails, Runner, RunResult
from agents.agent_output import _WRAPPER_DICT_KEY
from agents.testing import ScriptedModel
from agents.util._pretty_print import (
    pretty_print_result,
    pretty_print_run_error_details,
    pretty_print_run_result_streaming,
)

from .test_responses import get_final_output_message, get_text_message


@pytest.mark.asyncio
async def test_pretty_result():
    model = ScriptedModel()
    model.enqueue([get_text_message("Hi there")])

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


def test_pretty_result_handles_none_final_output():
    agent = Agent(name="none_agent")
    result = RunResult(
        input="Hello",
        new_items=[],
        raw_responses=[],
        final_output=None,
        input_guardrail_results=[],
        output_guardrail_results=[],
        tool_input_guardrail_results=[],
        tool_output_guardrail_results=[],
        context_wrapper=RunContextWrapper(context=None),
        _last_agent=agent,
    )

    assert pretty_print_result(result) == snapshot("""\
RunResult:
- Last agent: Agent(name="none_agent", ...)
- Final output (NoneType):
    None
- 0 new item(s)
- 0 raw response(s)
- 0 input guardrail result(s)
- 0 output guardrail result(s)
(See `RunResult` for more details)\
""")


def test_pretty_run_error_details():
    agent = Agent(name="error_agent")
    details = RunErrorDetails(
        input="Hello",
        new_items=[],
        raw_responses=[],
        last_agent=agent,
        context_wrapper=RunContextWrapper(context=None),
        input_guardrail_results=[],
        output_guardrail_results=[],
    )

    assert pretty_print_run_error_details(details) == snapshot("""\
RunErrorDetails:
- Last agent: Agent(name="error_agent", ...)
- 0 new item(s)
- 0 raw response(s)
- 0 input guardrail result(s)
- 0 output guardrail result(s)
- 0 tool input guardrail result(s)
- 0 tool output guardrail result(s)
(See `RunErrorDetails` for more details)\
""")


@pytest.mark.asyncio
async def test_pretty_run_result_streaming():
    model = ScriptedModel()
    model.enqueue([get_text_message("Hi there")])

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


@dataclass
class TextOutput:
    text: str

    def __str__(self) -> str:
        return self.text


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("output_kind", ["text", "model", "dataclass"])
async def test_result_str_escapes_terminal_controls(streaming, output_kind):
    payload = "Hello\x1b[2J\x1b]0;title\x07\x08\x00\x7f\x9b31m\x9d0;title\x9c"
    escaped = r"Hello\x1b[2J\x1b]0;title\x07\x08\x00\x7f\x9b31m\x9d0;title\x9c"
    model = ScriptedModel()
    if output_kind == "model":
        output_type = Foo
        response = Foo(bar=payload).model_dump_json()
    elif output_kind == "dataclass":
        output_type = TextOutput
        response = json.dumps({_WRAPPER_DICT_KEY: {"text": payload}})
    else:
        output_type = None
        response = payload
    model.enqueue([get_text_message(response)])
    agent = Agent(name="agent\x1b[2J\x9b31m", model=model, output_type=output_type)

    if streaming:
        result = Runner.run_streamed(agent, input="Hello")
        async for _ in result.stream_events():
            pass
    else:
        result = await Runner.run(agent, input="Hello")

    rendered = str(result)
    assert r'Agent(name="agent\x1b[2J\x9b31m", ...)' in rendered
    if output_kind == "model":
        assert result.final_output.bar == payload
        # JSON already escapes C0 controls; the diagnostic printer must also escape C1.
        assert r"\u001b[2J" in rendered
        assert r"\x9b31m\x9d0;title\x9c" in rendered
    elif output_kind == "dataclass":
        assert result.final_output.text == payload
        assert escaped in rendered
    else:
        assert result.final_output == payload
        assert escaped in rendered
    assert agent.name == "agent\x1b[2J\x9b31m"
    assert all(char >= " " or char in "\n\t" for char in rendered)
    assert not any("\x7f" <= char <= "\x9f" for char in rendered)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_result_str_preserves_readable_multiline_output(streaming):
    payload = "Hello, 世界 👋\r\n\tCafé\n\nlast line"
    model = ScriptedModel()
    model.enqueue([get_text_message(payload)])
    agent = Agent(name="test_agent", model=model)
    if streaming:
        result = Runner.run_streamed(agent, input="Hello")
        async for _ in result.stream_events():
            pass
    else:
        result = await Runner.run(agent, input="Hello")

    assert "    Hello, 世界 👋\n    \tCafé\n    \n    last line\n- 1 new item(s)" in str(result)
    assert result.final_output == payload


@pytest.mark.asyncio
async def test_pretty_run_result_structured_output():
    model = ScriptedModel()
    model.enqueue(
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
    model = ScriptedModel()
    model.enqueue(
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
    model = ScriptedModel()
    model.enqueue(
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
    model = ScriptedModel()
    model.enqueue(
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
