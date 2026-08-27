---
search:
  exclude: true
---
# トレーシング

Agents SDK にはトレーシングが組み込まれており、エージェントの実行中に発生するイベント（LLM 生成、ツール呼び出し、ハンドオフ、ガードレール、さらにはカスタムイベント）を包括的に記録します。[Traces ダッシュボード](https://platform.openai.com/traces)を使用すると、開発環境と本番環境の両方でワークフローのデバッグ、可視化、監視を行えます。

!!!note

    トレーシングはデフォルトで有効です。一般的な次の 3 つの方法で無効にできます。

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定して、トレーシングをグローバルに無効化できます
    2. [`set_tracing_disabled(True)`][agents.set_tracing_disabled] を使用して、コード内でトレーシングをグローバルに無効化できます
    3. [`agents.run.RunConfig.tracing_disabled`][] を `True` に設定して、1 回の実行に対するトレーシングを無効化できます

***ゼロデータ保持（ZDR）ポリシーの下で OpenAI の API を使用する組織では、トレーシングを利用できません。***

## トレースとスパン {#traces-and-spans}

-   **トレース** は、「ワークフロー」における単一のエンドツーエンドの処理を表します。トレースはスパンで構成され、次のプロパティがあります。
    -   `workflow_name`: 論理的なワークフローまたはアプリの名前です。たとえば、「コード生成」や「カスタマーサービス」です。
    -   `trace_id`: トレースの一意な ID です。指定しない場合は自動生成されます。形式は `trace_<32_alphanumeric>` である必要があります。
    -   `group_id`: 同じ会話に含まれる複数のトレースを関連付けるための、省略可能なグループ ID です。たとえば、チャットスレッド ID を使用できます。
    -   `disabled`: True の場合、トレースは記録されません。
    -   `metadata`: トレースの省略可能なメタデータです。
-   **スパン** は、開始時刻と終了時刻を持つ処理を表します。スパンには次のものがあります。
    -   `started_at` と `ended_at` のタイムスタンプ。
    -   `trace_id`: 所属するトレースを表します
    -   `parent_id`: このスパンの親スパン（存在する場合）を指します
    -   `span_data`: スパンに関する情報です。たとえば、`AgentSpanData` にはエージェントに関する情報が含まれ、`GenerationSpanData` には LLM 生成に関する情報が含まれます。

## デフォルトのトレーシング {#default-tracing}

デフォルトでは、SDK は次の項目をトレースします。

-   `Runner.{run, run_sync, run_streamed}()` 全体が `trace()` でラップされます。
-   Runner の各呼び出しが `task_span()` でラップされます。
-   モデルの各ターンが `turn_span()` でラップされます。
-   エージェントが実行されるたびに、`agent_span()` でラップされます
-   LLM 生成が `generation_span()` でラップされます
-   各関数ツール呼び出しが `function_span()` でラップされます
-   ガードレールが `guardrail_span()` でラップされます
-   ハンドオフが `handoff_span()` でラップされます
-   音声入力（音声テキスト変換）が `transcription_span()` でラップされます
-   音声出力（テキスト音声変換）が `speech_span()` でラップされます
-   SDK は、関連する音声スパンを `speech_group_span()` の子としてまとめる場合があります

デフォルトのトレース名は、リテラル文字列 `Agent workflow` です。`trace` を使用する場合はこの名前を設定できます。また、[`RunConfig`][agents.run.RunConfig] を使用して、名前やその他のプロパティを構成することもできます。

よりコンパクトな階層にするには、実行時にタスクスパンとターンスパンの自動作成を無効にします。エージェント、生成、関数、ガードレール、ハンドオフ、カスタムの各スパンは引き続き記録されます。

```python
from agents import RunConfig, Runner

result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(tracing={"include_task_and_turn_spans": False}),
)
```

さらに、トレースを別の送信先へ送るために、[カスタムトレースプロセッサー](#custom-tracing-processors)を設定できます（送信先の置き換え、または追加の送信先として使用できます）。

## 長時間実行ワーカーと即時エクスポート {#long-running-workers-and-immediate-exports}

デフォルトの [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] は、数秒ごと、またはメモリ内キューがサイズのしきい値に達した場合はそれより早く、バックグラウンドでトレースをエクスポートします。また、プロセス終了時に最終的なフラッシュも実行します。Celery、RQ、Dramatiq、FastAPI のバックグラウンドタスクなどの長時間実行ワーカーでは、通常、追加のコードなしでトレースが自動的にエクスポートされます。ただし、各ジョブの完了直後には Traces ダッシュボードに表示されない場合があります。

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

[`flush_traces()`][agents.tracing.flush_traces] は、現在バッファーされているトレースとスパンがエクスポートされるまで処理をブロックします。そのため、構築途中のトレースをフラッシュしないよう、`trace()` が閉じた後に呼び出してください。デフォルトのエクスポート遅延で問題ない場合は、この呼び出しを省略できます。

## 上位レベルのトレース {#higher-level-traces}

複数の `run()` 呼び出しを 1 つのトレースに含めたい場合があります。その場合は、コード全体を `trace()` でラップします。

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

1. 2 回の `Runner.run` 呼び出しが `with trace()` でラップされているため、それぞれが個別のトレースを作成するのではなく、両方の実行が 1 つの全体的なトレースに含まれます。

## トレースの作成 {#creating-traces}

[`trace()`][agents.tracing.trace] 関数を使用してトレースを作成できます。トレースは開始してから終了する必要があります。これには次の 2 つの方法があります。

1. **推奨**: トレースをコンテキストマネージャーとして使用します。つまり、`with trace(...) as my_trace` を使用します。これにより、適切なタイミングでトレースが自動的に開始および終了します。
2. [`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を手動で呼び出すこともできます。

現在のトレースは、Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) を介して追跡されます。つまり、並行処理でも自動的に動作します。トレースを手動で開始および終了する場合は、現在のトレースを更新するため、`start()` に `mark_as_current` を、`finish()` に `reset_current` を渡します。

## スパンの作成 {#creating-spans}

さまざまな [`*_span()`][agents.tracing.create] メソッドを使用して、スパンを作成できます。通常、スパンを手動で作成する必要はありません。カスタムスパン情報を追跡するために、[`custom_span()`][agents.tracing.custom_span] 関数を利用できます。

スパンは自動的に現在のトレースの一部となり、Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) を介して追跡される、現在の最も近いスパンの子としてネストされます。

## 機密データ {#sensitive-data}

一部のスパンでは、機密性のある可能性のあるデータが取得される場合があります。

`generation_span()` は LLM 生成の入力と出力を保存し、`function_span()` は関数呼び出しの入力と出力を保存します。これらには機密データが含まれる可能性があるため、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] を使用して、そのデータの取得を無効化できます。

同様に、音声スパンには、デフォルトで入出力音声の Base64 エンコードされた PCM データが含まれます。[`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] を構成することで、この音声データの取得を無効化できます。

デフォルトでは、`trace_include_sensitive_data` は `True` です。アプリを実行する前に、環境変数 `OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA` を `true/1` または `false/0` に設定してエクスポートすると、コードを使用せずにデフォルト値を設定できます。

## カスタムトレースプロセッサー {#custom-tracing-processors}

トレーシングの上位レベルのアーキテクチャは次のとおりです。

-   初期化時に、トレースの作成を担うグローバルな [`TraceProvider`][agents.tracing.provider.TraceProvider] を作成します。
-   `TraceProvider` に [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] を構成します。これは、トレースとスパンをバッチで [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] に送信し、同エクスポーターがスパンとトレースを OpenAI バックエンドへバッチでエクスポートします。

このデフォルト設定をカスタマイズし、別のバックエンドや追加のバックエンドへトレースを送信したり、エクスポーターの動作を変更したりするには、次の 2 つの方法があります。

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] を使用すると、準備が整ったトレースとスパンを受信する **追加の** トレースプロセッサーを追加できます。これにより、OpenAI のバックエンドへのトレース送信に加えて、独自の処理を実行できます。
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] を使用すると、デフォルトのプロセッサーを独自のトレースプロセッサーで **置き換える** ことができます。この場合、トレースを送信する `TracingProcessor` を含めない限り、OpenAI のバックエンドにはトレースが送信されません。


