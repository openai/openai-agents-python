---
search:
  exclude: true
---
# 测试

SDK 为智能体工作流、沙箱会话、实时会话和语音管线提供确定性、与提供商无关的测试工具。这些工具在内存中运行，不会向模型、沙箱提供商或 Realtime API 发出请求，并会记录由 SDK 管理的标准化交互。下方的可运行方案会为每次运行禁用追踪，以免配置 OpenAI API 密钥后，默认追踪处理器上传测试活动。

使用这些工具测试由应用程序和 SDK 管理的编排：工具执行、任务转移、安全防护措施、重试、流式传输、会话行为、沙箱功能、实时事件处理以及语音管线组合。对于由外部模型、网络协议、沙箱提供商或音频系统管理的行为，请使用真实的提供商适配器或集成环境。

## 方案查找 {#find-the-recipe-you-need}

| 我的目标 | 使用 | 前往 |
| --- | --- | --- |
| 返回固定的最终答案 | 带有 `assistant_message()` 的 `ScriptedModel` | [返回固定响应](#return-a-fixed-response) |
| 执行多轮工具循环 | `function_call()`，后接智能体响应 | [测试工具工作流](#test-a-tool-workflow) |
| 根据请求选择响应 | `ModelStep.respond()` 或 `responder` 映射 | [根据请求生成响应](#derive-a-response-from-the-request) |
| 断言运行器发送给模型的内容 | `calls`、`first_call` 或 `last_call` | [检查模型调用](#inspect-model-calls) |
| 测试流式运行 | 常规响应步骤，或用于精确事件的 `ModelStep.stream()` | [测试流式传输](#test-streaming) |
| 测试错误或重试决策 | `ModelStep.raise_error()` | [注入模型故障](#inject-model-failures) |
| 检测意外的工作流变更 | 精确的 FIFO 步骤加 `assert_complete()` | [检测工作流偏移](#detect-workflow-drift) |
| 在不启动沙箱的情况下测试 `SandboxAgent` | `scripted_sandbox_session()` 加 `ScriptedModel` | [测试沙箱智能体工作流](#test-a-sandbox-agent-workflow) |
| 匹配沙箱调用或生成其结果 | 沙箱步骤上的 `match` 或 `responder` | [配置沙箱步骤](#configure-sandbox-steps) |
| 在不建立连接的情况下测试实时会话 | `ScriptedRealtimeModel` 和 `RealtimeStep` | [测试实时会话](#test-a-realtime-session) |
| 测试实时工具工作流 | 发出 `RealtimeModelToolCallEvent` 并预期工具输出 | [测试实时工具工作流](#test-a-realtime-tool-workflow) |
| 测试静态或流式语音管线 | `ScriptedSTTModel`、`ScriptedTTSModel`，以及脚本化或真实的工作流 | [测试语音管线](#test-a-voice-pipeline) |
| 测试提供商序列化或线上传输载荷 | 使用受控网络传输的真实提供商适配器 | [选择正确的边界](#choose-the-correct-boundary) |

## 导入 {#imports}

测试 API 位于其所替代的运行时边界旁：

| 边界 | 导入路径 |
| --- | --- |
| 智能体模型和沙箱工作流 | `agents.testing` |
| 实时模型传输 | `agents.realtime.testing` |
| 语音 STT、TTS 和工作流组件 | `agents.voice.testing` |

测试符号特意不包含在顶层 `agents` 导入中。

## 智能体工作流方案 {#agent-workflow-recipes}

### 固定响应的返回 {#return-a-fixed-response}

为每个预期的模型调用传入一个标准化输出项序列。输出序列简写形式会为一次请求接收确定性的响应 ID 和用量。

```python
import pytest

from agents import Agent, RunConfig, Runner
from agents.testing import ScriptedModel, assistant_message


@pytest.mark.asyncio
async def test_fixed_response() -> None:
    model = ScriptedModel([[assistant_message("Paris is the capital of France.")]])
    agent = Agent(name="Geography assistant", model=model)

    result = await Runner.run(
        agent,
        "What is the capital of France?",
        run_config=RunConfig(tracing_disabled=True),
    )

    assert result.final_output == "Paris is the capital of France."
    assert len(model.calls) == 1
    model.assert_complete()
```

使用 `model.assert_complete()` 完成确定性工作流测试。它可以捕获工作流在用完所有已配置步骤之前停止的情况。

### 工具工作流的测试 {#test-a-tool-workflow}

编写一个调用工具的模型响应脚本，再编写一个生成最终答案的响应脚本。真实的 SDK 工具管线会在这些模型调用之间运行。

```python
import pytest

from agents import Agent, RunConfig, Runner
from agents.decorators import tool
from agents.testing import ScriptedModel, assistant_message, function_call


@tool
def get_weather(city: str) -> str:
    """Return the weather for a city."""
    return f"{city}: sunny"


@pytest.mark.asyncio
async def test_tool_workflow() -> None:
    model = ScriptedModel(
        [
            [function_call("get_weather", {"city": "Tokyo"}, call_id="call_1")],
            [assistant_message("It is sunny in Tokyo.")],
        ]
    )
    agent = Agent(name="Weather assistant", model=model, tools=[get_weather])

    result = await Runner.run(
        agent,
        "What is the weather in Tokyo?",
        run_config=RunConfig(tracing_disabled=True),
    )

    assert result.final_output == "It is sunny in Tokyo."
    assert len(model.calls) == 2
    assert model.last_call is not None
    assert any(item.get("type") == "function_call_output" for item in model.last_call.input)
    model.assert_complete()
```

此模式涵盖工具输入验证、执行、结果转换、钩子、安全防护措施以及下一轮模型交互。直接调用 Python 函数会绕过这些 SDK 行为。

### 基于请求的响应生成 {#derive-a-response-from-the-request}

当响应确实依赖标准化模型调用，或断言应位于模型边界时，请使用 `ModelStep.respond()`。响应器可以是同步或异步的，并且可以返回 `ScriptedModel` 接受的任何步骤形式。

```python
import pytest

from agents import Agent, RunConfig, Runner
from agents.testing import ModelCall, ModelStep, ScriptedModel, assistant_message


def respond(call: ModelCall):
    assert call.streamed is False
    assert call.input == [{"content": "Summarize this", "role": "user"}]
    return {"output": [assistant_message("Handled the normalized request.")]}


@pytest.mark.asyncio
async def test_request_aware_response() -> None:
    model = ScriptedModel([ModelStep.respond(respond)])
    agent = Agent(name="Assistant", model=model)

    result = await Runner.run(
        agent,
        "Summarize this",
        run_config=RunConfig(tracing_disabled=True),
    )

    assert result.final_output == "Handled the normalized request."
    model.assert_complete()
```

`ScriptedModel` 接受 `ModelStep`、与之等效的字典形式、`ModelResponse`、标准化输出项序列或异常。如果响应不依赖调用，请优先使用固定输出序列，因为固定脚本能让意外轮次更易于诊断。

### 模型调用的检查 {#inspect-model-calls}

`ScriptedModel` 会在解析每次调用或引发所选步骤的异常之前记录该调用。

| 成员 | 内容 |
| --- | --- |
| `calls` | 按调用顺序排列的每个 `ModelCall` |
| `first_call` | 第一次调用，或 `None` |
| `last_call` | 最近一次调用，或 `None` |
| `remaining_steps` | 尚未消耗的已配置步骤数量 |

常见断言包括 `call.input`、`call.model_settings`、`call.tools`、`call.handoffs` 和 `call.streamed`。可变请求数据会在调用边界生成快照，并且每个公共历史记录访问器都会返回分离的快照。工具、任务转移、输出 schema 和追踪对象会保留其运行时标识。

结构化的 `call_index` 和 `input_index` 错误字段从零开始，因此可直接索引 `calls[...]` 或提供的步骤序列。可读错误消息显示从一开始计数的调用或步骤编号。

当一个测试需要逐步追加模型步骤时，请使用 `enqueue()` 或 `extend()`。对于独立场景，请创建新的 `ScriptedModel`；该工具不会重置已消耗的步骤或调用历史记录。

### 流式传输的测试 {#test-streaming}

常规响应步骤同时支持 `Runner.run()` 和 `Runner.run_streamed()`。对于常见的智能体消息、推理项、函数调用和应用补丁调用，`ScriptedModel` 会生成标准化的开始事件、增量事件、项目完成事件和终止响应事件。终止响应包含完整输出和用量。

仅当标准化 `TResponseStreamEvent` 的精确序列属于待测试行为的一部分时，才使用 `ModelStep.stream()`：

```python
step = ModelStep.stream(
    events,
    output=[assistant_message("The terminal output used by the runner.")],
)
```

`events` 可以是固定序列，也可以是接收已记录 `ModelCall` 的异步工厂。可选的 `output` 是在非流式调用中使用同一步骤时返回的响应。精确流式事件是 SDK 标准化事件，而不是 Responses API 或 Chat Completions 的线上传输分块。

自动流式传输会拒绝尚未实现增量生命周期的标准化输出项种类。对于这些项，请使用 `ModelStep.stream(...)`，而不是依赖不完整的事件序列。

### 模型故障的注入 {#inject-model-failures}

使用 `ModelStep.raise_error()` 使一次模型调用失败。可选的重试建议仅适用于该特定脚本化错误：

```python
from agents import ModelRetryAdvice
from agents.testing import ModelStep


step = ModelStep.raise_error(
    RuntimeError("temporary failure"),
    retry_advice=ModelRetryAdvice(suggested=True, replay_safety="safe"),
)
```

运行器的重试策略决定该建议是否会触发另一次尝试。每次重试都是一次新的模型调用，并会消耗下一个脚本化步骤。Python 辅助工具接受固定的 `ModelRetryAdvice` 值；当重试建议本身需要根据尝试次数动态变化时，请使用自定义 `Model`。

### 工作流偏移的检测 {#detect-workflow-drift}

将脚本化调用视为预期的工作流结构。额外的模型请求会引发 `UnexpectedModelCall`；提前退出会留下步骤，供 `assert_complete()` 报告。

当测试框架支持 teardown 或 finalizer 时，如果还希望在其他断言失败后报告未消耗的步骤，请将 `assert_complete()` 放在其中。不要在常规回归测试中捕获不匹配错误。

| 错误 | 结构化字段 | 含义 |
| --- | --- | --- |
| `InvalidModelStep` | `reason`、`input_index` | 步骤格式错误，并在进入队列前被拒绝 |
| `UnexpectedModelCall` | `call`、`call_index` | 脚本结束后，工作流又进行了一次模型调用 |
| `UnconsumedModelSteps` | `remaining_steps` | 工作流在使用完所有步骤前结束 |

## 沙箱智能体方案 {#sandbox-agent-recipes}

### 沙箱智能体工作流的测试 {#test-a-sandbox-agent-workflow}

将 `ScriptedModel` 与 `scripted_sandbox_session()` 结合使用，可在不创建本地容器或远程沙箱的情况下运行真实的 `SandboxAgent` 运行时。模型脚本选择一个能力工具，而沙箱脚本定义相应 `SandboxSession` 方法返回的内容。

```python
import pytest

from agents import RunConfig, Runner
from agents.sandbox import ExecResult, SandboxAgent
from agents.sandbox.capabilities import Shell
from agents.testing import (
    ScriptedModel,
    assistant_message,
    function_call,
    scripted_sandbox_session,
)


@pytest.mark.asyncio
async def test_sandbox_workflow() -> None:
    sandbox = scripted_sandbox_session(
        [
            {
                "method": "exec",
                "match": lambda call: call.args == ("pwd",),
                "result": ExecResult(
                    stdout=b"/workspace\n",
                    stderr=b"",
                    exit_code=0,
                ),
            }
        ]
    )
    model = ScriptedModel(
        [
            [function_call("exec_command", {"cmd": "pwd"}, call_id="call_1")],
            [assistant_message("The workspace is /workspace.")],
        ]
    )
    agent = SandboxAgent(
        name="Workspace assistant",
        model=model,
        capabilities=[Shell()],
    )

    async with sandbox:
        result = await Runner.run(
            agent,
            "Which directory are you in?",
            run_config=RunConfig(
                sandbox={"session": sandbox},
                tracing_disabled=True,
            ),
        )

    assert result.final_output == "The workspace is /workspace."
    assert [call.method for call in sandbox.calls] == ["exec"]
    sandbox.assert_complete()
    model.assert_complete()
```

此测试会跨越两个标准化 SDK 边界。它涵盖工具参数验证、能力路由、沙箱会话调用、将工具结果传递至下一轮模型交互以及最终输出处理。它不会测试真实模型是否会选择该命令，也不会测试真实沙箱提供商如何执行该命令。

### 沙箱步骤的配置 {#configure-sandbox-steps}

每次匹配的沙箱调用都会消耗全局 FIFO 序列中的下一个步骤。方法不匹配、匹配器拒绝或匹配器异常都会让该步骤保持待处理状态。设置 `method`，只选择一种结果，并且仅在调用详情很重要时添加 `match`。

| 步骤成员 | 适用情形 |
| --- | --- |
| `result` | 方法应返回固定的类型化值 |
| `responder` | 结果取决于分离的 `SandboxCall` |
| `error` | 方法应引发特定异常 |
| `match` | 除非匹配器返回 `False` 以外的值，否则应在生成结果前拒绝该调用 |

支持的脚本化方法名称包括 `apply_patch`、`exec`、`ls`、`mkdir`、`pty_exec_start`、`pty_write_stdin`、`read`、`rm` 和 `write`。仅公开已配置且面向模型的能力。配置任一 PTY 方法时，这两个 PTY 方法都会一起公开，因为它们共同构成一项交互式 shell 能力，但调用仍会消耗全局 FIFO 脚本。

`sandbox.calls` 包含分离的 `SandboxCall` 快照，其中有从零开始的 `call_index`、`method`、位置参数 `args` 和只读 `kwargs`。静态结果也会在创建脚本时生成快照。支持 `io.BytesIO` 和 `io.StringIO` 值；对于其他实时流对象或生命周期行为，请使用自定义沙箱会话。

| 错误 | 结构化字段 | 含义 |
| --- | --- | --- |
| `InvalidSandboxStep` | `reason`、`input_index`、`method` | 步骤格式错误或指定了不受支持的方法 |
| `UnexpectedSandboxCall` | `call`、`call_index`、`actual_method`、`expected_method`、`remaining_steps` | 工作流调用了错误的方法，或在脚本结束后继续运行 |
| `SandboxCallMatcherError` | `call`、`call_index`、`method` | 步骤匹配器返回了 `False` |
| `UnconsumedSandboxSteps` | `remaining_steps`、`pending_methods` | 工作流在使用完所有步骤前结束 |

返回的对象就是会话本身。请将其直接传递给 `RunConfig(sandbox={"session": sandbox})`；不存在包装器 `.session` 属性。

## 实时方案 {#realtime-recipes}

### 实时会话的测试 {#test-a-realtime-session}

`ScriptedRealtimeModel` 实现了 Python SDK 的标准化 `RealtimeModel` 边界。每个 `RealtimeStep` 都会匹配一个出站 `RealtimeModelSendEvent`，随后发出标准化的入站 `RealtimeModelEvent` 对象或引发注入的错误。

```python
import pytest

from agents.realtime import (
    RealtimeAgent,
    RealtimeModelOutputTextDeltaEvent,
    RealtimeModelSendUserInput,
    RealtimeRawModelEvent,
    RealtimeRunner,
)
from agents.realtime.testing import RealtimeStep, ScriptedRealtimeModel


@pytest.mark.asyncio
async def test_realtime_message() -> None:
    reply = RealtimeModelOutputTextDeltaEvent(
        item_id="item_1",
        delta="Hello!",
        response_id="response_1",
    )
    model = ScriptedRealtimeModel(
        [
            RealtimeStep(
                expect=RealtimeModelSendUserInput(user_input="Hello"),
                emit=[reply],
            )
        ]
    )
    runner = RealtimeRunner(
        RealtimeAgent(name="Assistant"),
        model=model,
        config={"tracing_disabled": True},
    )

    observed_reply = False
    async with await runner.run() as session:
        await session.send_message("Hello")
        async for event in session:
            if isinstance(event, RealtimeRawModelEvent) and event.data == reply:
                observed_reply = True
                break

    assert observed_reply
    assert model.sent_events == (RealtimeModelSendUserInput(user_input="Hello"),)
    assert model.closed is True
    model.assert_complete()
```

预期值可以是精确事件值、通过 `isinstance` 匹配的事件类，或接收出站事件并在匹配时返回 `True` 的可调用对象。默认启用严格模式。使用 `strict=False` 时，无关的出站事件会被记录，但不会消耗待处理步骤；当会话发出与待测试行为无关的附带事件时，这种方式很有用。

使用 `connect_events` 在连接期间发出入站事件。使用 `connect_error` 或 `close_error` 注入生命周期故障，使用 `RealtimeStep(error=...)` 注入与某次已匹配发送相关的故障。一个步骤不能同时定义 `emit` 和 `error`。

### 实时工具工作流的测试 {#test-a-realtime-tool-workflow}

将真实函数工具附加到 `RealtimeAgent`，发出标准化工具调用，并预期 SDK 通过模型边界发送工具输出。将 `async_tool_calls` 设置为 `False`，可让这个小型示例在连接期间完成，而无须使用测试专用的等待机制。

```python
import pytest

from agents.decorators import tool
from agents.realtime import (
    RealtimeAgent,
    RealtimeModelSendToolOutput,
    RealtimeModelToolCallEvent,
    RealtimeRunner,
)
from agents.realtime.testing import RealtimeStep, ScriptedRealtimeModel


@tool
def lookup_order(order_id: str) -> str:
    """Look up an order by ID."""
    return f"Order {order_id} has shipped."


@pytest.mark.asyncio
async def test_realtime_tool_workflow() -> None:
    tool_call = RealtimeModelToolCallEvent(
        name="lookup_order",
        call_id="call_1",
        arguments='{"order_id":"order_123"}',
    )

    def matches_tool_output(event) -> bool:
        return (
            isinstance(event, RealtimeModelSendToolOutput)
            and event.tool_call.call_id == "call_1"
            and event.output == "Order order_123 has shipped."
        )

    model = ScriptedRealtimeModel(
        [RealtimeStep(expect=matches_tool_output)],
        connect_events=[tool_call],
    )
    agent = RealtimeAgent(
        name="Order assistant",
        tools=[lookup_order],
    )
    runner = RealtimeRunner(
        agent,
        model=model,
        config={"async_tool_calls": False, "tracing_disabled": True},
    )

    async with await runner.run():
        pass

    model.assert_complete()
```

这会运行真实的实时工具查找、参数验证、执行和输出路由。它无法证明真实模型会选择该工具。

### 实时调用和生命周期的检查 {#inspect-realtime-calls-and-lifecycle}

| 成员 | 内容 |
| --- | --- |
| `connect_calls` | 不含凭据的分离连接快照 |
| `sent_events` | 按调用顺序排列的分离出站事件快照 |
| `remaining_steps` | 尚未发生的预期出站发送 |
| `listeners` | 当前已注册的监听器对象 |
| `connected`、`closed`、`close_calls` | 当前内存中生命周期状态 |

连接历史记录只记录是否提供了 API 密钥或标头字段，绝不会存储它们的值。URL 快照会移除用户信息、查询参数和片段。可变事件数据和设置会被分离，而工具、任务转移和播放跟踪器等实时 SDK 对象会保留其标识。

最后调用 `model.assert_complete()`，并让 `RealtimeSession` 异步上下文管理器关闭模型。Python 工具特意不提供待处理的预期 Promise、隐式超时或单独的 `assert_closed()` 辅助工具。

| 错误 | 结构化字段 | 含义 |
| --- | --- | --- |
| `UnexpectedRealtimeSend` | `actual`、`expected` | 严格出站发送与下一步骤不匹配，或已没有剩余步骤 |
| `UnconsumedRealtimeSteps` | `remaining_steps` | 会话在完成所有预期发送前结束 |
| `RealtimeScriptError` | 无 | 脚本在无效的生命周期状态下使用，例如在断开连接时发送 |

## 语音管线方案 {#voice-pipeline-recipes}

### 语音管线的测试 {#test-a-voice-pipeline}

将脚本化 STT 和 TTS 模型、`SingleAgentVoiceWorkflow` 以及由 `ScriptedModel` 支持的智能体结合起来，可在不发出提供商请求的情况下测试完整的语音转文本 -> 智能体 -> 文本转语音管线。

```python
import numpy as np
import pytest

from agents import Agent
from agents.testing import ScriptedModel, assistant_message
from agents.voice import AudioInput, SingleAgentVoiceWorkflow, VoicePipeline
from agents.voice.testing import (
    ScriptedSTTModel,
    ScriptedTTSModel,
    TTSResult,
    pcm16_samples,
)


@pytest.mark.asyncio
async def test_voice_pipeline() -> None:
    model = ScriptedModel([[assistant_message("Hello there.")]])
    stt = ScriptedSTTModel("hello")
    pcm = pcm16_samples([0, 100, -100, 0])
    tts = ScriptedTTSModel([TTSResult([pcm])])
    pipeline = VoicePipeline(
        workflow=SingleAgentVoiceWorkflow(Agent(name="Voice assistant", model=model)),
        stt_model=stt,
        tts_model=tts,
        config={"tracing_disabled": True, "tts_settings": {"buffer_size": 1}},
    )

    result = await pipeline.run(AudioInput(np.zeros(2, dtype=np.int16)))
    events = [event async for event in result.stream()]

    assert events
    assert [call.text for call in tts.calls] == ["Hello there."]
    stt.assert_complete()
    tts.assert_complete()
    model.assert_complete()
```

当测试目标是管线的 STT/TTS 生命周期而非智能体编排时，请改用 `ScriptedVoiceWorkflow`：

```python
from agents.voice.testing import ScriptedVoiceWorkflow


workflow = ScriptedVoiceWorkflow(
    turns=["Hello there."],
    start="Welcome.",
)
```

`start` 步骤由 `on_start()` 消耗。仅当使用 `StreamedAudioInput` 时，`VoicePipeline` 才会调用 `on_start()`；静态 `AudioInput` 运行不会消耗 `start`。每个常规轮次都会记录其转录文本，并消耗一个已配置的结果。一个字符串对应一个片段；字符串序列可在文本分割和 TTS 前控制片段边界。

### 流式转录的测试 {#test-streamed-transcription}

`ScriptedSTTModel` 接受静态 `transcriptions` 和独立编写脚本的流式 `sessions`。会话可以是 `ScriptedTranscriptionSession`、转录轮次序列、异常或单个字符串：

```python
from agents.voice.testing import ScriptedSTTModel, ScriptedTranscriptionSession


session = ScriptedTranscriptionSession(["first turn", "second turn"])
stt = ScriptedSTTModel(sessions=[session])
```

关闭 `ScriptedTranscriptionSession` 会停止迭代，并保留被跳过的轮次，供 `assert_complete()` 报告。类似地，`ScriptedTTSModel` 每次调用都会消耗一个 `TTSResult`、字节块序列或异常。

### 语音调用的检查 {#inspect-voice-calls}

| 组件 | 记录的历史 |
| --- | --- |
| `ScriptedSTTModel` | `calls`、`session_calls` 和实时 `created_sessions` 标识 |
| `ScriptedTTSModel` | 包含文本和分离设置的 `calls` |
| `ScriptedVoiceWorkflow` | 按轮次顺序排列的 `transcriptions` |

静态音频缓冲区和可变设置会在调用时生成快照。`StreamedAudioInput` 和创建的转录会话对象会保留其实时标识，因为管线仍会继续使用它们。

| 错误 | 结构化字段 | 含义 |
| --- | --- | --- |
| `UnexpectedVoiceCall` | `operation` | 静态转录、流式会话、TTS 调用、工作流启动或工作流轮次没有已配置的步骤 |
| `UnconsumedVoiceSteps` | `remaining_steps` | 仍有一个或多个已配置的语音步骤 |

对测试配置的每个脚本化语音组件调用 `assert_complete()`。`ScriptedSTTModel.assert_complete()` 还会检查其创建的转录会话中的轮次。

## 正确边界的选择 {#choose-the-correct-boundary}

当测试应运行 SDK 运行循环、工具、任务转移、安全防护措施、会话、重试或标准化流式传输，而不依赖模型提供商时，请使用 `ScriptedModel`。

当测试应运行 `SandboxAgent` 功能和编排，而不启动沙箱提供商时，请将 `scripted_sandbox_session()` 与 `ScriptedModel` 配合使用。对于提供商创建、进程执行、文件系统保真度、持久化、资源限制和隔离检查，请保留针对真实沙箱提供商的集成测试。

当测试应运行 `RealtimeSession` 行为或 `RealtimeAgent` 工具和任务转移编排，而不建立 WebSocket 连接时，请使用 `ScriptedRealtimeModel`。原始实时客户端/服务器事件、身份验证、网络恢复和音频传输行为应在真实传输层或集成环境中测试。Realtime API 会话会在客户端发送输入并接收事件期间保持连接打开，因此这些网络和协议问题属于标准化模型边界之下。有关生产连接架构，请参阅 [OpenAI Realtime API 指南](https://developers.openai.com/api/docs/guides/realtime)。

当测试应运行 STT/TTS 排序、流式转录清理、工作流片段交付或完整的语音管线组合，而不依赖语音提供商时，请使用语音测试组件。当测试对象是转录质量、生成语音、编码兼容性、延迟或播放时，请使用真实音频模型和具有代表性的音频。

不要使用这些工具测试 Responses API 或 Chat Completions 的请求序列化、身份验证标头、提供商默认值、HTTP 载荷、提供商流分块、实时线上传输帧或提供商特定的生命周期行为。对于这些测试，请保留真实适配器，并替换或控制其网络边界。使用 `openai` v3 时，OpenAI 适配器测试应使用 `httpx2` 的请求、响应、传输和异常类型；旧版 `httpx` 不是 Agents SDK 的核心依赖项。

## 最终检查清单 {#final-checklist}

- 仅为标准化模型、沙箱会话、实时模型或语音管线边界所管理的交互编写脚本。
- 断言重要的公共请求或调用字段，而不是运行器的私有状态。
- 优先使用固定响应步骤；仅对依赖请求的行为使用响应器。
- 优先使用自动模型流式传输；仅当事件级行为很重要时才使用精确流。
- 在每个脚本化组件测试结束时调用其 `assert_complete()` 方法。
- 当外围测试管理相应生命周期时，请使用异步上下文管理器清理实时和沙箱生命周期。
- 断言结构化错误字段，而不是解析可读消息。
- 使用具有受控网络传输的真实适配器执行提供商线上传输测试。

## 范围与当前限制 {#scope-and-current-limitations}

测试模块特意不提供：

- 每种标准化模型输出项的便捷构建器。常见情况请使用 `assistant_message()` 和 `function_call()`，其他标准化项则直接传入。
- 提供商协议模拟器。精确模型流使用标准化 SDK 事件，而不是 Responses API 或 Chat Completions 的线上传输分块。
- 高层级的模拟实时服务器。测试会显式匹配标准化出站发送，并发出场景所需的标准化入站事件。
- 无序沙箱或实时预期。这两个工具都会按一个全局顺序消耗预期步骤。
- 测试运行器特定的匹配器、fixture、隐式超时或自动 teardown。
- 重置 API。`ScriptedModel` 支持使用 `enqueue()` 和 `extend()` 构建增量脚本，但对于独立场景，请创建新的脚本化组件。

当测试需要格式错误的流、受控暂停或并发、精确取消，或脚本化工具无法保留的生命周期边界时，请自定义实现相应的公共接口，并在测试中记录该专用边界。

## API 参考 {#api-reference}

- [`agents.testing`](ref/testing.md)
- [`agents.realtime.testing`](ref/realtime/testing.md)
- [`agents.voice.testing`](ref/voice/testing.md)