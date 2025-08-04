---
search:
  exclude: true
---
# トレーシング

Agents SDK には組み込みのトレーシング機能があり、エージェント実行中に発生する LLM 生成、ツール呼び出し、ハンドオフ、ガードレール、さらにカスタムイベントまでを包括的に記録します。 [Traces ダッシュボード](https://platform.openai.com/traces) を使用すると、開発中および本番環境でワークフローをデバッグ・可視化・監視できます。

!!!note

    トレーシングはデフォルトで有効になっています。無効化する方法は 2 つあります。

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定してグローバルに無効化する  
    2. 1 回の実行のみ無効化する場合は [`agents.run.RunConfig.tracing_disabled`][] を `True` に設定する

***OpenAI の API を ZDR (Zero Data Retention) ポリシーで利用している組織では、トレーシングは利用できません。***

## トレースとスパン

- **トレース**: 1 回の「ワークフロー」のエンドツーエンドの操作を表します。トレースは複数のスパンで構成され、次のプロパティを持ちます。  
    - `workflow_name`: 論理的なワークフローまたはアプリ名。例: "Code generation" や "Customer service"  
    - `trace_id`: トレースの一意 ID。渡さなかった場合は自動生成されます。形式は `trace_<32_alphanumeric>`  
    - `group_id`: 同一の会話で発生する複数トレースを結び付けるオプションのグループ ID (例: チャットスレッド ID)  
    - `disabled`: True の場合、このトレースは記録されません  
    - `metadata`: トレースに付随するオプションのメタデータ  
- **スパン**: 開始時刻と終了時刻を持つ操作を表します。スパンは次を持ちます。  
    - `started_at` と `ended_at` タイムスタンプ  
    - 属するトレースを示す `trace_id`  
    - 親スパンを指す `parent_id` (存在する場合)  
    - スパン情報を含む `span_data`。例えば `AgentSpanData` はエージェント情報、`GenerationSpanData` は LLM 生成情報など  

## デフォルトのトレーシング

デフォルトでは、SDK は以下をトレースします。

- `Runner.{run, run_sync, run_streamed}()` 全体が `trace()` にラップされる  
- エージェントが実行されるたびに `agent_span()` にラップされる  
- LLM 生成は `generation_span()` にラップされる  
- 関数ツール呼び出しごとに `function_span()` にラップされる  
- ガードレールは `guardrail_span()` にラップされる  
- ハンドオフは `handoff_span()` にラップされる  
- 音声入力 (speech-to-text) は `transcription_span()` にラップされる  
- 音声出力 (text-to-speech) は `speech_span()` にラップされる  
- 関連する音声スパンは `speech_group_span()` 配下に配置される場合がある  

デフォルトのトレース名は "Agent workflow" です。`trace` を使用して名前を設定するか、[`RunConfig`][agents.run.RunConfig] で名前やその他のプロパティを設定できます。

さらに、[カスタムトレースプロセッサ](#custom-tracing-processors) を設定して、別の送信先へトレースをプッシュする (置き換えまたは追加) こともできます。

## 上位レベルのトレース

複数回の `run()` 呼び出しを 1 つのトレースにまとめたい場合があります。その場合、コード全体を `trace()` でラップしてください。

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

1. 2 回の `Runner.run` 呼び出しが `with trace()` にラップされているため、個別に 2 つのトレースが作成されるのではなく、両方の実行が 1 つのトレースに含まれます。

## トレースの作成

[`trace()`][agents.tracing.trace] 関数を使用してトレースを作成できます。トレースは開始と終了が必要で、方法は 2 つあります。

1. **推奨**: `with trace(...) as my_trace` のようにコンテキストマネージャとして使用する。開始終了が自動化されます。  
2. [`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を手動で呼び出す。  

現在のトレースは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) により追跡されるため、自動的に並行処理と連携します。手動で開始終了する場合は、`start()`/`finish()` に `mark_as_current` と `reset_current` を渡して現在のトレースを更新してください。

## スパンの作成

各種 [`*_span()`][agents.tracing.create] メソッドでスパンを作成できますが、通常は手動で作成する必要はありません。カスタムスパン情報を追跡するための [`custom_span()`][agents.tracing.custom_span] も用意されています。

スパンは自動的に現在のトレースに属し、Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で追跡される最も近い現在のスパンの下にネストされます。

## 機微データ

一部のスパンは機微なデータを含む可能性があります。

`generation_span()` は LLM 生成の入力/出力を、`function_span()` は関数呼び出しの入力/出力を保存します。これらに機微データが含まれる場合があるため、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] でデータの保存を無効化できます。

同様に、オーディオスパンはデフォルトで入力・出力音声の base64 エンコード PCM データを含みます。[`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] を設定してオーディオデータの保存を無効化できます。

## カスタムトレースプロセッサ

トレーシングの高レベル構成は次のとおりです。

- 初期化時にグローバル [`TraceProvider`][agents.tracing.setup.TraceProvider] を作成し、トレース生成を担当させる  
- `TraceProvider` に [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] を設定し、トレース/スパンをバッチで [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] へ送信し、OpenAI バックエンドにエクスポートする  

デフォルト設定をカスタマイズして、別のバックエンドへ送信したりエクスポーターの動作を変更したりするには 2 つの方法があります。

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] を使用して **追加** のトレースプロセッサを登録する。OpenAI バックエンドへの送信に加えて独自処理を行えます。  
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] を使用してデフォルトプロセッサを **置き換え** る。OpenAI バックエンドへ送信したい場合は、その処理を行う `TracingProcessor` を含める必要があります。  

## 非 OpenAI モデルでのトレーシング

OpenAI API キーを使用して非 OpenAI モデルのトレーシングを有効にすると、トレーシングを無効化せずに OpenAI Traces ダッシュボードで無料のトレースを確認できます。

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
- 無料のトレースは OpenAI Traces ダッシュボードで確認できます。

## 外部トレースプロセッサ一覧

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