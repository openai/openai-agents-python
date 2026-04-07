---
search:
  exclude: true
---
# 追踪

Agents SDK 包含内置追踪，可在智能体运行期间收集完整的事件记录：LLM 生成、工具调用、任务转移、安全防护措施，甚至发生的自定义事件。使用[Traces 控制台](https://platform.openai.com/traces)，你可以在开发和生产中调试、可视化并监控工作流。

!!!note

    默认启用追踪。你可以通过三种常见方式禁用它：

    1. 你可以通过设置环境变量 `OPENAI_AGENTS_DISABLE_TRACING=1` 全局禁用追踪
    2. 你可以在代码中使用 [`set_tracing_disabled(True)`][agents.set_tracing_disabled] 全局禁用追踪
    3. 你可以通过将 [`agents.run.RunConfig.tracing_disabled`][] 设为 `True` 来禁用单次运行的追踪

***对于在 OpenAI API 下使用零数据保留（ZDR）策略的组织，追踪不可用。***

## Traces 与 spans

-   **Traces** 表示“工作流”的一次端到端操作。它由 Span 组成。Traces 具有以下属性：
    -   `workflow_name`：这是逻辑工作流或应用。例如“代码生成”或“客户服务”。
    -   `trace_id`：trace 的唯一 ID。如果你未传入，会自动生成。格式必须为 `trace_<32_alphanumeric>`。
    -   `group_id`：可选的组 ID，用于关联同一会话中的多个 traces。例如，你可以使用聊天线程 ID。
    -   `disabled`：若为 True，则不会记录该 trace。
    -   `metadata`：trace 的可选元数据。
-   **Spans** 表示具有开始和结束时间的操作。Spans 具有：
    -   `started_at` 和 `ended_at` 时间戳。
    -   `trace_id`，表示其所属的 trace
    -   `parent_id`，指向该 Span 的父 Span（如有）
    -   `span_data`，即该 Span 的信息。例如，`AgentSpanData` 包含智能体信息，`GenerationSpanData` 包含 LLM 生成信息等。

## 默认追踪

默认情况下，SDK 会追踪以下内容：

-   整个 `Runner.{run, run_sync, run_streamed}()` 都包裹在 `trace()` 中。
-   每次智能体运行都包裹在 `agent_span()` 中
-   LLM 生成包裹在 `generation_span()` 中
-   每次工具调用都分别包裹在 `function_span()` 中
-   安全防护措施包裹在 `guardrail_span()` 中
-   任务转移包裹在 `handoff_span()` 中
-   音频输入（语音转文本）包裹在 `transcription_span()` 中
-   音频输出（文本转语音）包裹在 `speech_span()` 中
-   相关音频 spans 可能作为 `speech_group_span()` 的子级

默认情况下，trace 名称为“Agent workflow”。如果你使用 `trace`，可以设置该名称；你也可以通过 [`RunConfig`][agents.run.RunConfig] 配置名称和其他属性。

此外，你可以设置[自定义追踪进程](#custom-tracing-processors)，将 traces 推送到其他目的地（作为替代或次要目的地）。

## 长时间运行的 worker 与即时导出

默认的 [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] 会在后台每隔几秒导出一次 traces，
或者在内存队列达到大小触发条件时更早导出，
并且在进程退出时执行最终刷新。在 Celery、
RQ、Dramatiq 或 FastAPI 后台任务等长时间运行的 worker 中，这意味着 traces 通常会自动导出，
无需额外代码，但它们可能不会在每个任务结束后立即出现在 Traces 控制台中。

如果你需要在一个工作单元结束时立即交付的保证，请在
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
导出完成，因此请在 `trace()` 关闭后调用，以避免刷新尚未完整构建的 trace。若可接受
默认导出延迟，则可跳过此调用。

## 高层级 traces

有时你可能希望多次 `run()` 调用属于同一个 trace。你可以通过将整段代码包裹在 `trace()` 中来实现。

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

1. 由于两次 `Runner.run` 调用都包裹在 `with trace()` 中，单次运行将成为整体 trace 的一部分，而不是创建两个 trace。

## 创建 traces

你可以使用 [`trace()`][agents.tracing.trace] 函数来创建 trace。trace 需要被启动和结束。你有两个选项：

1. **推荐**：将 trace 用作上下文管理器，即 `with trace(...) as my_trace`。这会在正确时间自动启动并结束 trace。
2. 你也可以手动调用 [`trace.start()`][agents.tracing.Trace.start] 和 [`trace.finish()`][agents.tracing.Trace.finish]。

当前 trace 通过 Python 的 [`contextvar`](https://docs.python.org/3/library/contextvars.html) 跟踪。这意味着它可自动适配并发。若你手动启动/结束 trace，则需要向 `start()`/`finish()` 传入 `mark_as_current` 和 `reset_current` 来更新当前 trace。

## 创建 spans

你可以使用各种 [`*_span()`][agents.tracing.create] 方法来创建 span。通常，你不需要手动创建 spans。也提供了 [`custom_span()`][agents.tracing.custom_span] 函数来跟踪自定义 span 信息。

Spans 会自动归属于当前 trace，并嵌套在最近的当前 span 下，而该状态通过 Python 的 [`contextvar`](https://docs.python.org/3/library/contextvars.html) 跟踪。

## 敏感数据

某些 spans 可能会捕获潜在敏感数据。

`generation_span()` 会存储 LLM 生成的输入/输出，`function_span()` 会存储函数调用的输入/输出。这些内容可能包含敏感数据，因此你可以通过 [`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] 禁用这些数据的捕获。

类似地，音频 spans 默认包含输入与输出音频的 base64 编码 PCM 数据。你可以通过配置 [`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] 禁用这些音频数据的捕获。

默认情况下，`trace_include_sensitive_data` 为 `True`。你可以在运行应用前通过导出 `OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA` 环境变量并设为 `true/1` 或 `false/0` 来在无代码情况下设置默认值。

## 自定义追踪进程

追踪的高层架构为：

-   初始化时，我们会创建全局 [`TraceProvider`][agents.tracing.setup.TraceProvider]，其负责创建 traces。
-   我们将 `TraceProvider` 配置为使用 [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor]，该进程会将 traces/spans 批量发送到 [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter]，后者再将 spans 和 traces 批量导出到 OpenAI 后端。

要自定义此默认设置，以将 traces 发送到替代或附加后端，或修改导出器行为，你有两个选项：

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] 允许你添加**额外的**追踪进程，它会在 traces 和 spans 就绪时接收它们。这使你可以在发送到 OpenAI 后端之外执行自己的处理。
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] 允许你用自己的追踪进程**替换**默认进程。这意味着除非你包含一个会这样做的 `TracingProcessor`，否则 traces 不会发送到 OpenAI 后端。


## 使用非 OpenAI 模型进行追踪

你可以将 OpenAI API key 与非 OpenAI 模型一起使用，以在 OpenAI Traces 控制台中启用免费追踪，而无需禁用追踪。有关适配器选择和设置注意事项，请参阅 Models 指南中的[第三方适配器](models/index.md#third-party-adapters)部分。

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

如果你仅需为单次运行使用不同的追踪密钥，请通过 `RunConfig` 传入，而不是更改全局导出器。

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(tracing={"api_key": "sk-tracing-123"}),
)
```

## 附加说明
- 在 Openai Traces 控制台查看免费 traces。


## 生态系统集成

以下社区与供应商集成支持 OpenAI Agents SDK 追踪接口。

### 外部追踪进程列表

-   [Weights & Biases](https://weave-docs.wandb.ai/guides/integrations/openai_agents)
-   [Arize-Phoenix](https://docs.arize.com/phoenix/tracing/integrations-tracing/openai-agents-sdk)
-   [Future AGI](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/openai_agents)
-   [MLflow（self-hosted/OSS）](https://mlflow.org/docs/latest/tracing/integrations/openai-agent)
-   [MLflow（Databricks hosted）](https://docs.databricks.com/aws/en/mlflow/mlflow-tracing#-automatic-tracing)
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