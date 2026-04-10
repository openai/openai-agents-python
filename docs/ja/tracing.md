---
search:
  exclude: true
---
# トレーシング

Agents SDK には組み込みのトレーシングが含まれており、エージェント実行中のイベント（ LLM 生成、ツール呼び出し、ハンドオフ、ガードレール、さらに発生したカスタムイベント）を包括的に記録します。[Traces ダッシュボード](https://platform.openai.com/traces) を使用すると、開発中および本番環境でワークフローのデバッグ、可視化、監視ができます。

!!!note

    トレーシングはデフォルトで有効です。無効化する一般的な方法は 3 つあります。

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定して、グローバルにトレーシングを無効化できます
    2. [`set_tracing_disabled(True)`][agents.set_tracing_disabled] を使ってコード上でグローバルにトレーシングを無効化できます
    3. 単一の実行で [`agents.run.RunConfig.tracing_disabled`][] を `True` に設定して、トレーシングを無効化できます

***OpenAI の API を使用し、 Zero Data Retention ( ZDR ) ポリシーの下で運用している組織では、トレーシングは利用できません。***

## トレースとスパン

-   **トレース**は「ワークフロー」の単一のエンドツーエンド操作を表します。スパンで構成されます。トレースには次のプロパティがあります。
    -   `workflow_name`: 論理的なワークフローまたはアプリです。たとえば「Code generation」や「Customer service」です。
    -   `trace_id`: トレースの一意な ID です。指定しない場合は自動生成されます。形式は `trace_<32_alphanumeric>` である必要があります。
    -   `group_id`: 任意のグループ ID です。同じ会話からの複数のトレースを関連付けるために使います。たとえばチャットスレッド ID を使用できます。
    -   `disabled`: True の場合、トレースは記録されません。
    -   `metadata`: トレースの任意のメタデータです。
-   **スパン**は開始時刻と終了時刻を持つ操作を表します。スパンには次があります。
    -   `started_at` と `ended_at` のタイムスタンプ。
    -   `trace_id`: 所属するトレースを表します
    -   `parent_id`: このスパンの親スパン（存在する場合）を指します
    -   `span_data`: スパンに関する情報です。たとえば `AgentSpanData` にはエージェント情報、`GenerationSpanData` には LLM 生成情報が含まれます。

## デフォルトトレーシング

デフォルトでは、 SDK は次をトレースします。

-   `Runner.{run, run_sync, run_streamed}()` 全体が `trace()` でラップされます。
-   エージェントが実行されるたびに `agent_span()` でラップされます
-   LLM 生成は `generation_span()` でラップされます
-   関数ツール呼び出しはそれぞれ `function_span()` でラップされます
-   ガードレールは `guardrail_span()` でラップされます
-   ハンドオフは `handoff_span()` でラップされます
-   音声入力（ speech-to-text ）は `transcription_span()` でラップされます
-   音声出力（ text-to-speech ）は `speech_span()` でラップされます
-   関連する音声スパンは `speech_group_span()` の子になる場合があります

デフォルトでは、トレース名は「Agent workflow」です。`trace` を使う場合はこの名前を設定できます。また、[`RunConfig`][agents.run.RunConfig] で名前やその他のプロパティを設定することもできます。

さらに、[カスタムトレースプロセッサー](#custom-tracing-processors) を設定して、トレースを別の送信先にプッシュできます（置き換えまたは副次的な送信先として）。

## 長時間稼働ワーカーと即時エクスポート

デフォルトの [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] は、数秒ごとにバックグラウンドでトレースをエクスポートします。あるいは、メモリ内キューがサイズのしきい値に達した場合はより早くエクスポートされ、プロセス終了時には最終フラッシュも実行されます。Celery、RQ、Dramatiq、FastAPI のバックグラウンドタスクのような長時間稼働ワーカーでは、通常は追加コードなしでトレースが自動的にエクスポートされますが、各ジョブ終了直後には Traces ダッシュボードにすぐ表示されない場合があります。

作業単位の終了時に即時配信を保証したい場合は、トレースコンテキスト終了後に [`flush_traces()`][agents.tracing.flush_traces] を呼び出してください。

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

[`flush_traces()`][agents.tracing.flush_traces] は、現在バッファされたトレースとスパンがエクスポートされるまでブロックするため、部分的に構築されたトレースをフラッシュしないよう、`trace()` が閉じた後に呼び出してください。デフォルトのエクスポート遅延で問題ない場合は、この呼び出しを省略できます。

## 高レベルトレース

場合によっては、`run()` の複数回の呼び出しを 1 つのトレースに含めたいことがあります。これは、コード全体を `trace()` でラップすることで実現できます。

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

1. `Runner.run` の 2 回の呼び出しは `with trace()` でラップされているため、2 つのトレースを作成するのではなく、個々の実行が全体のトレースの一部になります。

## トレース作成

[`trace()`][agents.tracing.trace] 関数を使ってトレースを作成できます。トレースは開始と終了が必要です。方法は 2 つあります。

1. **推奨**: トレースをコンテキストマネージャーとして使用します。つまり `with trace(...) as my_trace` です。これにより適切なタイミングで自動的に開始・終了されます。
2. [`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を手動で呼び出すこともできます。

現在のトレースは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で追跡されます。これは並行処理でも自動的に動作することを意味します。トレースを手動で開始/終了する場合は、現在のトレースを更新するために `start()` / `finish()` に `mark_as_current` と `reset_current` を渡す必要があります。

## スパン作成

さまざまな [`*_span()`][agents.tracing.create] メソッドを使ってスパンを作成できます。一般には、スパンを手動で作成する必要はありません。カスタムスパン情報を追跡するために [`custom_span()`][agents.tracing.custom_span] 関数が利用できます。

スパンは自動的に現在のトレースの一部となり、最も近い現在のスパンの下にネストされます。これは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で追跡されます。

## 機密データ

一部のスパンは、機密性のある可能性があるデータを取得する場合があります。

`generation_span()` は LLM 生成の入力/出力を保存し、`function_span()` は関数呼び出しの入力/出力を保存します。これらには機密データが含まれる可能性があるため、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] でそのデータの取得を無効化できます。

同様に、音声スパンにはデフォルトで入力および出力音声の base64 エンコード PCM データが含まれます。[`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] を設定すると、この音声データの取得を無効化できます。

デフォルトで `trace_include_sensitive_data` は `True` です。アプリ実行前に `OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA` 環境変数を `true/1` または `false/0` に設定することで、コードを書かずにデフォルトを設定できます。

## カスタムトレースプロセッサー

トレーシングの高レベルアーキテクチャは次のとおりです。

-   初期化時に、トレース作成を担当するグローバルな [`TraceProvider`][agents.tracing.setup.TraceProvider] を作成します。
-   `TraceProvider` に [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] を設定し、[`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] へトレース/スパンをバッチ送信します。`BackendSpanExporter` はスパンとトレースを OpenAI バックエンドにバッチでエクスポートします。

このデフォルト設定をカスタマイズし、トレースを代替または追加のバックエンドに送信したり、エクスポーター動作を変更したりするには、2 つの方法があります。

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] を使うと、準備でき次第トレースとスパンを受け取る **追加の** トレースプロセッサーを追加できます。これにより、OpenAI バックエンドへの送信に加えて独自処理を実行できます。
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] を使うと、デフォルトのプロセッサーを独自のトレースプロセッサーで **置き換え** できます。これは、そうした処理を行う `TracingProcessor` を含めない限り、トレースが OpenAI バックエンドに送信されないことを意味します。


## 非 OpenAI モデルでのトレーシング

非 OpenAI モデルでも OpenAI API キーを使うことで、トレーシングを無効化せずに OpenAI Traces ダッシュボードで無料トレーシングを有効にできます。アダプターの選択と設定上の注意点については、Models ガイドの [Third-party adapters](models/index.md#third-party-adapters) セクションを参照してください。

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

単一の実行でのみ別のトレーシングキーが必要な場合は、グローバルエクスポーターを変更する代わりに `RunConfig` 経由で渡してください。

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(tracing={"api_key": "sk-tracing-123"}),
)
```

## 追加の注意
- Openai Traces ダッシュボードで無料トレースを表示できます。


## エコシステム統合

以下のコミュニティおよびベンダー統合は、OpenAI Agents SDK のトレーシング機能をサポートしています。

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
-   [HoneyHive](https://docs.honeyhive.ai/v2/integrations/openai-agents)
-   [Asqav](https://www.asqav.com/docs/integrations#openai-agents)