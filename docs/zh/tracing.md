---
search:
  exclude: true
---
# 追踪

Agents SDK 内置了追踪功能，可收集智能体运行期间事件的完整记录：LLM 生成、工具调用、任务转移、安全防护措施，甚至包括发生的自定义事件。通过使用[追踪仪表盘](https://platform.openai.com/traces)，你可以在开发和生产环境中调试、可视化并监控工作流。

!!!note

    追踪默认启用。你可以通过以下三种常见方式将其禁用：

    1. 你可以通过设置环境变量 `OPENAI_AGENTS_DISABLE_TRACING=1` 全局禁用追踪
    2. 你可以在代码中通过 [`set_tracing_disabled(True)`][agents.set_tracing_disabled] 全局禁用追踪
    3. 你可以通过将 [`agents.run.RunConfig.tracing_disabled`][] 设为 `True` 来为单次运行禁用追踪

***对于在使用 OpenAI API 时采用零数据保留（ZDR）策略的组织，追踪功能不可用。***

## 追踪与跨度

-   **追踪**表示“工作流”的一次端到端操作。它由多个跨度组成。追踪具有以下属性：
    -   `workflow_name`：逻辑工作流或应用。例如“代码生成”或“客户服务”。
    -   `trace_id`：追踪的唯一 ID。如果你未传入则会自动生成。格式必须为 `trace_<32_alphanumeric>`。
    -   `group_id`：可选分组 ID，用于关联同一会话中的多个追踪。例如，你可以使用聊天线程 ID。
    -   `disabled`：若为 True，则不会记录该追踪。
    -   `metadata`：追踪的可选元数据。
-   **跨度**表示具有开始和结束时间的操作。跨度具有：
    -   `started_at` 和 `ended_at` 时间戳。
    -   `trace_id`，表示其所属追踪
    -   `parent_id`，指向该跨度的父跨度（如有）
    -   `span_data`，即跨度信息。例如，`AgentSpanData` 包含智能体信息，`GenerationSpanData` 包含 LLM 生成信息，等等。

## 默认追踪

默认情况下，SDK 会追踪以下内容：

-   整个 `Runner.{run, run_sync, run_streamed}()` 会被包装在 `trace()` 中。
-   每次智能体运行时，都会包装在 `agent_span()` 中
-   LLM 生成会包装在 `generation_span()` 中
-   每次函数工具调用都会分别包装在 `function_span()` 中
-   安全防护措施会包装在 `guardrail_span()` 中
-   任务转移会包装在 `handoff_span()` 中
-   音频输入（语音转文本）会包装在 `transcription_span()` 中
-   音频输出（文本转语音）会包装在 `speech_span()` 中
-   相关音频跨度可能会作为 `speech_group_span()` 的子级

默认情况下，追踪名称为“Agent workflow”。如果你使用 `trace`，可以设置此名称；你也可以通过 [`RunConfig`][agents.run.RunConfig] 配置名称和其他属性。

此外，你还可以设置[自定义追踪处理器](#custom-tracing-processors)，将追踪推送到其他目标（作为替代目标或次要目标）。

## 更高层级追踪

有时，你可能希望多次调用 `run()` 都属于同一条追踪。你可以通过将整段代码包裹在 `trace()` 中来实现。

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

1. 由于对 `Runner.run` 的两次调用都被包裹在 `with trace()` 中，因此这些单独运行会成为整体追踪的一部分，而不是创建两条追踪。

## 创建追踪

你可以使用 [`trace()`][agents.tracing.trace] 函数创建追踪。追踪需要被启动和结束。你有两种方式：

1. **推荐**：将 trace 用作上下文管理器，即 `with trace(...) as my_trace`。这样会在正确时间自动启动并结束追踪。
2. 你也可以手动调用 [`trace.start()`][agents.tracing.Trace.start] 和 [`trace.finish()`][agents.tracing.Trace.finish]。

当前追踪通过 Python 的 [`contextvar`](https://docs.python.org/3/library/contextvars.html) 跟踪。这意味着它可自动适配并发。如果你手动启动/结束追踪，则需要向 `start()`/`finish()` 传递 `mark_as_current` 和 `reset_current` 来更新当前追踪。

## 创建跨度

你可以使用各种 [`*_span()`][agents.tracing.create] 方法创建跨度。通常你无需手动创建跨度。可使用 [`custom_span()`][agents.tracing.custom_span] 函数来追踪自定义跨度信息。

跨度会自动归属于当前追踪，并嵌套在最近的当前跨度下；这通过 Python 的 [`contextvar`](https://docs.python.org/3/library/contextvars.html) 进行跟踪。

## 敏感数据

某些跨度可能会捕获潜在敏感数据。

`generation_span()` 会存储 LLM 生成的输入/输出，`function_span()` 会存储函数调用的输入/输出。这些内容可能包含敏感数据，因此你可以通过 [`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] 禁用这类数据的捕获。

类似地，音频跨度默认包含输入和输出音频的 base64 编码 PCM 数据。你可以通过配置 [`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] 禁用该音频数据的捕获。

默认情况下，`trace_include_sensitive_data` 为 `True`。你也可以在不改代码的情况下，在运行应用前导出 `OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA` 环境变量并设为 `true/1` 或 `false/0` 来设置默认值。

## 自定义追踪处理器

追踪的高层架构如下：

-   在初始化时，我们会创建一个全局 [`TraceProvider`][agents.tracing.setup.TraceProvider]，负责创建追踪。
-   我们会为 `TraceProvider` 配置一个 [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor]，它会将追踪/跨度按批次发送到 [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter]，后者会将跨度和追踪按批次导出到 OpenAI 后端。

要自定义这套默认配置，以将追踪发送到替代或额外后端，或修改导出器行为，你有两个选项：

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] 允许你添加一个**额外**的追踪处理器，它会在追踪和跨度就绪时接收数据。这样你可以在将追踪发送到 OpenAI 后端之外执行自己的处理。
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] 允许你用自己的追踪处理器**替换**默认处理器。这意味着除非你包含一个执行该操作的 `TracingProcessor`，否则追踪不会发送到 OpenAI 后端。


## 使用非 OpenAI 模型进行追踪

你可以将 OpenAI API 密钥与非 OpenAI 模型一起使用，以在 OpenAI 追踪仪表盘中启用免费追踪，而无需禁用追踪。

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

如果你只需要为单次运行使用不同的追踪密钥，请通过 `RunConfig` 传入，而不是更改全局导出器。

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(tracing={"api_key": "sk-tracing-123"}),
)
```

## 附加说明
- 在 Openai 追踪仪表盘查看免费追踪。


## 生态系统集成

以下社区和供应商集成支持 OpenAI Agents SDK 的追踪能力。

### 外部追踪处理器列表

-   [Weights & Biases](https://weave-docs.wandb.ai/guides/integrations/openai_agents)
-   [Arize-Phoenix](https://docs.arize.com/phoenix/tracing/integrations-tracing/openai-agents-sdk)
-   [Future AGI](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/openai_agents)
-   [MLflow（自托管/开源）](https://mlflow.org/docs/latest/tracing/integrations/openai-agent)
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