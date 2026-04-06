---
search:
  exclude: true
---
# トレーシング

Agents SDK には組み込みのトレーシングがあり、エージェント実行中のイベントを包括的に記録します: LLM 生成、ツール呼び出し、ハンドオフ、ガードレール、さらに発生したカスタムイベントも含まれます。[Traces ダッシュボード](https://platform.openai.com/traces) を使うことで、開発中および本番環境でワークフローをデバッグ、可視化、監視できます。

!!!note

    トレーシングはデフォルトで有効です。一般的な無効化方法は 3 つあります:

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定して、グローバルにトレーシングを無効化できます
    2. [`set_tracing_disabled(True)`][agents.set_tracing_disabled] を使って、コード内でグローバルにトレーシングを無効化できます
    3. [`agents.run.RunConfig.tracing_disabled`][] を `True` に設定して、単一の実行でトレーシングを無効化できます

***OpenAI の API を使用し、Zero Data Retention (ZDR) ポリシー下で運用している組織では、トレーシングは利用できません。***

## トレースとスパン

-   **トレース**は 1 つの「ワークフロー」におけるエンドツーエンドの単一操作を表します。トレースはスパンで構成されます。トレースには次のプロパティがあります:
    -   `workflow_name`: 論理的なワークフローまたはアプリです。たとえば「Code generation」や「Customer service」です。
    -   `trace_id`: トレースの一意 ID です。指定しない場合は自動生成されます。形式は `trace_<32_alphanumeric>` である必要があります。
    -   `group_id`: 同じ会話の複数トレースを関連付けるための任意のグループ ID です。たとえば、チャットスレッド ID を使えます。
    -   `disabled`: True の場合、トレースは記録されません。
    -   `metadata`: トレースの任意メタデータです。
-   **スパン**は開始時刻と終了時刻を持つ操作を表します。スパンには次があります:
    -   `started_at` と `ended_at` のタイムスタンプ。
    -   属するトレースを表す `trace_id`
    -   このスパンの親スパンを指す `parent_id`（存在する場合）
    -   スパンに関する情報である `span_data`。たとえば `AgentSpanData` には Agent の情報、`GenerationSpanData` には LLM 生成の情報などが含まれます。

## デフォルトトレーシング

デフォルトで SDK は次をトレースします:

-   `Runner.{run, run_sync, run_streamed}()` 全体は `trace()` でラップされます。
-   エージェントが実行されるたびに `agent_span()` でラップされます
-   LLM 生成は `generation_span()` でラップされます
-   関数ツール呼び出しはそれぞれ `function_span()` でラップされます
-   ガードレールは `guardrail_span()` でラップされます
-   ハンドオフは `handoff_span()` でラップされます
-   音声入力（speech-to-text）は `transcription_span()` でラップされます
-   音声出力（text-to-speech）は `speech_span()` でラップされます
-   関連する音声スパンは `speech_group_span()` の子になる場合があります

デフォルトでトレース名は「Agent workflow」です。`trace` を使う場合はこの名前を設定できます。または [`RunConfig`][agents.run.RunConfig] で名前やその他のプロパティを設定できます。

さらに、[カスタムトレースプロセッサー](#custom-tracing-processors) を設定して、トレースを他の宛先へ送信できます（置き換えまたは副次的な宛先として）。

## 長時間稼働ワーカーと即時エクスポート

デフォルトの [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] は、
数秒ごとにバックグラウンドでトレースをエクスポートし、メモリー内キューがサイズしきい値に達した場合はより早く実行し、
さらにプロセス終了時に最終フラッシュも実行します。Celery、
RQ、Dramatiq、または FastAPI バックグラウンドタスクのような長時間稼働ワーカーでは、これにより通常は追加コードなしで
トレースが自動的にエクスポートされますが、各ジョブ完了直後には Traces ダッシュボードに
表示されない場合があります。

作業単位の終了時に即時配信保証が必要な場合は、
トレースコンテキスト終了後に [`flush_traces()`][agents.tracing.flush_traces] を呼び出してください。

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

[`flush_traces()`][agents.tracing.flush_traces] は現在バッファされているトレースとスパンが
エクスポートされるまでブロックするため、部分的に構築されたトレースをフラッシュしないよう
`trace()` が閉じた後で呼び出してください。デフォルトのエクスポート遅延を許容できる場合は、
この呼び出しは省略できます。

## 上位レベルトレース

場合によっては、複数の `run()` 呼び出しを単一のトレースの一部にしたいことがあります。これは、コード全体を `trace()` でラップすることで実現できます。

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

1. `Runner.run` への 2 回の呼び出しが `with trace()` でラップされているため、個々の実行は 2 つのトレースを作成するのではなく、全体トレースの一部になります。

## トレース作成

[`trace()`][agents.tracing.trace] 関数を使ってトレースを作成できます。トレースは開始と終了が必要です。方法は 2 つあります:

1. **推奨**: `with trace(...) as my_trace` のように、トレースをコンテキストマネージャーとして使います。これにより適切なタイミングで自動的にトレースが開始・終了されます。
2. [`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を手動で呼び出すこともできます。

現在のトレースは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で追跡されます。これは並行処理でも自動的に機能することを意味します。トレースを手動で開始/終了する場合は、現在のトレースを更新するために `start()`/`finish()` に `mark_as_current` と `reset_current` を渡す必要があります。

## スパン作成

さまざまな [`*_span()`][agents.tracing.create] メソッドを使ってスパンを作成できます。一般的には、スパンを手動で作成する必要はありません。カスタムスパン情報を追跡するための [`custom_span()`][agents.tracing.custom_span] 関数も利用できます。

スパンは自動的に現在のトレースの一部となり、最も近い現在のスパンの下にネストされます。これは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で追跡されます。

## 機密データ

一部のスパンは機密性の可能性があるデータを取得する場合があります。

`generation_span()` は LLM 生成の入力/出力を保存し、`function_span()` は関数呼び出しの入力/出力を保存します。これらには機密データが含まれる可能性があるため、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] を使ってそのデータ取得を無効化できます。

同様に、Audio スパンにはデフォルトで入力/出力音声の base64 エンコード済み PCM データが含まれます。[`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] を設定して、この音声データの取得を無効化できます。

デフォルトでは `trace_include_sensitive_data` は `True` です。アプリ実行前に環境変数 `OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA` を `true/1` または `false/0` に設定することで、コードを書かずにデフォルトを設定できます。

## カスタムトレーシングプロセッサー

トレーシングの高レベルアーキテクチャは次のとおりです:

-   初期化時に、トレース作成を担当するグローバル [`TraceProvider`][agents.tracing.setup.TraceProvider] を作成します。
-   `TraceProvider` に [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] を設定し、これがトレース/スパンをバッチで [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] に送信します。`BackendSpanExporter` はスパンとトレースをバッチで OpenAI バックエンドにエクスポートします。

このデフォルト設定をカスタマイズして、代替または追加のバックエンドにトレースを送信したり、エクスポーターの動作を変更したりするには、次の 2 つの方法があります:

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] は、準備でき次第トレースとスパンを受け取る**追加**のトレースプロセッサーを追加できます。これにより、OpenAI バックエンドへの送信に加えて独自処理を行えます。
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] は、デフォルトプロセッサーを独自のトレースプロセッサーで**置き換える**ことができます。これは、そうする `TracingProcessor` を含めない限り、トレースが OpenAI バックエンドに送信されないことを意味します。


