---
search:
  exclude: true
---
# トレーシング

Agents SDK には組み込みのトレーシングが含まれており、エージェント実行中のイベントの包括的な記録を収集します。これには、LLM 生成、ツール呼び出し、ハンドオフ、ガードレール、さらには発生するカスタムイベントまで含まれます。[Traces ダッシュボード](https://platform.openai.com/traces)を使用すると、開発中および本番環境でワークフローをデバッグ、可視化、監視できます。

!!!note

    トレーシングはデフォルトで有効になっています。一般的には、次の 3 つの方法で無効にできます。

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定することで、トレーシングをグローバルに無効化できます
    2. [`set_tracing_disabled(True)`][agents.set_tracing_disabled] を使って、コード内でトレーシングをグローバルに無効化できます
    3. [`agents.run.RunConfig.tracing_disabled`][] を `True` に設定することで、単一の実行についてトレーシングを無効化できます

***OpenAI の API を使用し、Zero Data Retention (ZDR) ポリシー下で運用している組織では、トレーシングは利用できません。***

## トレースとスパン

-   **トレース** は、「ワークフロー」の単一のエンドツーエンド操作を表します。これらはスパンで構成されます。トレースには以下のプロパティがあります:
    -   `workflow_name`: 論理的なワークフローまたはアプリです。たとえば「コード生成」や「カスタマーサービス」です。
    -   `trace_id`: トレースの一意の ID です。渡さない場合は自動生成されます。形式は `trace_<32_alphanumeric>` でなければなりません。
    -   `group_id`: 任意のグループ ID で、同じ会話からの複数のトレースを関連付けるために使用します。たとえば、チャットスレッド ID を使用できます。
    -   `disabled`: True の場合、トレースは記録されません。
    -   `metadata`: トレースの任意のメタデータです。
-   **スパン** は、開始時刻と終了時刻を持つ操作を表します。スパンには以下があります:
    -   `started_at` と `ended_at` のタイムスタンプ。
    -   `trace_id`: そのスパンが属するトレースを表します
    -   `parent_id`: このスパンの親スパン (存在する場合) を指します
    -   `span_data`: スパンに関する情報です。たとえば、`AgentSpanData` にはエージェントに関する情報が含まれ、`GenerationSpanData` には LLM 生成に関する情報が含まれる、などです。

## デフォルトのトレーシング

デフォルトでは、SDK は以下をトレースします:

-   `Runner.{run, run_sync, run_streamed}()` 全体が `trace()` でラップされます。
-   エージェントが実行されるたびに、`agent_span()` でラップされます
-   LLM 生成は `generation_span()` でラップされます
-   関数ツール呼び出しはそれぞれ `function_span()` でラップされます
-   ガードレールは `guardrail_span()` でラップされます
-   ハンドオフは `handoff_span()` でラップされます
-   音声入力 (音声テキスト変換) は `transcription_span()` でラップされます
-   音声出力 (テキスト音声変換) は `speech_span()` でラップされます
-   関連する音声スパンは `speech_group_span()` の下に親子関係として配置される場合があります

デフォルトでは、トレース名は "Agent workflow" です。`trace` を使用する場合はこの名前を設定できます。また、[`RunConfig`][agents.run.RunConfig] で名前やその他のプロパティを構成できます。

さらに、[カスタムトレーシングプロセッサー](#custom-tracing-processors)を設定して、トレースを他の送信先へ送信することもできます (置き換え、または副次的な送信先として)。

## 長時間実行ワーカーと即時エクスポート

デフォルトの [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] は、数秒ごとにバックグラウンドでトレースをエクスポートします。
または、メモリ内キューがサイズトリガーに達した場合はそれより早くエクスポートし、
プロセス終了時にも最終的なフラッシュを実行します。Celery、RQ、Dramatiq、FastAPI バックグラウンドタスクのような長時間実行ワーカーでは、通常、追加コードなしでトレースが自動的にエクスポートされますが、各ジョブの完了直後に Traces ダッシュボードへ表示されない場合があります。

作業単位の終了時に即時配信の保証が必要な場合は、トレースコンテキストが終了した後に
[`flush_traces()`][agents.tracing.flush_traces] を呼び出してください。

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
エクスポートされるまでブロックします。そのため、部分的に構築されたトレースをフラッシュしないように、`trace()` が閉じた後に呼び出してください。デフォルトのエクスポート遅延で許容できる場合は、この呼び出しを省略できます。

## 上位レベルのトレース

場合によっては、`run()` への複数回の呼び出しを 1 つのトレースの一部にしたいことがあります。コード全体を `trace()` でラップすることで実現できます。

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

1. 2 つの `Runner.run` 呼び出しは `with trace()` でラップされているため、個別の実行は 2 つのトレースを作成するのではなく、全体のトレースの一部になります。

## トレースの作成

[`trace()`][agents.tracing.trace] 関数を使用してトレースを作成できます。トレースは開始および終了する必要があります。これには 2 つの方法があります:

1. **推奨**: トレースをコンテキストマネージャーとして使用します。つまり、`with trace(...) as my_trace` とします。これにより、適切なタイミングでトレースが自動的に開始および終了されます。
2. [`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を手動で呼び出すこともできます。

現在のトレースは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) によって追跡されます。これにより、並行処理でも自動的に機能します。トレースを手動で開始/終了する場合は、現在のトレースを更新するために `start()`/`finish()` に `mark_as_current` と `reset_current` を渡す必要があります。

## スパンの作成

各種 [`*_span()`][agents.tracing.create] メソッドを使用してスパンを作成できます。一般に、スパンを手動で作成する必要はありません。カスタムスパン情報を追跡するために [`custom_span()`][agents.tracing.custom_span] 関数が利用できます。

スパンは自動的に現在のトレースの一部となり、現在の最も近いスパンの下にネストされます。この現在のスパンは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) によって追跡されます。

## 機密データ

一部のスパンは、機密性の高い可能性があるデータをキャプチャする場合があります。

`generation_span()` は LLM 生成の入力/出力を保存し、`function_span()` は関数呼び出しの入力/出力を保存します。これらには機密データが含まれる可能性があるため、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] を使ってそのデータのキャプチャを無効化できます。

同様に、音声スパンには、デフォルトで入力および出力音声の base64 エンコードされた PCM データが含まれます。この音声データのキャプチャは、[`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] を構成することで無効化できます。

デフォルトでは、`trace_include_sensitive_data` は `True` です。アプリを実行する前に `OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA` 環境変数を `true/1` または `false/0` にエクスポートすることで、コードなしでデフォルトを設定できます。

## カスタムトレーシングプロセッサー

トレーシングの高レベルアーキテクチャは次のとおりです:

-   初期化時に、トレースの作成を担当するグローバルな [`TraceProvider`][agents.tracing.setup.TraceProvider] を作成します。
-   `TraceProvider` は、トレース/スパンをバッチで [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] に送信する [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] で構成します。`BackendSpanExporter` は、スパンとトレースをバッチで OpenAI バックエンドにエクスポートします。

このデフォルト設定をカスタマイズして、代替または追加のバックエンドへトレースを送信したり、エクスポーターの動作を変更したりするには、2 つの選択肢があります:

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] を使用すると、利用可能になったトレースとスパンを受け取る **追加** のトレーシングプロセッサーを追加できます。これにより、トレースを OpenAI バックエンドに送信することに加えて、独自の処理を実行できます。
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] を使用すると、デフォルトのプロセッサーを独自のトレーシングプロセッサーで **置き換える** ことができます。これは、それを行う `TracingProcessor` を含めない限り、トレースが OpenAI バックエンドに送信されないことを意味します。


