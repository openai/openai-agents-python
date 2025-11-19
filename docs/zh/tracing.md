---
search:
  exclude: true
---
# 追踪

Agents SDK 内置了追踪功能，会在一次智能体运行中收集完整的事件记录：LLM 生成、工具调用、任务转移、安全防护措施，甚至自定义事件。使用 [Traces 仪表板](https://platform.openai.com/traces)，你可以在开发与生产环境中调试、可视化并监控工作流。

!!!note

    追踪默认启用。可以通过两种方式禁用追踪：

    1. 通过设置环境变量 `OPENAI_AGENTS_DISABLE_TRACING=1` 全局禁用追踪
    2. 通过将 [`agents.run.RunConfig.tracing_disabled`][] 设为 `True`，对单次运行禁用追踪

***对于在使用 OpenAI API 且采用 Zero Data Retention (ZDR) 策略的组织，不支持追踪。***

## Traces 与 spans

-   **Traces** 表示“工作流”的一次端到端操作。它们由多个 Span 组成。Trace 具有以下属性：
    -   `workflow_name`：逻辑上的工作流或应用名称。例如“代码生成”或“客户服务”。
    -   `trace_id`：Trace 的唯一 ID。如果未传入，将自动生成。必须符合 `trace_<32_alphanumeric>` 格式。
    -   `group_id`：可选的分组 ID，用于将同一会话中的多个 Trace 关联起来。例如，你可以使用聊天线程 ID。
    -   `disabled`：若为 True，则不会记录该 Trace。
    -   `metadata`：Trace 的可选元数据。
-   **Spans** 表示具有开始和结束时间的操作。Span 具有：
    -   `started_at` 和 `ended_at` 时间戳。
    -   `trace_id`，表示其所属的 Trace
    -   `parent_id`，指向该 Span 的父级 Span（如有）
    -   `span_data`，即关于 Span 的信息。例如，`AgentSpanData` 包含智能体信息，`GenerationSpanData` 包含 LLM 生成信息，等。

## 默认追踪

默认情况下，SDK 会追踪以下内容：

-   整个 `Runner.{run, run_sync, run_streamed}()` 被包裹在 `trace()` 中。
-   每次智能体运行都会包裹在 `agent_span()` 中
-   LLM 生成会包裹在 `generation_span()` 中
-   工具调用会分别包裹在 `function_span()` 中
-   安全防护措施会包裹在 `guardrail_span()` 中
-   任务转移会包裹在 `handoff_span()` 中
-   音频输入（语音转文本）会包裹在 `transcription_span()` 中
-   音频输出（文本转语音）会包裹在 `speech_span()` 中
-   相关音频 spans 可能会归入 `speech_group_span()` 之下

默认情况下，Trace 名称为 “Agent workflow”。如果使用 `trace`，你可以设置此名称；或者通过 [`RunConfig`][agents.run.RunConfig] 配置名称及其他属性。

此外，你可以设置[自定义追踪进程](#custom-tracing-processors)，将追踪数据推送到其他目的地（作为替代，或作为次要目的地）。

## 高层级追踪

有时你可能希望多次调用 `run()` 属于同一个 Trace。可以将整段代码包裹在 `trace()` 中实现。

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

1. 因为两次对 `Runner.run` 的调用都放在 `with trace()` 中，单次运行会成为整体 Trace 的组成部分，而不是创建两个独立的 Trace。

## 创建 traces

你可以使用 [`trace()`][agents.tracing.trace] 函数创建 Trace。Trace 需要开始与结束。你有两种方式：

1. 推荐：将 trace 作为上下文管理器使用，即 `with trace(...) as my_trace`。它会在合适的时机自动开始和结束 Trace。
2. 也可以手动调用 [`trace.start()`][agents.tracing.Trace.start] 和 [`trace.finish()`][agents.tracing.Trace.finish]。

当前 Trace 通过 Python 的 [`contextvar`](https://docs.python.org/3/library/contextvars.html) 进行跟踪。这意味着它会自动适配并发场景。如果你手动开始/结束 Trace，需要在 `start()`/`finish()` 中传入 `mark_as_current` 和 `reset_current` 来更新当前 Trace。

## 创建 spans

你可以使用各类 [`*_span()`][agents.tracing.create] 方法创建 Span。一般情况下，不需要手动创建 Spans。提供了 [`custom_span()`][agents.tracing.custom_span] 函数以追踪自定义 Span 信息。

Spans 会自动加入当前 Trace，并嵌套在最近的当前 Span 之下，而该信息通过 Python 的 [`contextvar`](https://docs.python.org/3/library/contextvars.html) 进行跟踪。

## 敏感数据

某些 Span 可能会捕获潜在的敏感数据。

`generation_span()` 会存储 LLM 生成的输入/输出，`function_span()` 会存储工具调用的输入/输出。这些可能包含敏感数据，因此你可以通过 [`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] 禁用这些数据的采集。

同样地，音频相关的 Spans 默认包含输入与输出音频的 base64 编码 PCM 数据。你可以通过配置 [`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] 来禁用这些音频数据的采集。

## 自定义追踪进程

追踪的高层架构如下：

-   初始化时，我们创建一个全局的 [`TraceProvider`][agents.tracing.setup.TraceProvider]，负责创建 Traces。
-   我们将 `TraceProvider` 配置为使用 [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] 批量发送 Traces/Spans 到 [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter]，后者会将 Spans 与 Traces 批量导出到 OpenAI 后端。

若要自定义此默认设置，以便将追踪发送到替代或附加后端，或修改导出器行为，你有两种选择：

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] 允许你添加“额外”的追踪进程，它会在 Traces 与 Spans 准备就绪时接收它们。这样你可以在将追踪发送到 OpenAI 后端之外，执行自定义处理。
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] 允许你“替换”默认的进程为你自定义的追踪进程。除非你包含一个负责发送到 OpenAI 后端的 `TracingProcessor`，否则 Traces 将不会发送到 OpenAI 后端。

## 使用非 OpenAI 模型进行追踪

你可以在非 OpenAI 模型中使用 OpenAI API key 启用在 OpenAI Traces 仪表板中的免费追踪，而无需禁用追踪。

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

## 备注
- 在 Openai Traces 仪表板查看免费追踪。

## 外部追踪进程列表

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