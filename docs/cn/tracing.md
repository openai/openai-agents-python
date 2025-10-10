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

## デフォルトのトレーシング

デフォルトでは、 SDK は次をトレースします:

-   全体の `Runner.{run, run_sync, run_streamed}()` は `trace()` でラップされます。
-   エージェントが実行されるたびに、`agent_span()` でラップされます
-   LLM 生成は `generation_span()` でラップされます
-   関数ツールの呼び出しは個々に `function_span()` でラップされます
-   ガードレールは `guardrail_span()` でラップされます
-   ハンドオフは `handoff_span()` でラップされます
-   音声入力（音声認識）は `transcription_span()` でラップされます
-   音声出力（音声合成）は `speech_span()` でラップされます
-   関連する音声スパンは `speech_group_span()` の子になる場合があります

デフォルトでは、トレース名は "Agent workflow" です。`trace` を使う場合にこの名前を設定できますし、[`RunConfig`][agents.run.RunConfig] で名前やその他のプロパティを設定することもできます。

さらに、[カスタム トレース プロセッサー](#custom-tracing-processors) を設定して、トレースを他の送信先に出力できます（置き換え、または副次的な送信先として）。

## 上位レベルのトレース

`run()` への複数回の呼び出しを 1 つのトレースにまとめたい場合があります。これを行うには、コード全体を `trace()` でラップします。

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

1. `Runner.run` への 2 回の呼び出しが `with trace()` でラップされているため、個々の実行は 2 つのトレースを作成するのではなく、全体のトレースの一部になります。

## トレースの作成

[`trace()`][agents.tracing.trace] 関数を使ってトレースを作成できます。トレースは開始と終了が必要です。方法は 2 つあります:

1. 推奨: トレースをコンテキストマネージャとして使用します（例: `with trace(...) as my_trace`）。これにより、適切なタイミングで自動的に開始・終了します。
2. 手動で [`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を呼び出すこともできます。

現在のトレースは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) によって追跡されます。これは自動的に並行処理で機能することを意味します。トレースを手動で開始/終了する場合、現在のトレースを更新するために `start()`/`finish()` に `mark_as_current` と `reset_current` を渡す必要があります。

## スパンの作成

各種の [`*_span()`][agents.tracing.create] メソッドを使ってスパンを作成できます。一般的にはスパンを手動で作成する必要はありません。カスタムのスパン情報を追跡するために、[`custom_span()`][agents.tracing.custom_span] 関数を利用できます。

スパンは自動的に現在のトレースの一部となり、Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) によって追跡される、最も近い現在のスパンの下にネストされます。

## 機微データ

一部のスパンは機微なデータを取得する可能性があります。

`generation_span()` は LLM 生成の入力/出力を保存し、`function_span()` は関数呼び出しの入力/出力を保存します。これらには機微なデータが含まれる可能性があるため、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] によってその取得を無効化できます。

同様に、音声スパンはデフォルトで入出力の音声に対して base64 でエンコードされた PCM データを含みます。[`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] を設定して、この音声データの取得を無効化できます。

## カスタム トレーシング プロセッサー

トレーシングの高レベルなアーキテクチャは次のとおりです:

-   初期化時に、トレースを作成する役割を持つグローバルな [`TraceProvider`][agents.tracing.setup.TraceProvider] を作成します。
-   `TraceProvider` に、スパン/トレースをバッチで [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] に送信する [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] を設定します。`BackendSpanExporter` は OpenAI バックエンドへバッチでエクスポートします。

デフォルト設定をカスタマイズし、別のバックエンドに送る／追加のバックエンドに複製する／エクスポーターの挙動を変更するには、次の 2 つの方法があります:

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] は、トレースとスパンの準備が整い次第それらを受け取る、**追加の** トレースプロセッサーを追加できます。これにより、OpenAI のバックエンドへの送信に加えて、独自の処理を行えます。
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] は、デフォルトのプロセッサーを独自のトレースプロセッサーに**置き換え**られます。OpenAI バックエンドにトレースを送るには、その処理を行う `TracingProcessor` を含める必要があります。

## 非 OpenAI モデルでのトレーシング

OpenAI の API キーを非 OpenAI モデルで使用すると、トレーシングを無効化することなく OpenAI Traces ダッシュボードで無料のトレーシングを有効化できます。

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

## 注意事項
- 無料のトレースは OpenAI Traces ダッシュボードで表示できます。

## 外部トレーシング プロセッサー一覧

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