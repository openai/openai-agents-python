---
search:
  exclude: true
---
# 追踪

Agents SDK 内置了追踪功能，可收集智能体运行期间事件的完整记录：LLM 生成、工具调用、任务转移、安全防护措施，甚至包括发生的自定义事件。借助[Traces 仪表板](https://platform.openai.com/traces)，你可以在开发和生产环境中调试、可视化并监控你的工作流。

!!!note

    追踪默认启用。你可以通过以下三种常见方式禁用它：

    1. 你可以通过设置环境变量 `OPENAI_AGENTS_DISABLE_TRACING=1` 全局禁用追踪
    2. 你可以在代码中使用 [`set_tracing_disabled(True)`][agents.set_tracing_disabled] 全局禁用追踪
    3. 你可以通过将 [`agents.run.RunConfig.tracing_disabled`][] 设置为 `True` 来为单次运行禁用追踪

***对于在 Zero Data Retention (ZDR) 策略下使用 OpenAI API 的组织，追踪不可用。***

## Traces 和 spans

-   **Traces** 表示“工作流”的单个端到端操作。它们由 Span 组成。Traces 具有以下属性：
    -   `workflow_name`：这是逻辑工作流或应用。例如“代码生成”或“客户服务”。
    -   `trace_id`：Trace 的唯一 ID。如果你未传入，则会自动生成。格式必须为 `trace_<32_alphanumeric>`。
    -   `group_id`：可选的分组 ID，用于关联同一会话中的多个 trace。例如，你可以使用聊天线程 ID。
    -   `disabled`：如果为 True，则不会记录该 trace。
    -   `metadata`：trace 的可选元数据。
-   **Spans** 表示具有开始时间和结束时间的操作。Span 具有：
    -   `started_at` 和 `ended_at` 时间戳。
    -   `trace_id`，表示它们所属的 trace
    -   `parent_id`，指向该 Span 的父 Span（如果有）
    -   `span_data`，即有关该 Span 的信息。例如，`AgentSpanData` 包含有关 Agent 的信息，`GenerationSpanData` 包含有关 LLM 生成的信息，等等。

## 默认追踪

默认情况下，SDK 会追踪以下内容：

-   整个 `Runner.{run, run_sync, run_streamed}()` 都包装在 `trace()` 中。
-   每次智能体运行时，都会包装在 `agent_span()` 中
-   LLM 生成会包装在 `generation_span()` 中
-   每次工具调用都会分别包装在 `function_span()` 中
-   安全防护措施会包装在 `guardrail_span()` 中
-   任务转移会包装在 `handoff_span()` 中
-   音频输入（语音转文本）会包装在 `transcription_span()` 中
-   音频输出（文本转语音）会包装在 `speech_span()` 中
-   相关的音频 span 可能会作为 `speech_group_span()` 的子项

默认情况下，trace 名称为“Agent workflow”。如果你使用 `trace`，可以设置该名称；也可以使用 [`RunConfig`][agents.run.RunConfig] 配置名称和其他属性。

此外，你还可以设置[自定义追踪处理器](#custom-tracing-processors)，将 trace 推送到其他目标位置（作为替代目标或次级目标）。

## 长时间运行的 worker 与即时导出

默认的 [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] 会在后台每隔几秒导出一次 traces，
或者当内存队列达到其大小触发阈值时更快导出，
并且还会在进程退出时执行最终刷新。在 Celery、
RQ、Dramatiq 或 FastAPI 后台任务等长时间运行的 worker 中，这意味着 traces 通常会自动导出，
无需额外代码，但它们可能不会在每个作业
完成后立即出现在 Traces 仪表板中。

如果你需要在一个工作单元结束时立即投递的保证，请在
trace 上下文退出后调用 [`flush_traces()`][agents.tracing.flush_traces]。

```python
from agents import Runner, flush_traces, trace


@celery_app.task
def run_agent_task(prompt: str):
    try:
        with trace("celery_task"):
            result = Runner.run_sync(agent, prompt)
        return result.final_output
    finally:
        flush_traces()
```

```python
from fastapi import BackgroundTasks, FastAPI
from agents import Runner, flush_traces, trace

app = FastAPI()


def process_in_background(prompt: str) -> None:
    try:
        with trace("background_job"):
            Runner.run_sync(agent, prompt)
    finally:
        flush_traces()


@app.post("/run")
async def run(prompt: str, background_tasks: BackgroundTasks):
    background_tasks.add_task(process_in_background, prompt)
    return {"status": "queued"}
```

[`flush_traces()`][agents.tracing.flush_traces] 会阻塞，直到当前缓冲的 traces 和 spans
被导出，因此请在 `trace()` 关闭后调用它，以避免刷新尚未完全构建的 trace。若默认的
导出延迟可以接受，则可以跳过
此调用。

## 更高层级的 traces

有时，你可能希望多次调用 `run()` 属于同一个 trace。你可以通过将整个代码包装在 `trace()` 中来实现。

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

1. 因为这两次对 `Runner.run` 的调用被包装在 `with trace()` 中，所以这些单独的运行将成为整体 trace 的一部分，而不是创建两个 trace。

## 创建 traces

你可以使用 [`trace()`][agents.tracing.trace] 函数创建 trace。Trace 需要被启动和结束。你有两种方式：

1. **推荐**：将 trace 用作上下文管理器，即 `with trace(...) as my_trace`。这样会在正确的时间自动启动和结束 trace。
2. 你也可以手动调用 [`trace.start()`][agents.tracing.Trace.start] 和 [`trace.finish()`][agents.tracing.Trace.finish]。

当前 trace 通过 Python 的 [`contextvar`](https://docs.python.org/3/library/contextvars.html) 进行跟踪。这意味着它能够自动适配并发。如果你手动启动/结束 trace，则需要向 `start()`/`finish()` 传递 `mark_as_current` 和 `reset_current` 以更新当前 trace。

## 创建 spans

你可以使用各种 [`*_span()`][agents.tracing.create] 方法创建 span。通常，你不需要手动创建 span。也提供了 [`custom_span()`][agents.tracing.custom_span] 函数，用于跟踪自定义 span 信息。

Span 会自动归属于当前 trace，并嵌套在最近的当前 span 之下，而这个当前 span 是通过 Python 的 [`contextvar`](https://docs.python.org/3/library/contextvars.html) 进行跟踪的。

## 敏感数据

某些 span 可能会捕获潜在的敏感数据。

`generation_span()` 会存储 LLM 生成的输入/输出，而 `function_span()` 会存储函数调用的输入/输出。这些内容可能包含敏感数据，因此你可以通过 [`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] 禁用对这些数据的捕获。

同样，音频 span 默认会包含输入和输出音频的 base64 编码 PCM 数据。你可以通过配置 [`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] 来禁用对这些音频数据的捕获。

默认情况下，`trace_include_sensitive_data` 为 `True`。你也可以在不修改代码的情况下，通过在运行应用前将 `OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA` 环境变量导出为 `true/1` 或 `false/0` 来设置默认值。

## 自定义追踪处理器

追踪的高层架构如下：

-   初始化时，我们会创建一个全局的 [`TraceProvider`][agents.tracing.setup.TraceProvider]，它负责创建 traces。
-   我们会为 `TraceProvider` 配置一个 [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor]，它会将 traces/spans 分批发送给 [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter]，后者再将 spans 和 traces 分批导出到 OpenAI 后端。

若要自定义这一默认设置，将 traces 发送到替代或附加后端，或修改导出器行为，你有两个选项：

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] 允许你添加一个**额外的**追踪处理器，它会在 traces 和 spans 就绪时接收它们。这样你就可以在将 traces 发送到 OpenAI 后端之外，执行自己的处理。
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] 允许你用自己的追踪处理器**替换**默认处理器。这意味着 traces 不会发送到 OpenAI 后端，除非你包含一个会执行该操作的 `TracingProcessor`。


## 非 OpenAI 模型的追踪

你可以将 OpenAI API key 与非 OpenAI 模型一起使用，从而在无需禁用追踪的情况下，于 OpenAI Traces 仪表板中启用免费追踪。有关适配器选择和设置注意事项，请参阅 Models 指南中的[第三方适配器](models/index.md#third-party-adapters)部分。

```python
import os
from agents import set_tracing_export_api_key, Agent, Runner
from agents.extensions.models.any_llm_model import AnyLLMModel

tracing_api_key = os.environ["OPENAI_API_KEY"]
set_tracing_export_api_key(tracing_api_key)

model = AnyLLMModel(
    model="your-provider/your-model-name",
    api_key="your-api-key",
)

agent = Agent(
    name="Assistant",
    model=model,
)
```

如果你只需要为单次运行使用不同的追踪 key，请通过 `RunConfig` 传递，而不是更改全局导出器。

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(tracing={"api_key": "sk-tracing-123"}),
)
```

## 附加说明
- 在 Openai Traces 仪表板查看免费 traces。


## 生态系统集成

以下社区和供应商集成支持 OpenAI Agents SDK 的追踪接口。

### 外部追踪处理器列表

-   [Weights & Biases](https://weave-docs.wandb.ai/guides/integrations/openai_agents)
-   [Arize-Phoenix](https://docs.arize.com/phoenix/tracing/integrations-tracing/openai-agents-sdk)
-   [Future AGI](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/openai_agents)
-   [MLflow（自托管/OSS）](https://mlflow.org/docs/latest/tracing/integrations/openai-agent)
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
-   [HoneyHive](https://docs.honeyhive.ai/v2/integrations/openai-agents)
-   [Asqav](https://www.asqav.com/docs/integrations#openai-agents)
-   [Datadog](https://docs.datadoghq.com/llm_observability/instrumentation/auto_instrumentation/?tab=python#openai-agents)