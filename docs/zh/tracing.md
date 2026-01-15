---
search:
  exclude: true
---
# 追踪

Agents SDK 内置了追踪功能，会在智能体运行期间收集全面的事件记录：LLM 生成、工具调用、任务转移、安全防护措施，甚至自定义事件。使用 [Traces 仪表盘](https://platform.openai.com/traces)，你可以在开发和生产环境中调试、可视化并监控工作流。

!!!note

    追踪默认启用。可通过以下两种方式禁用追踪：

    1. 通过设置环境变量 `OPENAI_AGENTS_DISABLE_TRACING=1` 全局禁用追踪
    2. 可通过将 [`agents.run.RunConfig.tracing_disabled`][] 设为 `True` 来在单次运行中禁用追踪

***对于在使用 OpenAI API 的零数据保留（Zero Data Retention，ZDR）策略下运营的组织，追踪不可用。***

## 追踪与 Span

-   **追踪（Traces）** 代表一次“工作流”的端到端操作。它们由 Span 组成。追踪具有以下属性：
    -   `workflow_name`：逻辑上的工作流或应用。例如 “Code generation” 或 “Customer service”。
    -   `trace_id`：追踪的唯一 ID。如果未传入则自动生成。必须符合 `trace_<32_alphanumeric>` 格式。
    -   `group_id`：可选的分组 ID，用于关联同一会话中的多个追踪。例如，你可以使用聊天线程 ID。
    -   `disabled`：若为 True，则不会记录该追踪。
    -   `metadata`：追踪的可选元数据。
-   **Span** 表示一个有开始与结束时间的操作。Span 包含：
    -   `started_at` 和 `ended_at` 时间戳。
    -   `trace_id`，表示其所属的追踪
    -   `parent_id`，指向该 Span 的父级 Span（如果有）
    -   `span_data`，包含关于 Span 的信息。例如，`AgentSpanData` 包含智能体信息，`GenerationSpanData` 包含 LLM 生成的信息，等等。

## 默认追踪

默认情况下，SDK 会追踪以下内容：

-   整个 `Runner.{run, run_sync, run_streamed}()` 被包裹在 `trace()` 中。
-   每次智能体运行都会被包裹在 `agent_span()`
-   LLM 生成会被包裹在 `generation_span()`
-   工具调用会分别被包裹在 `function_span()`
-   安全防护措施会被包裹在 `guardrail_span()`
-   任务转移会被包裹在 `handoff_span()`
-   音频输入（语音转文本）会被包裹在 `transcription_span()`
-   音频输出（文本转语音）会被包裹在 `speech_span()`
-   相关音频 Span 可能被归为 `speech_group_span()` 的子级

默认情况下，追踪名称为 “Agent workflow”。如果你使用 `trace`，可以设置该名称；或者通过 [`RunConfig`][agents.run.RunConfig] 配置名称和其他属性。

此外，你可以设置[自定义追踪进程](#custom-tracing-processors)，将追踪数据推送到其他目的地（作为替代或附加目的地）。

## 更高层级的追踪

有时，你可能希望多次调用 `run()` 都属于同一个追踪。你可以通过将整段代码包裹在 `trace()` 中来实现。

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

1. 因为两次对 `Runner.run` 的调用被包裹在 `with trace()` 中，这些单独的运行会成为同一整体追踪的一部分，而不是创建两个独立的追踪。

## 创建追踪

你可以使用 [`trace()`][agents.tracing.trace] 函数创建一个追踪。追踪需要启动与结束。你有两种方式：

1. 推荐：将 trace 作为上下文管理器使用，即 `with trace(...) as my_trace`。这会在合适的时机自动开始和结束追踪。
2. 你也可以手动调用 [`trace.start()`][agents.tracing.Trace.start] 和 [`trace.finish()`][agents.tracing.Trace.finish]。

当前追踪通过 Python 的 [`contextvar`](https://docs.python.org/3/library/contextvars.html) 跟踪。这意味着它可自动适配并发场景。若你手动开始/结束追踪，需要在 `start()`/`finish()` 时传入 `mark_as_current` 和 `reset_current` 来更新当前追踪。

## 创建 Span

你可以使用各种 [`*_span()`][agents.tracing.create] 方法创建 Span。通常，你无需手动创建 Span。可使用 [`custom_span()`][agents.tracing.custom_span] 来记录自定义 Span 信息。

Span 会自动归属到当前追踪，并嵌套在最近的当前 Span 之下，该状态通过 Python 的 [`contextvar`](https://docs.python.org/3/library/contextvars.html) 进行跟踪。

## 敏感数据

某些 Span 可能会捕获潜在的敏感数据。

`generation_span()` 会存储 LLM 生成的输入/输出，而 `function_span()` 会存储函数调用的输入/输出。这些可能包含敏感数据，因此你可以通过 [`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] 禁用对这些数据的采集。

类似地，音频相关的 Span 默认会包含输入与输出音频的 base64 编码 PCM 数据。你可以通过配置 [`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] 禁用音频数据采集。

默认情况下，`trace_include_sensitive_data` 为 `True`。你也可以在不修改代码的情况下，通过在运行应用前导出环境变量 `OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA` 为 `true/1` 或 `false/0` 来设置默认值。

## 自定义追踪进程

追踪的高层架构如下：

-   在初始化时，我们会创建一个全局的 [`TraceProvider`][agents.tracing.setup.TraceProvider]，负责创建追踪。
-   我们为 `TraceProvider` 配置一个 [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor]，它会将追踪/Span 批量发送给 [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter]，后者再将 Span 与追踪批量导出到 OpenAI 后端。

若要自定义该默认设置，以便将追踪发送到替代或附加的后端，或修改导出器行为，你有两种选择：

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] 允许你添加一个“额外”的追踪进程，当追踪和 Span 就绪时会接收它们。这样你可以在将追踪发送到 OpenAI 后端之外，执行自定义处理。
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] 允许你“替换”默认的进程为你自己的追踪进程。这意味着除非你包含一个会将数据发送到 OpenAI 后端的 `TracingProcessor`，否则追踪将不会被发送至 OpenAI 后端。

## 在非 OpenAI 模型中的追踪

你可以将 OpenAI API key 与非 OpenAI 模型配合使用，在 OpenAI Traces 仪表盘中启用免费的追踪，而无需禁用追踪。

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

如果你只需要在单次运行中使用不同的追踪 key，可通过 `RunConfig` 传入，而不是修改全局导出器。

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(tracing={"api_key": "sk-tracing-123"}),
)
```

## 备注
- 在 OpenAI Traces 仪表盘查看免费追踪。

## 外部追踪进程清单

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