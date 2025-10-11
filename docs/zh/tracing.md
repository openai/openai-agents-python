---
search:
  exclude: true
---
# 追踪功能

Agents SDK包含内置的追踪功能，可全面记录智能体运行期间的事件：LLM生成、工具调用、交接、护栏，甚至发生的自定义事件。使用[追踪仪表板](https://platform.openai.com/traces)，你可以在开发和生产环境中调试、可视化和监控你的工作流程。

!!!note

    追踪功能默认启用。有两种方法可以禁用追踪：

    1. 你可以通过设置环境变量 `OPENAI_AGENTS_DISABLE_TRACING=1` 来全局禁用追踪
    2. 你可以通过将 [`agents.run.RunConfig.tracing_disabled`][] 设置为 `True` 来针对单次运行禁用追踪

***对于使用OpenAI API并在零数据保留(ZDR)策略下运营的组织，追踪功能不可用。***

## 追踪和跨度

-   **追踪** 代表"工作流"的单个端到端操作。它们由跨度组成。追踪具有以下属性:
    -   `workflow_name`: 这是逻辑工作流或应用。例如"代码生成"或"客户服务"。
    -   `trace_id`: 追踪的唯一ID。如果你没有传递一个，它会自动生成。格式必须是 `trace_<32_字母数字>`。
    -   `group_id`: 可选的组ID，用于链接来自同一会话的多个追踪。例如，你可以使用聊天线程ID。
    -   `disabled`: 如果为True，该追踪将不会被记录。
    -   `metadata`: 追踪的可选元数据。
-   **跨度** 代表具有开始和结束时间的操作。跨度具有:
    -   `started_at` 和 `ended_at` 时间戳。
    -   `trace_id`，表示它们所属的追踪
    -   `parent_id`，指向此跨度的父跨度（如果有）
    -   `span_data`，这是关于跨度的信息。例如，`AgentSpanData` 包含关于智能体的信息，`GenerationSpanData` 包含关于LLM生成的信息等。

## 默认追踪

默认情况下，SDK会追踪以下内容：

-   整个 `Runner.{run, run_sync, run_streamed}()` 调用被 `trace()` 包装。
-   每次智能体执行时，都会被 `agent_span()` 包装
-   LLM生成被 `generation_span()` 包装
-   函数工具的调用分别被 `function_span()` 包装
-   护栏被 `guardrail_span()` 包装
-   交接被 `handoff_span()` 包装
-   语音输入（语音识别）被 `transcription_span()` 包装
-   语音输出（语音合成）被 `speech_span()` 包装
-   相关的语音跨度可能成为 `speech_group_span()` 的子跨度

默认情况下，追踪名称为"Agent workflow"。你可以在使用 `trace` 时设置此名称，也可以在 [`RunConfig`][agents.run.RunConfig] 中设置名称和其他属性。

此外，你可以配置[自定义追踪处理器](#自定义追踪处理器)来将追踪输出到其他目标（作为替代或附加目标）。

## 高层级追踪

有时你可能想将多次 `run()` 调用合并到一个追踪中。要做到这一点，可以用 `trace()` 包装整个代码块。

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

1. 由于对 `Runner.run` 的两次调用都被 `with trace()` 包装，各个执行不会创建两个追踪，而是成为整体追踪的一部分。

## 创建追踪

你可以使用 [`trace()`][agents.tracing.trace] 函数创建追踪。追踪需要开始和结束，有两种方法：

1. 推荐：将追踪作为上下文管理器使用（例如：`with trace(...) as my_trace`）。这会在适当的时候自动开始和结束。
2. 也可以手动调用 [`trace.start()`][agents.tracing.Trace.start] 和 [`trace.finish()`][agents.tracing.Trace.finish]。

当前追踪由Python的 [`contextvar`](https://docs.python.org/3/library/contextvars.html) 跟踪。这意味着它在并发处理中自动工作。如果手动开始/结束追踪，需要向 `start()`/`finish()` 传递 `mark_as_current` 和 `reset_current` 来更新当前追踪。

## 创建跨度

你可以使用各种 [`*_span()`][agents.tracing.create] 方法创建跨度。通常不需要手动创建跨度。要追踪自定义跨度信息，可以使用 [`custom_span()`][agents.tracing.custom_span] 函数。

跨度会自动成为当前追踪的一部分，并嵌套在由Python的 [`contextvar`](https://docs.python.org/3/library/contextvars.html) 跟踪的最近的当前跨度之下。

## 敏感数据

某些跨度可能会捕获敏感数据。

`generation_span()` 保存LLM生成的输入/输出，`function_span()` 保存函数调用的输入/输出。这些可能包含敏感数据，因此可以通过 [`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] 禁用其捕获。

类似地，语音跨度默认包含输入/输出音频的base64编码PCM数据。可以设置 [`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] 来禁用此音频数据的捕获。

## 自定义追踪处理器

追踪的高层级架构如下：

-   初始化时，创建一个全局的 [`TraceProvider`][agents.tracing.setup.TraceProvider]，负责创建追踪。
-   为 `TraceProvider` 设置一个 [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor]，它将跨度/追踪批量发送到 [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter]。`BackendSpanExporter` 将其批量导出到OpenAI后端。

要自定义默认设置，发送到其他后端/复制到额外后端/更改导出器行为，有两种方法：

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] 可以添加**额外的**追踪处理器，一旦追踪和跨度准备就绪就会接收它们。这允许你在发送到OpenAI后端的同时执行自己的处理。
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] 可以用自己的追踪处理器**替换**默认处理器。要将追踪发送到OpenAI后端，需要包含执行该操作的 `TracingProcessor`。

## 非OpenAI模型的追踪

使用OpenAI的API密钥配合非OpenAI模型时，可以在不禁用追踪的情况下启用OpenAI追踪仪表板中的免费追踪。

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

## 注意事项
- 免费追踪可以在OpenAI追踪仪表板中查看。

## 外部追踪处理器列表

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
