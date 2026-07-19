---
search:
  exclude: true
---
# トレーシング

Agents SDK には組み込みのトレーシング機能が含まれており、エージェントの実行中に発生するイベント（LLM 生成、ツール呼び出し、ハンドオフ、ガードレール、さらにはカスタムイベント）を包括的に記録します。[トレースダッシュボード](https://platform.openai.com/traces)を使用すると、開発環境および本番環境でワークフローをデバッグ、可視化、監視できます。

!!!note

    トレーシングはデフォルトで有効です。一般的な無効化方法は次の 3 つです。

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定して、トレーシングをグローバルに無効化できます
    2. [`set_tracing_disabled(True)`][agents.set_tracing_disabled] を使用して、コード内でトレーシングをグローバルに無効化できます
    3. [`agents.run.RunConfig.tracing_disabled`][] を `True` に設定して、単一の実行に対するトレーシングを無効化できます

***OpenAI の API を使用し、ゼロデータ保持（ZDR）ポリシーの下で運用している組織では、トレーシングを利用できません。***

## トレースとスパン

-   **トレース**は、「ワークフロー」の単一のエンドツーエンド処理を表します。トレースはスパンで構成され、次のプロパティがあります。
    -   `workflow_name`: 論理的なワークフローまたはアプリです。たとえば、「コード生成」や「カスタマーサービス」です。
    -   `trace_id`: トレースの一意な ID です。指定しない場合は自動的に生成されます。形式は `trace_<32_alphanumeric>` である必要があります。
    -   `group_id`: 同じ会話に由来する複数のトレースを関連付けるための、オプションのグループ ID です。たとえば、チャットスレッド ID を使用できます。
    -   `disabled`: True の場合、トレースは記録されません。
    -   `metadata`: トレースのオプションのメタデータです。
-   **スパン**は、開始時刻と終了時刻を持つ処理を表します。スパンには次のものがあります。
    -   `started_at` と `ended_at` のタイムスタンプ。
    -   `trace_id`。所属するトレースを表します
    -   `parent_id`。このスパンの親スパン（存在する場合）を指します
    -   `span_data`。スパンに関する情報です。たとえば、`AgentSpanData` にはエージェントに関する情報が含まれ、`GenerationSpanData` には LLM 生成に関する情報が含まれます。

## デフォルトのトレーシング

デフォルトでは、SDK は次の項目をトレースします。

-   `Runner.{run, run_sync, run_streamed}()` 全体が `trace()` でラップされます。
-   Runner の各呼び出しが `task_span()` でラップされます。
-   モデルの各ターンが `turn_span()` でラップされます。
-   エージェントが実行されるたびに、`agent_span()` でラップされます
-   LLM 生成が `generation_span()` でラップされます
-   関数ツールの各呼び出しが `function_span()` でラップされます
-   ガードレールが `guardrail_span()` でラップされます
-   ハンドオフが `handoff_span()` でラップされます
-   音声入力（音声テキスト変換）が `transcription_span()` でラップされます
-   音声出力（テキスト音声変換）が `speech_span()` でラップされます
-   関連する音声スパンは、`speech_group_span()` の子になる場合があります

デフォルトでは、トレースの名前は「Agent workflow」です。`trace` を使用する場合はこの名前を設定できます。また、[`RunConfig`][agents.run.RunConfig] を使用して、名前やその他のプロパティを構成できます。

よりコンパクトな階層にしたい場合は、実行時にタスクスパンとターンスパンの自動作成を無効にします。エージェント、生成、関数、ガードレール、ハンドオフ、カスタムの各スパンは引き続き記録されます。

```python
from agents import RunConfig, Runner

result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(tracing={"include_task_and_turn_spans": False}),
)
```

さらに、[カスタムトレースプロセッサー](#custom-tracing-processors)を設定して、トレースを別の送信先へ送ることもできます（置き換え先または追加の送信先として）。

## 長時間実行ワーカーと即時エクスポート

デフォルトの [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] は、数秒ごと、またはメモリ内キューがサイズのしきい値に達した時点で、それより早くバックグラウンドでトレースをエクスポートします。また、プロセスの終了時に最後のフラッシュも実行します。Celery、RQ、Dramatiq、FastAPI のバックグラウンドタスクなど、長時間実行されるワーカーでは、通常、追加のコードなしでトレースが自動的にエクスポートされますが、各ジョブの完了直後にはトレースダッシュボードに表示されない場合があります。

作業単位の終了時に即時配信を保証する必要がある場合は、トレースコンテキストの終了後に [`flush_traces()`][agents.tracing.flush_traces] を呼び出します。

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

[`flush_traces()`][agents.tracing.flush_traces] は、現在バッファリングされているトレースとスパンがエクスポートされるまで処理をブロックします。そのため、構築途中のトレースをフラッシュしないよう、`trace()` が閉じた後に呼び出してください。デフォルトのエクスポート遅延で問題ない場合は、この呼び出しを省略できます。

## 上位レベルのトレース

複数回の `run()` 呼び出しを単一のトレースに含めたい場合があります。その場合は、コード全体を `trace()` でラップします。

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

1. `Runner.run` の 2 回の呼び出しが `with trace()` でラップされているため、個別の実行によって 2 つのトレースが作成されるのではなく、全体のトレースの一部になります。

## トレースの作成

[`trace()`][agents.tracing.trace] 関数を使用してトレースを作成できます。トレースは開始および終了する必要があります。これには次の 2 つの方法があります。

1. **推奨**: トレースをコンテキストマネージャーとして、つまり `with trace(...) as my_trace` の形式で使用します。これにより、適切なタイミングでトレースが自動的に開始および終了します。
2. [`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を手動で呼び出すこともできます。

現在のトレースは、Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) を介して追跡されます。つまり、並行処理でも自動的に動作します。トレースを手動で開始または終了する場合、現在のトレースを更新するには、`start()`/`finish()` に `mark_as_current` と `reset_current` を渡す必要があります。

## スパンの作成

さまざまな [`*_span()`][agents.tracing.create] メソッドを使用してスパンを作成できます。通常、スパンを手動で作成する必要はありません。カスタムスパン情報を追跡するために、[`custom_span()`][agents.tracing.custom_span] 関数を利用できます。

スパンは自動的に現在のトレースの一部となり、Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) を介して追跡される、最も近い現在のスパンの下にネストされます。

## 機密データ

一部のスパンは、機密性の高い可能性があるデータを取得する場合があります。

`generation_span()` は LLM 生成の入力と出力を保存し、`function_span()` は関数呼び出しの入力と出力を保存します。これらには機密データが含まれる可能性があるため、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] を使用して、そのデータの取得を無効化できます。

同様に、音声スパンには、デフォルトで入力音声と出力音声の base64 エンコードされた PCM データが含まれます。[`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] を構成することで、この音声データの取得を無効化できます。

デフォルトでは、`trace_include_sensitive_data` は `True` です。アプリを実行する前に、環境変数 `OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA` を `true/1` または `false/0` に設定することで、コードを使用せずにデフォルト値を設定できます。

## カスタムトレーシングプロセッサー

トレーシングの上位レベルのアーキテクチャは次のとおりです。

-   初期化時に、トレースの作成を担当するグローバルな [`TraceProvider`][agents.tracing.setup.TraceProvider] を作成します。
-   [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] を使用して `TraceProvider` を構成します。`BatchTraceProcessor` は、トレースとスパンをバッチで [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] に送信し、`BackendSpanExporter` がスパンとトレースをバッチで OpenAI バックエンドにエクスポートします。

このデフォルト設定をカスタマイズして、トレースを別のバックエンドや追加のバックエンドへ送信したり、エクスポーターの動作を変更したりするには、次の 2 つの方法があります。

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] を使用すると、準備が整ったトレースとスパンを受け取る **追加の** トレースプロセッサーを追加できます。これにより、OpenAI のバックエンドへトレースを送信する処理に加えて、独自の処理を実行できます。
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] を使用すると、デフォルトのプロセッサーを独自のトレースプロセッサーで **置き換える** ことができます。この場合、OpenAI バックエンドへ送信する `TracingProcessor` を含めない限り、トレースは OpenAI バックエンドに送信されません。


