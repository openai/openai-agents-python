---
search:
  exclude: true
---
# トレーシング

Agents SDK には、エージェント実行中のイベントの包括的な記録を収集する組み込みのトレーシングが含まれています: LLM 生成、ツール呼び出し、ハンドオフ、ガードレール、さらには発生したカスタムイベントまで含まれます。[Traces ダッシュボード](https://platform.openai.com/traces)を使用すると、開発中および本番環境でワークフローをデバッグ、可視化、監視できます。

!!!note

    トレーシングはデフォルトで有効です。一般的には、次の 3 つの方法で無効化できます:

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定することで、トレーシングをグローバルに無効化できます
    2. コード内で [`set_tracing_disabled(True)`][agents.set_tracing_disabled] を使用して、トレーシングをグローバルに無効化できます
    3. 単一の実行については、[`agents.run.RunConfig.tracing_disabled`][] を `True` に設定することで、トレーシングを無効化できます

***OpenAI の API を使用して Zero Data Retention (ZDR) ポリシーの対象となっている組織では、トレーシングは利用できません。***

## トレースとスパン

-   **トレース** は、「ワークフロー」の単一のエンドツーエンド操作を表します。スパンで構成されます。トレースには次のプロパティがあります:
    -   `workflow_name`: これは論理的なワークフローまたはアプリです。たとえば「コード生成」や「カスタマーサービス」です。
    -   `trace_id`: トレースの一意の ID です。渡さない場合は自動生成されます。`trace_<32_alphanumeric>` の形式である必要があります。
    -   `group_id`: 任意のグループ ID で、同じ会話に由来する複数のトレースを関連付けるために使用します。たとえば、チャットスレッド ID を使用できます。
    -   `disabled`: True の場合、トレースは記録されません。
    -   `metadata`: トレースの任意のメタデータです。
-   **スパン** は、開始時刻と終了時刻を持つ操作を表します。スパンには次があります:
    -   `started_at` と `ended_at` のタイムスタンプ。
    -   `trace_id`: 属するトレースを表します
    -   `parent_id`: このスパンの親スパン（存在する場合）を指します
    -   `span_data`: スパンに関する情報です。たとえば、`AgentSpanData` にはエージェントに関する情報が含まれ、`GenerationSpanData` には LLM 生成に関する情報が含まれます。

## デフォルトのトレーシング

デフォルトでは、SDK は次をトレースします:

-   `Runner.{run, run_sync, run_streamed}()` 全体が `trace()` でラップされます。
-   エージェントが実行されるたびに、`agent_span()` でラップされます
-   LLM 生成は `generation_span()` でラップされます
-   関数ツール呼び出しは、それぞれ `function_span()` でラップされます
-   ガードレールは `guardrail_span()` でラップされます
-   ハンドオフは `handoff_span()` でラップされます
-   音声入力 (speech-to-text) は `transcription_span()` でラップされます
-   音声出力 (text-to-speech) は `speech_span()` でラップされます
-   関連する音声スパンは、`speech_group_span()` の配下に親子付けされる場合があります

デフォルトでは、トレースには "Agent workflow" という名前が付けられます。`trace` を使用する場合はこの名前を設定できます。また、[`RunConfig`][agents.run.RunConfig] を使用して名前やその他のプロパティを設定することもできます。

さらに、[カスタムトレースプロセッサー](#custom-tracing-processors)を設定して、トレースを他の送信先へ送ることもできます（置き換え先として、または二次的な送信先として）。

## 長時間実行ワーカーと即時エクスポート

デフォルトの [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] は、トレースを
数秒ごとにバックグラウンドでエクスポートします。また、メモリ内キューがサイズトリガーに達した場合はより早くエクスポートし、
プロセス終了時には最終的なフラッシュも実行します。Celery、
RQ、Dramatiq、FastAPI バックグラウンドタスクのような長時間実行ワーカーでは、通常、追加のコードなしでトレースが自動的にエクスポートされますが、各ジョブの終了直後に Traces ダッシュボードへ表示されない場合があります。

作業単位の終了時に即時配信の保証が必要な場合は、
トレースコンテキストを抜けた後に [`flush_traces()`][agents.tracing.flush_traces] を呼び出します。

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

[`flush_traces()`][agents.tracing.flush_traces] は、現在バッファリングされているトレースとスパンが
エクスポートされるまでブロックするため、構築途中のトレースをフラッシュしないよう、`trace()` が閉じた後に呼び出してください。デフォルトのエクスポート遅延で問題ない場合、この呼び出しは省略できます。

## 上位レベルのトレース

場合によっては、`run()` への複数回の呼び出しを単一のトレースの一部にしたいことがあります。これは、コード全体を `trace()` でラップすることで実現できます。

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

1. 2 回の `Runner.run` 呼び出しは `with trace()` でラップされているため、個々の実行は 2 つのトレースを作成するのではなく、全体のトレースの一部になります。

## トレースの作成

[`trace()`][agents.tracing.trace] 関数を使用してトレースを作成できます。トレースは開始して終了する必要があります。これには 2 つの選択肢があります:

1. **推奨**: トレースをコンテキストマネージャーとして使用します。つまり、`with trace(...) as my_trace` のようにします。これにより、適切なタイミングでトレースが自動的に開始および終了されます。
2. 手動で [`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を呼び出すこともできます。

現在のトレースは、Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) を介して追跡されます。つまり、並行処理でも自動的に機能します。トレースを手動で開始/終了する場合は、現在のトレースを更新するために、`start()`/`finish()` に `mark_as_current` と `reset_current` を渡す必要があります。

## スパンの作成

さまざまな [`*_span()`][agents.tracing.create] メソッドを使用してスパンを作成できます。一般に、スパンを手動で作成する必要はありません。カスタムスパン情報を追跡するために、[`custom_span()`][agents.tracing.custom_span] 関数が用意されています。

スパンは自動的に現在のトレースの一部となり、最も近い現在のスパンの下にネストされます。この現在のスパンは、Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) を介して追跡されます。

## 機微データ

特定のスパンは、潜在的に機微なデータをキャプチャする場合があります。

`generation_span()` は LLM 生成の入力/出力を保存し、`function_span()` は関数呼び出しの入力/出力を保存します。これらには機微データが含まれる可能性があるため、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] を使用して、そのデータのキャプチャを無効化できます。

同様に、音声スパンにはデフォルトで、入力音声および出力音声の base64 エンコードされた PCM データが含まれます。この音声データのキャプチャは、[`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] を設定することで無効化できます。

デフォルトでは、`trace_include_sensitive_data` は `True` です。アプリを実行する前に `OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA` 環境変数を `true/1` または `false/0` にエクスポートすることで、コードを変更せずにデフォルトを設定できます。

## カスタムトレーシングプロセッサー

トレーシングの高レベルアーキテクチャは次のとおりです:

-   初期化時に、グローバルな [`TraceProvider`][agents.tracing.setup.TraceProvider] を作成します。これはトレースの作成を担当します。
-   トレース/スパンをバッチで [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] に送信する [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] を使用して、`TraceProvider` を設定します。`BackendSpanExporter` は、スパンとトレースをバッチで OpenAI バックエンドへエクスポートします。

このデフォルト設定をカスタマイズし、トレースを代替または追加のバックエンドへ送信したり、エクスポーターの動作を変更したりするには、2 つの選択肢があります:

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] を使用すると、トレースやスパンの準備ができた時点でそれらを受け取る **追加の** トレースプロセッサーを追加できます。これにより、トレースを OpenAI のバックエンドへ送信することに加えて、独自の処理を行えます。
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] を使用すると、デフォルトのプロセッサーを独自のトレースプロセッサーで **置き換える** ことができます。これは、OpenAI バックエンドへの送信を行う `TracingProcessor` を含めない限り、トレースが OpenAI バックエンドへ送信されないことを意味します。


