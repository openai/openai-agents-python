---
search:
  exclude: true
---
# トレーシング

Agents SDK にはトレーシング機能が組み込まれており、エージェント実行中に発生する LLM 生成、ツール呼び出し、ハンドオフ、ガードレール、さらにはカスタムイベントまで、包括的なイベント記録を収集します。開発中および本番環境では、[Traces ダッシュボード](https://platform.openai.com/traces) を使用してワークフローをデバッグ、可視化、モニタリングできます。

!!!note

    トレーシングはデフォルトで有効になっています。無効化する方法は 2 つあります。

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定してグローバルに無効化する  
    2. 1 回の実行に対してのみ [`agents.run.RunConfig.tracing_disabled`][] を `True` に設定する

***OpenAI の API を Zero Data Retention (ZDR) ポリシー下で利用する組織では、トレーシングを使用できません。***

## トレースとスパン

- **トレース** は 1 回のワークフロー全体のエンドツーエンド操作を表します。トレースはスパンで構成され、以下のプロパティを持ちます。  
    - `workflow_name`: 論理的なワークフローまたはアプリ名。例: "Code generation" や "Customer service"  
    - `trace_id`: トレースの一意 ID。指定しない場合は自動生成されます。形式は `trace_<32_alphanumeric>`  
    - `group_id`: 同じ会話からの複数トレースをリンクするためのオプション ID。例としてチャットスレッド ID など  
    - `disabled`: `True` の場合、このトレースは記録されません  
    - `metadata`: トレースに付与するオプションのメタデータ
- **スパン** は開始時刻と終了時刻を持つ操作を表します。スパンは以下を持ちます。  
    - `started_at` と `ended_at` のタイムスタンプ  
    - 所属するトレースを示す `trace_id`  
    - 親スパンを指す `parent_id` (存在する場合)  
    - スパンに関する情報を保持する `span_data`。例: `AgentSpanData` はエージェント情報、`GenerationSpanData` は LLM 生成情報など

## デフォルトのトレーシング

デフォルトでは、SDK は次をトレースします。

- `Runner.{run, run_sync, run_streamed}()` 全体を `trace()` でラップ
- エージェント実行ごとに `agent_span()` でラップ
- LLM 生成を `generation_span()` でラップ
- 関数ツール呼び出しを `function_span()` でラップ
- ガードレールを `guardrail_span()` でラップ
- ハンドオフを `handoff_span()` でラップ
- 音声入力 (speech-to-text) を `transcription_span()` でラップ
- 音声出力 (text-to-speech) を `speech_span()` でラップ
- 関連する音声スパンは `speech_group_span()` の下にネストされる場合があります

デフォルトでは、トレース名は "Agent workflow" です。`trace` を使用して名前を設定するか、[`RunConfig`][agents.run.RunConfig] で名前やその他プロパティを構成できます。

さらに、[カスタムトレーシングプロセッサー](#custom-tracing-processors) を設定して、別の送信先へトレースをプッシュする (置換または追加送信) ことも可能です。

## 上位レベルのトレース

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

1. `with trace()` で 2 回の `Runner.run` 呼び出しをラップしているため、それぞれが個別にトレースを生成するのではなく、全体として 1 つのトレースになります。

## トレースの作成

トレースは [`trace()`][agents.tracing.trace] 関数で作成できます。トレースは開始と終了が必要で、以下の 2 通りがあります。

1. **推奨**: `with trace(...) as my_trace` のようにコンテキストマネージャとして使用する。開始と終了が自動で行われます。  
2. 手動で [`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を呼び出す。

現在のトレースは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で管理されるため、並行処理でも自動的に機能します。手動で開始/終了する場合は、`start()`/`finish()` に `mark_as_current` と `reset_current` を渡して現在のトレースを更新してください。

## スパンの作成

各種 [`*_span()`][agents.tracing.create] メソッドでスパンを作成できますが、通常は手動で作成する必要はありません。カスタム情報を追跡したい場合は [`custom_span()`][agents.tracing.custom_span] を使用できます。

スパンは自動的に現在のトレースに含まれ、Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で管理される最も近い現在のスパンの下にネストされます。

## センシティブデータ

一部のスパンには機微なデータが含まれる場合があります。

`generation_span()` は LLM 生成の入出力を、`function_span()` は関数呼び出しの入出力を保存します。機微データを含む可能性があるため、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] でデータの記録を無効化できます。

同様に、音声スパンはデフォルトで入力・出力音声の base64 エンコード PCM データを含みます。音声データの記録は [`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] で無効化できます。

## カスタムトレーシングプロセッサー

トレーシングの高レベル構成は以下の通りです。

- 初期化時にグローバル [`TraceProvider`][agents.tracing.setup.TraceProvider] を生成し、トレースの作成を担当します。  
- `TraceProvider` は [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] を用いてトレース/スパンをバッチ送信し、[`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] がそれらを OpenAI バックエンドへバッチエクスポートします。

デフォルト設定をカスタマイズし、他のバックエンドへトレースを送信したりエクスポーターの動作を変更したりするには、次の 2 つの方法があります。

1. [`add_trace_processor()`][agents.tracing.add_trace_processor]: **追加** のトレースプロセッサーを登録し、トレース/スパンを受け取って独自処理を行います。OpenAI バックエンドへの送信に加えて処理を追加できます。  
2. [`set_trace_processors()`][agents.tracing.set_trace_processors]: 既定のプロセッサーを **置換** します。OpenAI バックエンドへ送信したい場合は、その機能を持つ `TracingProcessor` を含める必要があります。

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