## OpenAI 以外のモデルでのトレーシング

OpenAI 以外のモデルで OpenAI API キーを使用すると、トレーシングを無効化せずに、OpenAI のトレースダッシュボードで無料のトレーシングを有効にできます。アダプターの選択とセットアップに関する注意事項については、モデルガイドの[サードパーティーアダプター](models/index.md#third-party-adapters)セクションを参照してください。

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

単一の実行にのみ別のトレーシングキーが必要な場合は、グローバルエクスポーターを変更する代わりに、`RunConfig` を介して渡してください。

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(tracing={"api_key": "sk-tracing-123"}),
)
```

## 追加の注意事項
- OpenAI のトレースダッシュボードで、トレースを無料で表示できます。


## エコシステム統合

次のコミュニティおよびベンダー統合は、OpenAI Agents SDK のトレーシングインターフェースをサポートしています。

### 外部トレーシングプロセッサーの一覧

-   [Weights & Biases](https://weave-docs.wandb.ai/guides/integrations/openai_agents)
-   [Arize-Phoenix](https://docs.arize.com/phoenix/tracing/integrations-tracing/openai-agents-sdk)
-   [Future AGI](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/openai_agents)
-   [MLflow（セルフホスト／OSS）](https://mlflow.org/docs/latest/tracing/integrations/openai-agent)
-   [MLflow（Databricks ホスト）](https://docs.databricks.com/aws/en/mlflow/mlflow-tracing#-automatic-tracing)
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
-   [DProvenanceKit](https://dprovenance.dev/openai-agents/)