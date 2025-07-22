---
search:
  exclude: true
---
# トレーシング

Agents SDK にはトレーシング機能が組み込まれており、エージェント実行中に発生する LLM 生成、ツール呼び出し、ハンドオフ、ガードレール、カスタムイベントなどの詳細なイベント記録を収集します。[Traces ダッシュボード](https://platform.openai.com/traces) を使用すると、開発中や本番環境でワークフローをデバッグ、可視化、監視できます。

!!!note

    トレーシングはデフォルトで有効になっています。無効化する方法は 2 つあります。

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定してグローバルに無効化する  
    2. 単一の実行で無効化する場合は [`agents.run.RunConfig.tracing_disabled`][] を `True` に設定する

***OpenAI の API を Zero Data Retention (ZDR) ポリシーで利用している組織では、トレーシングは利用できません。***

## トレースとスパン

- **トレース** は 1 つのワークフローのエンドツーエンド操作を表します。トレースは複数のスパンで構成されます。トレースのプロパティは次のとおりです。  
    - `workflow_name`: 論理的なワークフローまたはアプリ名。例: 「Code generation」や「Customer service」  
    - `trace_id`: トレースの一意 ID。渡さなかった場合は自動生成されます。形式は `trace_<32_alphanumeric>` である必要があります。  
    - `group_id`: オプションのグループ ID。同じ会話からの複数トレースをリンクするために使用します。例としてチャットスレッド ID など。  
    - `disabled`: `True` の場合、このトレースは記録されません。  
    - `metadata`: トレースに付与するオプションメタデータ。  
- **スパン** は開始時刻と終了時刻を持つ操作を表します。スパンのプロパティは次のとおりです。  
    - `started_at` と `ended_at` タイムスタンプ  
    - 所属するトレースを示す `trace_id`  
    - 親スパンを指す `parent_id` (存在する場合)  
    - スパンに関する情報を含む `span_data`。例: `AgentSpanData` はエージェント情報、`GenerationSpanData` は LLM 生成情報など。  

## デフォルトのトレーシング

デフォルトでは、SDK は次をトレースします。

- `Runner.{run, run_sync, run_streamed}()` 全体を `trace()` でラップ  
- エージェントが実行されるたびに `agent_span()` でラップ  
- LLM 生成を `generation_span()` でラップ  
- 関数ツール呼び出しをそれぞれ `function_span()` でラップ  
- ガードレールを `guardrail_span()` でラップ  
- ハンドオフを `handoff_span()` でラップ  
- 音声入力 (speech-to-text) を `transcription_span()` でラップ  
- 音声出力 (text-to-speech) を `speech_span()` でラップ  
- 関連する音声スパンは `speech_group_span()` の下にネストされることがあります  

デフォルトではトレース名は「Agent trace」です。`trace` を使用して名前を設定するか、[`RunConfig`][agents.run.RunConfig] で名前やその他のプロパティを設定できます。

さらに、[カスタムトレースプロセッサ](#custom-tracing-processors) を設定して、トレースを別の送信先にプッシュすることもできます (置き換えまたは追加送信先として)。

## 上位レベルのトレース

複数回の `run()` 呼び出しを 1 つのトレースにまとめたい場合があります。これを行うには、コード全体を `trace()` でラップします。

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

1. `Runner.run` への 2 回の呼び出しが `with trace()` 内にラップされているため、個々の実行は 2 つのトレースを作成せず、全体トレースの一部になります。

## トレースの作成

[`trace()`][agents.tracing.trace] 関数を使用してトレースを作成できます。トレースは開始と終了が必要です。方法は 2 つあります。

1. **推奨**: コンテキストマネージャとして使用する (例: `with trace(...) as my_trace`)。これにより開始と終了が自動化されます。  
2. [`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を手動で呼び出すことも可能です。  

現在のトレースは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で管理されます。これにより並行処理でも自動的に機能します。トレースを手動で開始／終了する場合は、`start()`／`finish()` に `mark_as_current` と `reset_current` を渡して現在のトレースを更新してください。

## スパンの作成

各種 [`*_span()`][agents.tracing.create] メソッドを使ってスパンを作成できます。通常は手動でスパンを作成する必要はありません。カスタムスパン情報を追跡するための [`custom_span()`][agents.tracing.custom_span] も用意されています。

スパンは自動的に現在のトレースに含まれ、最も近い現在のスパンの下にネストされます。これも Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で管理されます。

## 機微なデータ

一部のスパンは機微なデータを含む可能性があります。

`generation_span()` は LLM 生成の入出力を、`function_span()` は関数呼び出しの入出力を保存します。これらに機微なデータが含まれる場合があるため、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] でデータの取得を無効化できます。

同様に、オーディオスパンはデフォルトで入力・出力オーディオの base64 エンコード PCM データを含みます。[`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] を設定することで、このオーディオデータの取得を無効化できます。

## カスタムトレーシングプロセッサ

トレーシングの高レベルアーキテクチャは次のとおりです。

- 初期化時にグローバルな [`TraceProvider`][agents.tracing.setup.TraceProvider] を作成し、トレースの生成を担当します。  
- `TraceProvider` を [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] で構成し、スパンとトレースをバッチで [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] に送信し、OpenAI バックエンドへエクスポートします。  

デフォルト設定をカスタマイズして別のバックエンドへ送信したり、エクスポーター動作を変更したりする場合は、次の 2 つの方法があります。

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] で **追加の** トレースプロセッサを登録し、トレース／スパンを受け取って独自処理を行いつつ OpenAI バックエンドにも送信する  
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] でデフォルトプロセッサを **置き換え**、独自のプロセッサのみを使用する (OpenAI バックエンドへ送信したい場合は、その機能を持つ `TracingProcessor` を含める必要があります)  

## 外部トレーシングプロセッサ一覧

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