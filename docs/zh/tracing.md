---
search:
  exclude: true
---
# 追踪

Agents SDK 内置追踪功能，会收集智能体运行期间发生事件的完整记录：LLM 生成、工具调用、任务转移、安全防护措施，甚至包括发生的自定义事件。使用 [Traces 仪表板](https://platform.openai.com/traces)，你可以在开发与生产环境中调试、可视化并监控你的工作流。

!!!note

    默认启用追踪。禁用追踪有两种方式：

    1. 你可以通过设置环境变量 `OPENAI_AGENTS_DISABLE_TRACING=1` 来全局禁用追踪
    2. 你可以通过将 [`agents.run.RunConfig.tracing_disabled`][] 设为 `True` 来为单次运行禁用追踪

***对于在使用 OpenAI 的 API 且遵循零数据保留（ZDR）策略的组织，追踪功能不可用。***

## Traces 与 spans

-   **Traces** 表示一次端到端的“工作流”操作。它们由 Span 组成。Trace 具有以下属性：
    -   `workflow_name`：逻辑工作流或应用。例如 “Code generation” 或 “Customer service”。
    -   `trace_id`：Trace 的唯一 ID。如果你不传入，会自动生成。必须符合格式 `trace_<32_alphanumeric>`。
    -   `group_id`：可选的组 ID，用于关联同一对话的多个 trace。例如，你可以使用聊天线程 ID。
    -   `disabled`：若为 True，则不会记录该 trace。
    -   `metadata`：Trace 的可选元数据。
-   **Spans** 表示具有开始与结束时间的操作。Span 具有：
    -   `started_at` 和 `ended_at` 时间戳。
    -   `trace_id`，表示其所属的 trace
    -   `parent_id`，指向该 Span 的父 Span（如有）
    -   `span_data`，即关于该 Span 的信息。例如，`AgentSpanData` 包含关于 Agent 的信息，`GenerationSpanData` 包含关于 LLM 生成的信息，等等。

## 默认追踪

默认情况下，SDK 会追踪以下内容：

-   整个 `Runner.{run, run_sync, run_streamed}()` 会被包裹在一个 `trace()` 中。
-   每次智能体运行都会被包裹在 `agent_span()` 中
-   LLM 生成会被包裹在 `generation_span()` 中
-   工具调用中的函数调用会分别被包裹在 `function_span()` 中
-   安全防护措施会被包裹在 `guardrail_span()` 中
-   任务转移会被包裹在 `handoff_span()` 中
-   音频输入（语音转文本）会被包裹在 `transcription_span()` 中
-   音频输出（文本转语音）会被包裹在 `speech_span()` 中
-   相关的音频 span 可能会作为子项归于 `speech_group_span()`

默认情况下，trace 名称为 “Agent workflow”。如果你使用 `trace`，可以设置该名称；或者你也可以通过 [`RunConfig`][agents.run.RunConfig] 配置名称与其他属性。

此外，你可以设置 [自定义 trace 进程器](#custom-tracing-processors) 将 trace 推送到其他目的地（作为替代目的地或第二目的地）。

## 更高层级的 traces

有时，你可能希望多次 `run()` 调用属于同一个 trace。你可以通过将整个代码包裹在 `trace()` 中来实现。

```python
from agents import Agent, Runner, trace

async def main():
    agent = Agent(name="Joke generator", instructions="Tell funny jokes.")

    with trace("Joke workflow"): # (1)!
        first_result = await Runner.run(agent, "Tell me a joke")
        second_result = await Runner.run(agent, f"Rate this joke: {first_result.final_output}")
        print(f"Joke: {first_result.final_output}")
        print(f"Rating: {second_result.final_output}")
```

1. 因为对 `Runner.run` 的两次调用被包裹在 `with trace()` 中，各次运行会归入同一个整体 trace，而不是创建两个 trace。

## 创建 traces

你可以使用 [`trace()`][agents.tracing.trace] 函数来创建 trace。Trace 需要开始与结束。你有两种方式：

1. **推荐**：将 trace 用作上下文管理器，即 `with trace(...) as my_trace`。这会在合适的时间自动开始与结束 trace。
2. 你也可以手动调用 [`trace.start()`][agents.tracing.Trace.start] 和 [`trace.finish()`][agents.tracing.Trace.finish]。

当前 trace 通过 Python 的 [`contextvar`](https://docs.python.org/3/library/contextvars.html) 进行跟踪。这意味着它会自动适配并发场景。如果你手动开始/结束 trace，你需要将 `mark_as_current` 和 `reset_current` 传给 `start()`/`finish()` 来更新当前 trace。

## 创建 spans

你可以使用各种 [`*_span()`][agents.tracing.create] 方法来创建 span。一般来说，你不需要手动创建 span。可使用 [`custom_span()`][agents.tracing.custom_span] 函数来跟踪自定义 span 信息。

Span 会自动归属于当前 trace，并嵌套在最近的当前 span 下；当前 span 通过 Python 的 [`contextvar`](https://docs.python.org/3/library/contextvars.html) 进行跟踪。

## 敏感数据

某些 span 可能会捕获潜在的敏感数据。

`generation_span()` 会存储 LLM 生成的输入/输出，而 `function_span()` 会存储函数调用的输入/输出。这些可能包含敏感数据，因此你可以通过 [`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] 禁用对这些数据的采集。

同样地，音频 span 默认包含输入与输出音频的 base64 编码 PCM 数据。你可以通过配置 [`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] 禁用采集这些音频数据。

默认情况下，`trace_include_sensitive_data` 为 `True`。你可以在运行应用前，通过导出 `OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA` 环境变量为 `true/1` 或 `false/0` 来在无需改动代码的情况下设置默认值。

## 自定义追踪进程器

追踪的高层架构为：

-   在初始化时，我们创建一个全局 [`TraceProvider`][agents.tracing.setup.TraceProvider]，负责创建 trace。
-   我们使用一个 [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] 配置 `TraceProvider`，它会将 trace/span 分批发送到 [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter]，后者会将 span 和 trace 分批导出到 OpenAI 后端。

要自定义该默认设置，将 trace 发送到替代或额外的后端，或修改 exporter 行为，你有两种选择：

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] 允许你添加一个**额外的** trace 进程器，它会在 trace 和 span 就绪时接收它们。这让你能在将 trace 发送到 OpenAI 后端之外执行你自己的处理。
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] 允许你用自己的 trace 进程器**替换**默认进程器。这意味着除非你包含一个会执行该行为的 `TracingProcessor`，否则 trace 将不会被发送到 OpenAI 后端。


## 使用非 OpenAI 模型进行追踪

你可以使用 OpenAI API key 配合非 OpenAI Models，在无需禁用追踪的情况下，在 OpenAI Traces 仪表板中启用免费的追踪。

```python
import os
from agents import set_tracing_export_api_key, Agent, Runner
from agents.extensions.models.litellm_model import LitellmModel

tracing_api_key = os.environ["OPENAI_API_KEY"]
set_tracing_export_api_key(tracing_api_key)

model = LitellmModel(
    model="your-model-name",
    api_key="your-api-key",
)

agent = Agent(
    name="Assistant",
    model=model,
)
```

如果你只需要为单次运行使用不同的追踪 key，请通过 `RunConfig` 传入，而不是更改全局 exporter。

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(tracing={"api_key": "sk-tracing-123"}),
)
```

## 附加说明
- 在 Openai Traces 仪表板中查看免费 trace。


## 生态系统集成

以下社区与供应商集成支持 OpenAI Agents SDK 的追踪能力。

### 外部追踪进程器列表

-   [Weights & Biases](https://weave-docs.wandb.ai/guides/integrations/openai_agents)
-   [Arize-Phoenix](https://docs.arize.com/phoenix/tracing/integrations-tracing/openai-agents-sdk)
-   [Future AGI](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/openai_agents)
-   [MLflow (self-hosted/OSS)](https://mlflow.org/docs/latest/tracing/integrations/openai-agent)
-   [MLflow (Databricks hosted)](https://docs.databricks.com/aws/en/mlflow/mlflow-tracing#-automatic-tracing)
-   [Braintrust](https://braintrust.dev/docs/guides/traces/integrations#openai-agents-sdk)
-   [Pydantic Logfire](https://logfire.pydantic.dev/docs/integrations/llms/openai/#openai-agents)
-   [AgentOps](https://docs.agentops.ai/v1/integrations/agentssdk)
-   [Scorecard](https://docs.scorecard.io/docs/documentation/features/tracing#openai-agents-sdk-integration)
-   [Keywords AI](https://docs.keywordsai.co/integration/development-frameworks/openai-agent)
-   [LangSmith](https://docs.smith.langchain.com/observability/how_to_guides/trace_with_openai_agents_sdk)
-   [Maxim AI](https://www.getmaxim.ai/docs/observe/integrations/openai-agents-sdk)
-   [Comet Opik](https://www.comet.com/docs/opik/tracing/integrations/openai_agents)
-   [Langfuse](https://langfuse.com/docs/integrations/openaiagentssdk/openai-agents)
-   [Langtrace](https://docs.langtrace.ai/supported-integrations/llm-frameworks/openai-agents-sdk)
-   [Okahu-Monocle](https://github.com/monocle2ai/monocle)
-   [Galileo](https://v2docs.galileo.ai/integrations/openai-agent-integration#openai-agent-integration)
-   [Portkey AI](https://portkey.ai/docs/integrations/agents/openai-agents)
-   [LangDB AI](https://docs.langdb.ai/getting-started/working-with-agent-frameworks/working-with-openai-agents-sdk)
-   [Agenta](https://docs.agenta.ai/observability/integrations/openai-agents)
-   [PostHog](https://posthog.com/docs/llm-analytics/installation/openai-agents)
-   [Traccia](https://traccia.ai/docs/integrations/openai-agents)