## 非 OpenAI モデルのトレーシング

非 OpenAI モデルで OpenAI API キーを使用すると、トレーシングを無効化する必要なく、OpenAI Traces ダッシュボードで無料のトレーシングを有効化できます。アダプターの選択とセットアップ時の注意点については、モデルガイドの[サードパーティアダプター](models/index.md#third-party-adapters)セクションを参照してください。

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

単一の実行でのみ別のトレーシングキーが必要な場合は、グローバルエクスポーターを変更する代わりに、`RunConfig` で渡してください。

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(tracing={"api_key": "sk-tracing-123"}),
)
```

## 補足事項
- 無料のトレースは Openai Traces ダッシュボードで確認できます。


## エコシステム統合

以下のコミュニティおよびベンダーによる統合は、OpenAI Agents SDK のトレーシングインターフェイスをサポートしています。

### 外部トレーシングプロセッサー一覧

-   [Weights & Biases](https://weave-docs.wandb.ai/guides/integrations/openai_agents)
-   [Arize-Phoenix](https://docs.arize.com/phoenix/tracing/integrations-tracing/openai-agents-sdk)
-   [Future AGI](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/openai_agents)
-   [MLflow (セルフホスト/OSS)](https://mlflow.org/docs/latest/tracing/integrations/openai-agent)
-   [MLflow (Databricks ホスト版)](https://docs.databricks.com/aws/en/mlflow/mlflow-tracing#-automatic-tracing)
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