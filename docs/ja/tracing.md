---
search:
  exclude: true
---
# トレーシング

Agents SDK にはトレーシングが組み込まれており、エージェント実行中に発生する LLM 生成、ツール呼び出し、ハンドオフ、ガードレール、さらにカスタムイベントまで、包括的なイベント履歴を収集します。 [Traces ダッシュボード](https://platform.openai.com/traces) を利用すると、開発時および本番環境でワークフローをデバッグ・可視化・監視できます。

!!!note

    トレーシングはデフォルトで有効です。無効化する方法は 2 つあります。

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定してトレーシングをグローバルに無効化する  
    2. 1 回の実行のみ無効化する場合は [`agents.run.RunConfig.tracing_disabled`][] を `True` に設定する

***OpenAI の API を Zero Data Retention (ZDR) ポリシーで利用している組織では、トレーシングは利用できません。***

## トレースと Span

- **トレース**: 「ワークフロー」全体のエンドツーエンドの処理を表します。複数の Span で構成され、以下のプロパティを持ちます。  
    - `workflow_name`: 論理的なワークフロー／アプリ名。例:「コード生成」「カスタマーサービス」  
    - `trace_id`: トレースの一意 ID。未指定の場合は自動生成されます。フォーマットは `trace_<32_alphanumeric>`  
    - `group_id`: オプションのグループ ID。同一会話からの複数トレースを関連付ける際に使用します（例: チャットスレッド ID）  
    - `disabled`: `True` の場合、このトレースは記録されません  
    - `metadata`: トレースに付与するオプションのメタデータ  
- **Span**: 開始時刻と終了時刻を持つ処理単位を表します。  
    - `started_at` と `ended_at` タイムスタンプ  
    - 所属するトレースを示す `trace_id`  
    - 親 Span を指す `parent_id`（存在する場合）  
    - Span に関する情報を格納する `span_data`。例: `AgentSpanData` はエージェント情報、`GenerationSpanData` は LLM 生成情報など

## 既定のトレーシング

デフォルトで SDK は以下をトレースします。

- `Runner.{run, run_sync, run_streamed}()` 全体を `trace()` でラップ
- エージェント実行ごとに `agent_span()` でラップ
- LLM 生成を `generation_span()` でラップ
- 関数ツール呼び出しを `function_span()` でラップ
- ガードレールを `guardrail_span()` でラップ
- ハンドオフを `handoff_span()` でラップ
- 音声入力（音声→テキスト）を `transcription_span()` でラップ
- 音声出力（テキスト→音声）を `speech_span()` でラップ
- 関連する音声 Span は `speech_group_span()` の下に階層化される場合があります

デフォルトではトレース名は「Agent workflow」です。`trace` を使用すればこの名前を設定でき、[`RunConfig`][agents.run.RunConfig] で名前やその他プロパティを設定することも可能です。

さらに、[カスタムトレーシングプロセッサー](#custom-tracing-processors) を設定することで、トレースを別の送信先へ（置き換えまたは追加で）送ることができます。

## 高レベルトレース

複数回の `run()` 呼び出しを 1 つのトレースにまとめたい場合は、コード全体を `trace()` でラップします。

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

1. `Runner.run` への 2 回の呼び出しが `with trace()` に包まれているため、それぞれが個別のトレースを生成する代わりに 1 つのトレースにまとめられます。

## トレースの作成

トレースは [`trace()`][agents.tracing.trace] 関数で作成できます。開始と終了が必要で、方法は 2 通りあります。

1. **推奨**: コンテキストマネージャーとして使用する（例: `with trace(...) as my_trace`）。開始と終了が自動で行われます。  
2. 手動で [`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を呼び出す

現在のトレースは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で管理されるため、並行処理でも自動で機能します。手動で開始／終了する場合は `start()` と `finish()` に `mark_as_current` と `reset_current` を渡して現在のトレースを更新する必要があります。

## Span の作成

各種 [`*_span()`][agents.tracing.create] メソッドで Span を作成できますが、通常は手動で作成する必要はありません。カスタム Span 情報を追跡するために [`custom_span()`][agents.tracing.custom_span] も利用できます。

Span は自動的に現在のトレースに属し、最も近い現在の Span の下にネストされます。こちらも Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で管理されます。

## 機微なデータ

一部の Span は機微なデータを含む可能性があります。

`generation_span()` は LLM 生成の入力／出力を、`function_span()` は関数呼び出しの入力／出力を保存します。機微なデータが含まれる場合があるため、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] でこれらのデータ取得を無効化できます。

同様に、Audio Span にはデフォルトで入出力音声の Base64 エンコード PCM データが含まれます。[`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] を設定して音声データの取得を無効化できます。

## カスタムトレーシングプロセッサー

トレーシングのハイレベル構成は次のとおりです。

- 初期化時にグローバルな [`TraceProvider`][agents.tracing.setup.TraceProvider] を作成し、トレースの生成を担当します。  
- `TraceProvider` に [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] を設定し、トレース／Span をバッチで [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] へ送信します。Exporter は OpenAI バックエンドへバッチ送信を行います。

このデフォルト構成をカスタマイズして、別の送信先へトレースを送る、または Exporter の動作を変更する場合は以下の 2 つの方法があります。

1. [`add_trace_processor()`][agents.tracing.add_trace_processor]  
   既存構成に **追加** のトレースプロセッサーを登録します。OpenAI バックエンドへの送信に加えて独自処理を行えます。  
2. [`set_trace_processors()`][agents.tracing.set_trace_processors]  
   既定のプロセッサーを置き換え、独自のトレースプロセッサーを **設定** します。OpenAI バックエンドへ送信したい場合は、その機能を持つ `TracingProcessor` を含める必要があります。

## 非 OpenAI モデルでのトレーシング

OpenAI API キーを使って非 OpenAI モデルを呼び出す場合でも、トレーシングを無効化せずに OpenAI Traces ダッシュボードで無償トレースを有効化できます。

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

## 備考
- 無償トレースは OpenAI Traces ダッシュボードで確認できます。

## 外部トレーシングプロセッサー一覧

- [Weights & Biases](https://weave-docs.wandb.ai/guides/integrations/openai_agents)
- [Arize-Phoenix](https://docs.arize.com/phoenix/tracing/integrations-tracing/openai-agents-sdk)
- [Future AGI](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/openai_agents)
- [MLflow (self-hosted/OSS)](https://mlflow.org/docs/latest/tracing/integrations/openai-agent)
- [MLflow (Databricks hosted)](https://docs.databricks.com/aws/en/mlflow/mlflow-tracing#-automatic-tracing)
- [Braintrust](https://braintrust.dev/docs/guides/traces/integrations#openai-agents-sdk)
- [Pydantic Logfire](https://logfire.pydantic.dev/docs/integrations/llms/openai/#openai-agents)
- [AgentOps](https://docs.agentops.ai/v1/integrations/agentssdk)
- [Scorecard](https://docs.scorecard.io/docs/documentation/features/tracing#openai-agents-sdk-integration)
- [Keywords AI](https://docs.keywordsai.co/integration/development-frameworks/openai-agent)
- [LangSmith](https://docs.smith.langchain.com/observability/how_to_guides/trace_with_openai_agents_sdk)
- [Maxim AI](https://www.getmaxim.ai/docs/observe/integrations/openai-agents-sdk)
- [Comet Opik](https://www.comet.com/docs/opik/tracing/integrations/openai_agents)
- [Langfuse](https://langfuse.com/docs/integrations/openaiagentssdk/openai-agents)
- [Langtrace](https://docs.langtrace.ai/supported-integrations/llm-frameworks/openai-agents-sdk)
- [Okahu-Monocle](https://github.com/monocle2ai/monocle)
- [Galileo](https://v2docs.galileo.ai/integrations/openai-agent-integration#openai-agent-integration)
- [Portkey AI](https://portkey.ai/docs/integrations/agents/openai-agents)
- [LangDB AI](https://docs.langdb.ai/getting-started/working-with-agent-frameworks/working-with-openai-agents-sdk)