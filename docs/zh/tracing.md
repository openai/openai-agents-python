---
search:
  exclude: true
---
# 追踪

Agents SDK内置追踪功能，会在一次智能体运行期间收集全面的事件记录：LLM生成、工具调用、任务转移、安全防护措施，甚至发生的自定义事件。使用[Traces仪表板](https://platform.openai.com/traces)，你可以在开发和生产环境中调试、可视化并监控你的工作流。

!!!note

    追踪默认启用。你可以通过三种常见方式禁用它：

    1. 你可以通过设置环境变量`OPENAI_AGENTS_DISABLE_TRACING=1`全局禁用追踪
    2. 你可以在代码中使用[`set_tracing_disabled(True)`][agents.set_tracing_disabled]全局禁用追踪
    3. 你可以将[`agents.run.RunConfig.tracing_disabled`][]设置为`True`，为单次运行禁用追踪

***对于使用OpenAI API且遵循零数据保留（ZDR）政策的组织，追踪不可用。***

## 追踪和跨度

-   **追踪**表示“工作流”的单个端到端操作。它们由跨度组成。追踪具有以下属性：
    -   `workflow_name`：这是逻辑工作流或应用。例如“代码生成”或“客户服务”。
    -   `trace_id`：追踪的唯一ID。如果你未传入，则会自动生成。必须采用`trace_<32_alphanumeric>`格式。
    -   `group_id`：可选的组ID，用于关联来自同一对话的多个追踪。例如，你可以使用聊天线程ID。
    -   `disabled`：如果为True，则不会记录该追踪。
    -   `metadata`：追踪的可选元数据。
-   **跨度**表示具有开始和结束时间的操作。跨度具有：
    -   `started_at`和`ended_at`时间戳。
    -   `trace_id`，表示它们所属的追踪
    -   `parent_id`，指向此跨度的父级跨度（如有）
    -   `span_data`，即有关该跨度的信息。例如，`AgentSpanData`包含有关该智能体的信息，`GenerationSpanData`包含有关LLM生成的信息，等等。

## 默认追踪

默认情况下，SDK会追踪以下内容：

-   整个`Runner.{run, run_sync, run_streamed}()`都会被包装在`trace()`中。
-   每次智能体运行时，都会被包装在`agent_span()`中
-   LLM生成过程会被包装在`generation_span()`中
-   每次工具调用都会被包装在`function_span()`中
-   安全防护措施会被包装在`guardrail_span()`中
-   任务转移会被包装在`handoff_span()`中
-   音频输入（语音转文本）会被包装在`transcription_span()`中
-   音频输出（文本转语音）会被包装在`speech_span()`中
-   相关音频跨度可能以一个`speech_group_span()`为父级

默认情况下，追踪的名称为“Agent workflow”。如果使用`trace`，你可以设置此名称；也可以使用[`RunConfig`][agents.run.RunConfig]配置名称和其他属性。

此外，你可以设置[自定义追踪进程](#custom-tracing-processors)，以将追踪推送到其他目标位置（作为替代目标或次要目标）。

## 长时间运行的工作进程和即时导出

默认的[`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor]会导出追踪
在后台每隔几秒执行一次，或者当内存队列达到其大小触发阈值时更早执行，
并且还会在进程退出时执行最后一次刷新。在Celery、
RQ、Dramatiq或FastAPI后台任务等长时间运行的工作进程中，这意味着追踪通常会自动导出，
无需任何额外代码，但它们可能不会在每个作业
完成后立即出现在Traces仪表板中。

如果你需要在一个工作单元结束时获得即时交付保证，请在追踪上下文退出后调用
[`flush_traces()`][agents.tracing.flush_traces]。

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

[`flush_traces()`][agents.tracing.flush_traces]会阻塞，直到当前缓冲的追踪和跨度被
导出，因此请在`trace()`关闭后调用它，以避免刷新尚未构建完成的追踪。你可以在默认导出延迟可接受时跳过
此调用。

## 更高层级的追踪

有时，你可能希望对`run()`的多次调用成为同一个追踪的一部分。你可以通过将整个代码包装在`trace()`中来实现。

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

1. 由于对`Runner.run`的两次调用都被包装在`with trace()`中，单独的运行将成为整体追踪的一部分，而不是创建两个追踪。

## 追踪的创建

你可以使用[`trace()`][agents.tracing.trace]函数创建追踪。追踪需要启动并结束。有两种方式可以做到：

1. **推荐**：将追踪用作上下文管理器，即`with trace(...) as my_trace`。这会在正确的时间自动启动和结束追踪。
2. 你也可以手动调用[`trace.start()`][agents.tracing.Trace.start]和[`trace.finish()`][agents.tracing.Trace.finish]。

当前追踪通过Python的[`contextvar`](https://docs.python.org/3/library/contextvars.html)进行跟踪。这意味着它可以自动适配并发。如果你手动启动/结束追踪，需要将`mark_as_current`和`reset_current`传给`start()`/`finish()`，以更新当前追踪。

## 跨度的创建

你可以使用各种[`*_span()`][agents.tracing.create]方法创建跨度。一般来说，你不需要手动创建跨度。可使用[`custom_span()`][agents.tracing.custom_span]函数来跟踪自定义跨度信息。

跨度会自动成为当前追踪的一部分，并嵌套在最近的当前跨度之下；该当前跨度通过Python的[`contextvar`](https://docs.python.org/3/library/contextvars.html)进行跟踪。

## 敏感数据

某些跨度可能会捕获潜在敏感数据。

`generation_span()`会存储LLM生成的输入/输出，`function_span()`会存储函数调用的输入/输出。这些可能包含敏感数据，因此你可以通过[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data]禁用对这些数据的捕获。

同样，默认情况下，音频跨度会包含输入和输出音频的base64编码PCM数据。你可以通过配置[`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data]来禁用对这些音频数据的捕获。

默认情况下，`trace_include_sensitive_data`为`True`。你可以在运行应用之前将`OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA`环境变量导出为`true/1`或`false/0`，以在不编写代码的情况下设置默认值。

## 自定义追踪进程

追踪的高层架构如下：

-   初始化时，我们创建一个全局[`TraceProvider`][agents.tracing.setup.TraceProvider]，它负责创建追踪。
-   我们为`TraceProvider`配置一个[`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor]，它会将追踪/跨度分批发送到[`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter]，后者再将跨度和追踪分批导出到OpenAI后端。

若要自定义此默认设置，将追踪发送到替代或额外的后端，或修改导出器行为，有两种选择：

1. [`add_trace_processor()`][agents.tracing.add_trace_processor]可让你添加一个**额外**的追踪进程，它会在追踪和跨度就绪时接收它们。这样你就可以在将追踪发送到OpenAI后端之外执行自己的处理。
2. [`set_trace_processors()`][agents.tracing.set_trace_processors]可让你用自己的追踪进程**替换**默认进程。这意味着，除非你包含一个负责发送的`TracingProcessor`，否则追踪不会发送到OpenAI后端。


## 非OpenAI模型的追踪

你可以将OpenAI API密钥与非OpenAI模型一起使用，以在OpenAI Traces仪表板中启用免费追踪，而无需禁用追踪。请参阅Models指南中的[第三方适配器](models/index.md#third-party-adapters)部分，了解适配器选择和设置注意事项。

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

如果你只需要为单次运行使用不同的追踪密钥，请通过`RunConfig`传入，而不是更改全局导出器。

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(tracing={"api_key": "sk-tracing-123"}),
)
```

## 其他说明
- 在Openai Traces仪表板查看免费追踪记录。


## 生态系统集成

以下社区和供应商集成支持OpenAI Agents SDK的追踪接口。

### 外部追踪进程列表

-   [Weights & Biases](https://weave-docs.wandb.ai/guides/integrations/openai_agents)
-   [Arize-Phoenix](https://docs.arize.com/phoenix/tracing/integrations-tracing/openai-agents-sdk)
-   [Future AGI](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/openai_agents)
-   [MLflow（自托管/OSS）](https://mlflow.org/docs/latest/tracing/integrations/openai-agent)
-   [MLflow（Databricks托管）](https://docs.databricks.com/aws/en/mlflow/mlflow-tracing#-automatic-tracing)
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
-   [Latitude](https://docs.latitude.so/telemetry/frameworks/openai-agents)