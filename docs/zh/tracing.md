---
search:
  exclude: true
---
# 追踪

Agents SDK 内置了追踪功能，可在智能体运行期间收集全面的事件记录：LLM 生成、工具调用、任务转移、安全防护措施，以及发生的自定义事件。使用 [Traces 仪表盘](https://platform.openai.com/traces)，你可以在开发与生产环境中调试、可视化并监控工作流。

!!!note

    追踪默认启用。可以通过两种方式禁用追踪：

    1. 通过设置环境变量 `OPENAI_AGENTS_DISABLE_TRACING=1` 全局禁用追踪
    2. 通过将 [`agents.run.RunConfig.tracing_disabled`][] 设置为 `True`，仅为单次运行禁用追踪

***对于使用 OpenAI API 且遵循零数据保留（Zero Data Retention，ZDR）政策的组织，追踪不可用。***

## 追踪与跨度

-   **Traces（追踪）** 表示一个“工作流”的单次端到端操作。它由 Spans 组成。追踪具有以下属性：
    -   `workflow_name`：逻辑上的工作流或应用。例如 “Code generation” 或 “Customer service”。
    -   `trace_id`：追踪的唯一 ID。若未传入，将自动生成。必须符合 `trace_<32_alphanumeric>` 格式。
    -   `group_id`：可选的分组 ID，用于将同一会话中的多个追踪关联起来。例如，你可以使用聊天线程 ID。
    -   `disabled`：若为 True，则不会记录该追踪。
    -   `metadata`：追踪的可选元数据。
-   **Spans（跨度）** 表示具有起止时间的操作。跨度具有：
    -   `started_at` 和 `ended_at` 时间戳。
    -   `trace_id`，表示其所属追踪
    -   `parent_id`，指向该跨度的父跨度（如果有）
    -   `span_data`，即跨度的信息。例如，`AgentSpanData` 包含智能体相关信息，`GenerationSpanData` 包含 LLM 生成相关信息，等等。

## 默认追踪

默认情况下，SDK 会追踪以下内容：

-   整个 `Runner.{run, run_sync, run_streamed}()` 被包裹在 `trace()` 中。
-   每次智能体运行，都会被包裹在 `agent_span()` 中
-   LLM 生成被包裹在 `generation_span()` 中
-   工具调用分别被包裹在 `function_span()` 中
-   安全防护措施被包裹在 `guardrail_span()` 中
-   任务转移被包裹在 `handoff_span()` 中
-   音频输入（语音转文本）被包裹在 `transcription_span()` 中
-   音频输出（文本转语音）被包裹在 `speech_span()` 中
-   相关的音频跨度可能会作为 `speech_group_span()` 的子级

默认情况下，追踪名为 “Agent workflow”。你可以使用 `trace` 设置此名称，或通过 [`RunConfig`][agents.run.RunConfig] 配置名称和其他属性。

此外，你可以设置[自定义追踪进程](#custom-tracing-processors)，将追踪推送到其他目的地（替代或作为次级目的地）。

## 更高层级的追踪

有时，你可能希望多次调用 `run()` 属于同一个追踪。你可以通过将整段代码包裹在 `trace()` 中实现。

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

1. 因为两次对 `Runner.run` 的调用都包裹在 `with trace()` 中，这些独立运行将归属于一个整体追踪，而不是创建两个追踪。

## 创建追踪

你可以使用 [`trace()`][agents.tracing.trace] 函数来创建追踪。追踪需要启动与结束。你有两种方式：

1. 推荐：将追踪作为上下文管理器使用，即 `with trace(...) as my_trace`。这样会在合适的时机自动开始与结束追踪。
2. 你也可以手动调用 [`trace.start()`][agents.tracing.Trace.start] 和 [`trace.finish()`][agents.tracing.Trace.finish]。

当前追踪通过 Python 的 [`contextvar`](https://docs.python.org/3/library/contextvars.html) 进行跟踪。这意味着它能自动适配并发。如果你手动开始/结束追踪，你需要在 `start()`/`finish()` 时传入 `mark_as_current` 和 `reset_current` 来更新当前追踪。

## 创建跨度

你可以使用各类 [`*_span()`][agents.tracing.create] 方法创建跨度。通常你不需要手动创建跨度。可使用 [`custom_span()`][agents.tracing.custom_span] 函数来追踪自定义跨度信息。

跨度会自动归属于当前追踪，并嵌套在最近的当前跨度之下，当前跨度同样通过 Python 的 [`contextvar`](https://docs.python.org/3/library/contextvars.html) 进行跟踪。

## 敏感数据

某些跨度可能会捕获潜在的敏感数据。

`generation_span()` 会存储 LLM 生成的输入/输出，`function_span()` 会存储工具调用的输入/输出。这些可能包含敏感数据，因此你可以通过 [`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] 禁用此类数据的采集。

类似地，音频跨度默认会包含输入与输出音频的 base64 编码 PCM 数据。你可以通过配置 [`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] 禁用音频数据的采集。

默认情况下，`trace_include_sensitive_data` 为 `True`。你可以在运行应用前，通过设置环境变量 `OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA` 为 `true/1` 或 `false/0`，在无需修改代码的情况下设置默认值。

## 自定义追踪进程

追踪的高层架构为：

-   在初始化时，我们创建一个全局的 [`TraceProvider`][agents.tracing.setup.TraceProvider]，负责创建追踪。
-   我们使用 [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] 配置 `TraceProvider`，以批量将追踪/跨度发送到 [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter]，由其将跨度与追踪批量导出到 OpenAI 后端。

若要自定义此默认设置，将追踪发送到备用或附加后端，或修改导出器行为，你有两种选择：

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] 允许你添加“额外的”追踪进程，该进程会在追踪与跨度就绪时接收它们。这样你可以在将追踪发送到 OpenAI 后端之外，执行自定义处理。
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] 允许你“替换”默认的进程为你自己的追踪进程。除非你包含会发送到 OpenAI 后端的 `TracingProcessor`，否则追踪将不会发送到 OpenAI 后端。

## 使用非 OpenAI 模型的追踪

你可以使用 OpenAI API key 搭配非 OpenAI 模型，在无需禁用追踪的情况下，在 OpenAI Traces 仪表盘启用免费的追踪。

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

如果你只需要为单次运行使用不同的追踪 key，请通过 `RunConfig` 传入，而不是修改全局导出器。

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(tracing={"api_key": "sk-tracing-123"}),
)
```

## 备注
- 可在 OpenAI Traces 仪表盘查看免费追踪。

## 外部追踪进程列表

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