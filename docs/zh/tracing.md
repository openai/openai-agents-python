---
search:
  exclude: true
---
# 追踪

Agents SDK 内置追踪功能，会在智能体运行期间收集全面的事件记录：LLM 生成、工具调用、任务转移、安全防护措施，甚至是发生的自定义事件。使用 [Traces 仪表板](https://platform.openai.com/traces)，你可以在开发和生产环境中调试、可视化并监控你的工作流。

!!!note

    追踪默认启用。有两种方式可以禁用追踪：

    1. 你可以通过设置环境变量 `OPENAI_AGENTS_DISABLE_TRACING=1` 来全局禁用追踪
    2. 你可以通过将 [`agents.run.RunConfig.tracing_disabled`][] 设置为 `True` 来为单次运行禁用追踪

***对于在使用 OpenAI API 且遵循 Zero Data Retention (ZDR) 策略的组织，追踪不可用。***

## Traces 与 spans

-   **Traces** 表示一个“工作流”的单次端到端操作。它们由 Spans 组成。Traces 具有以下属性：
    -   `workflow_name`：逻辑工作流或应用。例如 “Code generation” 或 “Customer service”。
    -   `trace_id`：Trace 的唯一 ID。如果你未传入，会自动生成。格式必须为 `trace_<32_alphanumeric>`。
    -   `group_id`：可选的分组 ID，用于将同一对话中的多个 trace 关联起来。例如，你可以使用聊天线程 ID。
    -   `disabled`：若为 True，则不会记录该 trace。
    -   `metadata`：该 trace 的可选元数据。
-   **Spans** 表示具有开始与结束时间的操作。Spans 具有：
    -   `started_at` 和 `ended_at` 时间戳。
    -   `trace_id`：表示其所属的 trace
    -   `parent_id`：指向该 Span 的父 Span（如果有）
    -   `span_data`：关于该 Span 的信息。例如，`AgentSpanData` 包含关于 Agent 的信息，`GenerationSpanData` 包含关于 LLM 生成的信息，等等。

## 默认追踪

默认情况下，SDK 会追踪以下内容：

-   整个 `Runner.{run, run_sync, run_streamed}()` 会被包裹在 `trace()` 中。
-   每次智能体运行时，都会被包裹在 `agent_span()` 中
-   LLM 生成会被包裹在 `generation_span()` 中
-   工具调用中的每次函数调用都会被包裹在 `function_span()` 中
-   安全防护措施会被包裹在 `guardrail_span()` 中
-   任务转移会被包裹在 `handoff_span()` 中
-   音频输入（speech-to-text）会被包裹在 `transcription_span()` 中
-   音频输出（text-to-speech）会被包裹在 `speech_span()` 中
-   相关的音频 span 可能会作为子项挂在 `speech_group_span()` 下

默认情况下，trace 名称为 “Agent workflow”。如果你使用 `trace`，可以设置该名称；或者你也可以通过 [`RunConfig`][agents.run.RunConfig] 配置名称及其他属性。

此外，你还可以配置[自定义 trace 处理器](#custom-tracing-processors)，将 trace 推送到其他目标位置（作为替代或第二目标）。

## 更高层级的 traces

有时，你可能希望多次对 `run()` 的调用都属于同一个 trace。你可以通过用 `trace()` 包裹整个代码来实现。

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

1. 因为对 `Runner.run` 的两次调用都包裹在 `with trace()` 中，因此各次运行会成为整体 trace 的一部分，而不是创建两个 trace。

## 创建 traces

你可以使用 [`trace()`][agents.tracing.trace] 函数创建 trace。Trace 需要被启动和结束。你有两种选择：

1. **推荐**：将 trace 作为上下文管理器使用，即 `with trace(...) as my_trace`。这样会在正确的时间自动启动与结束该 trace。
2. 你也可以手动调用 [`trace.start()`][agents.tracing.Trace.start] 和 [`trace.finish()`][agents.tracing.Trace.finish]。

当前 trace 通过 Python 的 [`contextvar`](https://docs.python.org/3/library/contextvars.html) 进行跟踪。这意味着它会自动支持并发。如果你手动启动/结束 trace，则需要在 `start()`/`finish()` 中传入 `mark_as_current` 和 `reset_current` 来更新当前 trace。

## 创建 spans

你可以使用各种 [`*_span()`][agents.tracing.create] 方法创建 span。一般来说，你不需要手动创建 spans。我们提供了 [`custom_span()`][agents.tracing.custom_span] 函数，用于追踪自定义 span 信息。

Spans 会自动属于当前 trace，并嵌套在最近的当前 span 之下；当前 span 同样通过 Python 的 [`contextvar`](https://docs.python.org/3/library/contextvars.html) 进行跟踪。

## 敏感数据

某些 span 可能会捕获潜在的敏感数据。

`generation_span()` 会存储 LLM 生成的输入/输出，`function_span()` 会存储函数调用的输入/输出。这些可能包含敏感数据，因此你可以通过 [`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] 禁用对这些数据的捕获。

同样，音频 span 默认包含对输入与输出音频的 base64 编码 PCM 数据。你可以通过配置 [`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] 来禁用对音频数据的捕获。

默认情况下，`trace_include_sensitive_data` 为 `True`。你可以在运行应用之前，通过导出 `OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA` 环境变量为 `true/1` 或 `false/0` 来在不改代码的情况下设置默认值。

## 自定义追踪处理器

追踪的高层架构如下：

-   在初始化时，我们创建一个全局 [`TraceProvider`][agents.tracing.setup.TraceProvider]，负责创建 trace。
-   我们为 `TraceProvider` 配置一个 [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor]，它会将 traces/spans 以批处理方式发送给 [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter]，后者会以批处理方式将 spans 与 traces 导出到 OpenAI 后端。

要自定义此默认设置，将 trace 发送到替代或额外的后端，或修改 exporter 行为，你有两种选择：

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] 允许你添加一个**额外的** trace 处理器，它会在 traces 与 spans 就绪时接收它们。这使你可以在将 trace 发送到 OpenAI 后端之外，额外执行自己的处理。
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] 允许你用自己的 trace 处理器**替换**默认处理器。这意味着除非你包含了一个会将 traces 发送到 OpenAI 后端的 `TracingProcessor`，否则 traces 将不会被发送到 OpenAI 后端。

## 使用非 OpenAI 模型进行追踪

你可以使用 OpenAI API key 搭配非 OpenAI 模型，在无需禁用追踪的情况下，在 OpenAI Traces 仪表板中启用免费的追踪功能。

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

如果你只需要为单次运行使用不同的追踪 key，请通过 `RunConfig` 传入，而不是修改全局 exporter。

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(tracing={"api_key": "sk-tracing-123"}),
)
```

## 说明
- 在 Openai Traces 仪表板中查看免费的 traces。

## 外部追踪处理器列表

-   [Weights & Biases](https://weave-docs.wandb.ai/guides/integrations/openai_agents)
-   [Arize-Phoenix](https://docs.arize.com/phoenix/tracing/integrations-tracing/openai-agents-sdk)
-   [Future AGI](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/openai_agents)
-   [MLflow（自托管/OSS）](https://mlflow.org/docs/latest/tracing/integrations/openai-agent)
-   [MLflow（Databricks 托管）](https://docs.databricks.com/aws/en/mlflow/mlflow-tracing#-automatic-tracing)
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