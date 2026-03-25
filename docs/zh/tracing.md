---
search:
  exclude: true
---
# 追踪

Agents SDK 内置了追踪功能，可在智能体运行期间收集完整的事件记录：LLM 生成、工具调用、任务转移、安全防护措施，甚至发生的自定义事件。使用[Traces 仪表板](https://platform.openai.com/traces)，你可以在开发和生产中调试、可视化并监控你的工作流。

!!!note

    默认启用追踪。你可以通过三种常见方式禁用它：

    1. 你可以通过设置环境变量 `OPENAI_AGENTS_DISABLE_TRACING=1` 全局禁用追踪
    2. 你可以在代码中通过 [`set_tracing_disabled(True)`][agents.set_tracing_disabled] 全局禁用追踪
    3. 你可以通过将 [`agents.run.RunConfig.tracing_disabled`][] 设为 `True` 来禁用单次运行的追踪

***对于在 OpenAI API 下使用零数据保留（ZDR）策略的组织，追踪不可用。***

## Trace 与 Span

-   **Traces** 表示一个“工作流”的单次端到端操作。它们由 Spans 组成。Traces 具有以下属性：
    -   `workflow_name`：逻辑工作流或应用名称。例如“代码生成”或“客户服务”。
    -   `trace_id`：Trace 的唯一 ID。如果你不传入会自动生成。格式必须为 `trace_<32_alphanumeric>`。
    -   `group_id`：可选分组 ID，用于关联同一会话中的多个 Trace。例如，你可以使用聊天线程 ID。
    -   `disabled`：若为 True，则不会记录该 Trace。
    -   `metadata`：Trace 的可选元数据。
-   **Spans** 表示具有开始和结束时间的操作。Span 具有：
    -   `started_at` 和 `ended_at` 时间戳。
    -   `trace_id`，表示它们所属的 Trace
    -   `parent_id`，指向该 Span 的父 Span（如果有）
    -   `span_data`，即关于 Span 的信息。例如，`AgentSpanData` 包含智能体信息，`GenerationSpanData` 包含 LLM 生成信息，等等。

## 默认追踪

默认情况下，SDK 会追踪以下内容：

-   整个 `Runner.{run, run_sync, run_streamed}()` 会包裹在 `trace()` 中。
-   每次智能体运行都会包裹在 `agent_span()` 中
-   LLM 生成会包裹在 `generation_span()` 中
-   每次函数工具调用都会包裹在 `function_span()` 中
-   安全防护措施会包裹在 `guardrail_span()` 中
-   任务转移会包裹在 `handoff_span()` 中
-   音频输入（语音转文本）会包裹在 `transcription_span()` 中
-   音频输出（文本转语音）会包裹在 `speech_span()` 中
-   相关音频 Span 可能会作为子级归入 `speech_group_span()` 下

默认情况下，Trace 名称为“Agent workflow”。如果你使用 `trace`，可以设置此名称；也可以通过 [`RunConfig`][agents.run.RunConfig] 配置名称和其他属性。

此外，你还可以设置[自定义追踪进程](#custom-tracing-processors)，将 Trace 推送到其他目标（作为替代或辅助目标）。

## 更高层级的 Trace

有时，你可能希望多次对 `run()` 的调用都属于同一个 Trace。你可以通过将整段代码包裹在 `trace()` 中来实现。

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

1. 因为两次对 `Runner.run` 的调用都包裹在 `with trace()` 中，所以这些单独运行会属于同一个总体 Trace，而不是创建两个 Trace。

## 创建 Trace

你可以使用 [`trace()`][agents.tracing.trace] 函数来创建 Trace。Trace 需要开始和结束。你有两种方式：

1. **推荐**：将 trace 用作上下文管理器，即 `with trace(...) as my_trace`。这会在正确的时间自动开始和结束 Trace。
2. 你也可以手动调用 [`trace.start()`][agents.tracing.Trace.start] 和 [`trace.finish()`][agents.tracing.Trace.finish]。

当前 Trace 通过 Python 的 [`contextvar`](https://docs.python.org/3/library/contextvars.html) 跟踪。这意味着它可自动适配并发。如果你手动开始/结束 Trace，需要向 `start()`/`finish()` 传递 `mark_as_current` 和 `reset_current` 来更新当前 Trace。

## 创建 Span

你可以使用各种 [`*_span()`][agents.tracing.create] 方法来创建 Span。通常你不需要手动创建 Span。可使用 [`custom_span()`][agents.tracing.custom_span] 函数来跟踪自定义 Span 信息。

Span 会自动归属于当前 Trace，并嵌套在最近的当前 Span 下；这同样通过 Python 的 [`contextvar`](https://docs.python.org/3/library/contextvars.html) 跟踪。

## 敏感数据

某些 Span 可能会捕获潜在的敏感数据。

`generation_span()` 会存储 LLM 生成的输入/输出，`function_span()` 会存储函数调用的输入/输出。这些可能包含敏感数据，因此你可以通过 [`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] 禁用此类数据的捕获。

类似地，默认情况下，音频 Span 会包含输入和输出音频的 base64 编码 PCM 数据。你可以通过配置 [`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] 来禁用此音频数据的捕获。

默认情况下，`trace_include_sensitive_data` 为 `True`。你也可以在不改代码的情况下，通过在运行应用前导出 `OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA` 环境变量为 `true/1` 或 `false/0` 来设置默认值。

## 自定义追踪进程

追踪的高层架构为：

-   初始化时，我们会创建一个全局 [`TraceProvider`][agents.tracing.setup.TraceProvider]，负责创建 Trace。
-   我们使用 [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] 配置 `TraceProvider`，它会将 Trace/Span 批量发送到 [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter]，后者会将 Span 和 Trace 批量导出到 OpenAI 后端。

若要自定义此默认设置，将 Trace 发送到替代或附加后端，或修改导出器行为，你有两种选择：

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] 允许你添加一个**额外的**追踪进程，在 Trace 和 Span 就绪时接收它们。这使你可以在发送到 OpenAI 后端之外执行自己的处理。
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] 允许你用自己的追踪进程**替换**默认进程。这意味着除非你包含一个会执行该发送的 `TracingProcessor`，否则 Trace 不会发送到 OpenAI 后端。


## 使用非 OpenAI 模型进行追踪

你可以在非 OpenAI 模型中使用 OpenAI API key，在无需禁用追踪的情况下，于 OpenAI Traces 仪表板启用免费追踪。

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

如果你只需为单次运行使用不同的追踪 key，请通过 `RunConfig` 传入，而不是更改全局导出器。

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(tracing={"api_key": "sk-tracing-123"}),
)
```

## 附加说明
- 在 Openai Traces 仪表板查看免费 Trace。


## 生态系统集成

以下社区和供应商集成支持 OpenAI Agents SDK 追踪接口。

### 外部追踪进程列表

-   [Weights & Biases](https://weave-docs.wandb.ai/guides/integrations/openai_agents)
-   [Arize-Phoenix](https://docs.arize.com/phoenix/tracing/integrations-tracing/openai-agents-sdk)
-   [Future AGI](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/openai_agents)
-   [MLflow（self-hosted/OSS）](https://mlflow.org/docs/latest/tracing/integrations/openai-agent)
-   [MLflow（Databricks 托管）](https://docs.databricks.com/aws/en/mlflow/mlflow-tracing#-automatic-tracing)
-   [Braintrust](https://braintrust.dev/docs/guides/traces/integrations#openai-agents-sdk)
-   [Pydantic Logfire](https://logfire.pydantic.dev/docs/integrations/llms/openai/#openai-agents)
-   [AgentOps](https://docs.agentops.ai/v1/integrations/agentssdk)
-   [Scorecard](https://docs.scorecard.io/docs/documentation/features/tracing#openai-agents-sdk-integration)
-   [Respan](https://respan.ai/docs/integrations/tracing/openai-agents-sdk)
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
-   [PromptLayer](https://docs.promptlayer.com/languages/integrations#openai-agents-sdk)