## 非 OpenAI モデルでのトレーシング

非 OpenAI モデルで OpenAI API キーを使用すると、トレーシングを無効にする必要なく、OpenAI Traces ダッシュボードで無料のトレーシングを有効にできます。アダプターの選択とセットアップ時の注意点については、モデルガイドの[サードパーティアダプター](models/index.md#third-party-adapters)セクションを参照してください。

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

単一の実行で異なるトレーシングキーだけが必要な場合は、グローバルエクスポーターを変更する代わりに `RunConfig` 経由で渡してください。

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(tracing={"api_key": "sk-tracing-123"}),
)
```

## 補足事項
- Openai Traces ダッシュボードで無料のトレースを表示できます。


## エコシステム統合

以下のコミュニティおよびベンダー統合は、OpenAI Agents SDK のトレーシングインターフェイスに対応しています。

### 外部トレーシングプロセッサー一覧

-   [Weights & Biases](https://weave-docs.wandb.ai/guides/integrations/openai_agents)
-   [Arize-Phoenix](https://docs.arize.com/phoenix/tracing/integrations-tracing/openai-agents-sdk)
-   [Future AGI](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/openai_agents)
-   [MLflow (セルフホスト/OSS)](https://mlflow.org/docs/latest/tracing/integrations/openai-agent)
-   [MLflow (Databricks ホスト)](https://docs.databricks.com/aws/en/mlflow/mlflow-tracing#-automatic-tracing)
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