## 非 OpenAI モデルでのトレーシング

トレーシングを無効化せずに OpenAI Traces ダッシュボードで無料トレーシングを有効化するために、非 OpenAI モデルで OpenAI API キーを使用できます。アダプター選択と設定時の注意点については、Models ガイドの [Third-party adapters](models/index.md#third-party-adapters) セクションを参照してください。

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

単一実行にのみ別のトレーシングキーが必要な場合は、グローバルエクスポーターを変更する代わりに `RunConfig` で渡してください。

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(tracing={"api_key": "sk-tracing-123"}),
)
```

## 追加ノート
- Openai Traces ダッシュボードで無料トレースを確認できます。


## エコシステム統合

以下のコミュニティおよびベンダー統合は、OpenAI Agents SDK のトレーシングサーフェスをサポートしています。

### 外部トレーシングプロセッサー一覧

-   [Weights & Biases](https://weave-docs.wandb.ai/guides/integrations/openai_agents)
-   [Arize-Phoenix](https://docs.arize.com/phoenix/tracing/integrations-tracing/openai-agents-sdk)
-   [Future AGI](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/openai_agents)
-   [MLflow (self-hosted/OSS)](https://mlflow.org/docs/latest/tracing/integrations/openai-agent)
-   [MLflow (Databricks hosted)](https://docs.databricks.com/aws/en/mlflow/mlflow-tracing#-automatic-tracing)
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