## OpenAI 以外のモデルでのトレーシング {#tracing-with-non-openai-models}

OpenAI 以外のモデルを使用する場合、トレーシングを無効にすることなく OpenAI の Traces ダッシュボードで無料のトレーシングを有効にするため、トレーシングエクスポーターに OpenAI API キーを指定できます。アダプターの選択と設定に関する注意事項については、モデルガイドの[サードパーティーアダプター](models/index.md#third-party-adapters)セクションを参照してください。

```python
import os
from agents import set_tracing_export_api_key, Agent
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

1 回の実行にのみ別のトレーシングキーが必要な場合は、グローバルエクスポーターを変更する代わりに、`RunConfig` を介して渡します。

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(tracing={"api_key": "sk-tracing-123"}),
)
```

## 補足事項 {#additional-notes}
- OpenAI Traces ダッシュボードで無料のトレースを確認できます。


## エコシステム連携 {#ecosystem-integrations}

以下のコミュニティおよびベンダーによる連携は、OpenAI Agents SDK のトレーシング API サーフェスをサポートしています。

### 外部トレースプロセッサーの一覧 {#external-tracing-processors-list}

-   [Weights & Biases](https://docs.wandb.ai/weave/guides/integrations/agents/openai-agents-sdk)
-   [Arize Phoenix](https://arize.com/docs/phoenix/integrations/llm-providers/openai/openai-agents-sdk-tracing)
-   [Future AGI](https://docs.futureagi.com/docs/tracing/auto/openai_agents/)
-   [MLflow（セルフホスト／OSS）](https://mlflow.org/docs/latest/tracing/integrations/openai-agent)
-   [MLflow（Databricks ホスト）](https://docs.databricks.com/aws/en/mlflow3/genai/tracing/integrations/openai-agent)
-   [Braintrust](https://www.braintrust.dev/docs/integrations/agent-frameworks/openai-agents-sdk)
-   [Pydantic Logfire](https://pydantic.dev/docs/logfire/integrations/llms/openai/#openai-agents)
-   [AgentOps](https://docs.agentops.ai/v1/integrations/agentssdk)
-   [Scorecard](https://docs.scorecard.io/features/tracing#agent-frameworks)
-   [Respan](https://www.respan.ai/docs/integrations/openai-agents-sdk)
-   [LangSmith](https://docs.langchain.com/langsmith/trace-openai)
-   [Maxim AI](https://www.getmaxim.ai/docs/sdk/python/integrations/openai/agents-sdk)
-   [Comet Opik](https://www.comet.com/docs/opik/integrations/openai_agents)
-   [Langfuse](https://langfuse.com/integrations/frameworks/openai-agents)
-   [Langtrace](https://docs.langtrace.ai/supported-integrations/llm-frameworks/openai-agents-sdk)
-   [Okahu-Monocle](https://github.com/monocle2ai/monocle)
-   [Galileo](https://docs.galileo.ai/how-to-guides/third-party-integrations/openai-agent-integration)
-   [Portkey AI](https://portkey.ai/docs/integrations/agents/openai-agents)
-   [LangDB AI](https://docs.langdb.ai/getting-started/working-with-agent-frameworks/working-with-openai-agents-sdk/)
-   [Agenta](https://agenta.ai/docs/observability/integrations/openai-agents)
-   [PostHog](https://posthog.com/docs/ai-observability/installation/openai-agents)
-   [Traccia](https://traccia.ai/docs/integrations/openai-agents/)
-   [PromptLayer](https://docs.promptlayer.com/features/observability/traces/integrations#openai-agents-sdk)
-   [HoneyHive](https://docs.honeyhive.ai/v2/integrations/openai-agents)
-   [Asqav](https://www.asqav.com/docs/integrations#openai-agents)
-   [Datadog](https://docs.datadoghq.com/llm_observability/instrumentation/auto_instrumentation/?tab=python#openai-agents)
-   [Latitude](https://docs.latitude.so/telemetry/frameworks/openai-agents)
-   [DProvenanceKit](https://dprovenance.dev/openai-agents/)
-   [Tuning Engines](https://github.com/cerebrixos-org/tuning-engines-cli/tree/main/packages/tuning-agents#openai-agents-sdk)