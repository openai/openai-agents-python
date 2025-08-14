---
search:
  exclude: true
---
# トレーシング

Agents SDK にはトレーシング機能が組み込まれており、エージェント実行中の LLM 生成、ツール呼び出し、ハンドオフ、ガードレール、カスタムイベントなどを包括的に記録します。開発中および本番環境で [Traces ダッシュボード](https://platform.openai.com/traces) を使ってワークフローをデバッグ・可視化・モニタリングできます。

!!!note

    トレーシングはデフォルトで有効です。無効化する方法は 2 つあります：

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定してグローバルに無効化する  
    2. 単一の実行に対しては [`agents.run.RunConfig.tracing_disabled`][] を `True` に設定する

***OpenAI の API を Zero Data Retention (ZDR) ポリシー下で利用している組織では、トレーシングは利用できません。***

## トレースとスパン

- **トレース** は 1 つの「ワークフロー」のエンドツーエンドの操作を表します。複数のスパンで構成されます。トレースには次のプロパティがあります：  
    - `workflow_name`：論理的なワークフローまたはアプリ名。例：`"Code generation"` や `"Customer service"`  
    - `trace_id`：トレースの一意 ID。指定しない場合は自動生成されます。形式は `trace_<32_alphanumeric>`  
    - `group_id`：省略可。同一会話からの複数トレースを結び付けるための ID。たとえばチャットスレッド ID など  
    - `disabled`：`True` の場合、このトレースは記録されません  
    - `metadata`：省略可。トレースに付随するメタデータ  
- **スパン** は開始時刻と終了時刻を持つ操作を表します。スパンには次があります：  
    - `started_at` と `ended_at` タイムスタンプ  
    - 所属するトレースを示す `trace_id`  
    - 親スパンを指す `parent_id`（存在する場合）  
    - スパンに関する情報を含む `span_data`。例：`AgentSpanData` はエージェント情報、`GenerationSpanData` は LLM 生成情報など  

## デフォルトのトレーシング

デフォルトでは SDK が以下をトレースします：

- `Runner.{run, run_sync, run_streamed}()` 全体を `trace()` でラップ  
- エージェント実行ごとに `agent_span()` でラップ  
- LLM 生成は `generation_span()` でラップ  
- 関数ツール呼び出しは `function_span()` でラップ  
- ガードレールは `guardrail_span()` でラップ  
- ハンドオフは `handoff_span()` でラップ  
- 音声入力（音声→テキスト）は `transcription_span()` でラップ  
- 音声出力（テキスト→音声）は `speech_span()` でラップ  
- 関連する音声スパンは `speech_group_span()` の下にネストされる場合があります  

デフォルトではトレース名は `"Agent workflow"` です。`trace` を使用する際に名前を指定するか、[`RunConfig`][agents.run.RunConfig] で名前やその他プロパティを設定できます。

さらに、[カスタム トレーシングプロセッサー](#custom-tracing-processors) を設定して、別の送信先へトレースをプッシュする（置き換え・追加のいずれも可）ことができます。

## より上位のトレース

複数回の `run()` 呼び出しを 1 つのトレースにまとめたい場合は、コード全体を `trace()` でラップしてください。

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

1. `Runner.run` への 2 回の呼び出しは `with trace()` に包まれているため、個別のトレースを作成せず、全体トレースの一部になります。

## トレースの作成

[`trace()`][agents.tracing.trace] 関数を使ってトレースを作成できます。開始と終了が必要で、方法は 2 通りあります：

1. **推奨**：コンテキストマネージャとして使用し、`with trace(...) as my_trace` とする。適切なタイミングで自動的に開始・終了します。  
2. [`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を手動で呼び出す。

現在のトレースは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で管理されるため、並行処理でも自動的に機能します。手動で開始・終了する場合は、`start()`／`finish()` に `mark_as_current` と `reset_current` を指定して現在のトレースを更新してください。

## スパンの作成

さまざまな [`*_span()`][agents.tracing.create] メソッドでスパンを作成できますが、通常は手動での作成は不要です。カスタム情報を追跡したい場合は [`custom_span()`][agents.tracing.custom_span] を利用できます。

スパンは自動的に現在のトレースに属し、最も近い現在のスパンの下にネストされます。これも Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で管理されます。

## 機微データ

一部のスパンは機微データを含む可能性があります。

`generation_span()` は LLM 生成の入出力を、`function_span()` は関数呼び出しの入出力を格納します。これらに機微データが含まれる場合があるため、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] で記録を無効化できます。

同様に、音声スパンはデフォルトで入出力音声の base64 エンコード PCM データを含みます。[`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] を設定して音声データの記録を無効化できます。

## カスタム トレーシングプロセッサー

トレーシングの高レベル構成は以下のとおりです：

- 初期化時にグローバルな [`TraceProvider`][agents.tracing.setup.TraceProvider] を作成し、これがトレースを生成  
- `TraceProvider` に [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] を設定し、バッチで [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] へスパン／トレースを送信して OpenAI バックエンドにエクスポート

デフォルト設定を変更して他のバックエンドへ送信したり、エクスポーターの挙動を変更したりするには次の 2 つの方法があります：

1. [`add_trace_processor()`][agents.tracing.add_trace_processor]  
   既存の送信に **追加** でトレースプロセッサーを登録し、OpenAI バックエンドへの送信に加えて独自処理を行えます。  
2. [`set_trace_processors()`][agents.tracing.set_trace_processors]  
   デフォルトプロセッサーを **置き換え** て独自プロセッサーのみを使用します。OpenAI バックエンドへ送信したい場合は、その機能を持つ `TracingProcessor` を含めてください。

## 非 OpenAI モデルでのトレーシング

OpenAI API キーを使用して非 OpenAI モデルでもトレーシングを有効化できます。これによりトレーシングを無効化せずに OpenAI Traces ダッシュボードで無料トレースを確認できます。

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

## 注意
- 無料トレースは OpenAI Traces ダッシュボードで閲覧できます。

## 外部トレーシングプロセッサー一覧

- [Weights & Biases](https://weave-docs.wandb.ai/guides/integrations/openai_agents)
- [Arize-Phoenix](https://docs.arize.com/phoenix/tracing/integrations-tracing/openai-agents-sdk)
- [Future AGI](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/openai_agents)
- [MLflow (self-hosted/OSS](https://mlflow.org/docs/latest/tracing/integrations/openai-agent)
- [MLflow (Databricks hosted](https://docs.databricks.com/aws/en/mlflow/mlflow-tracing#-automatic-tracing)
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