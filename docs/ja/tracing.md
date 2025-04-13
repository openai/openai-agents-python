# トレーシング

Agents SDK には組み込みのトレーシング機能があり、エージェント実行中のイベント（LLM の生成、ツール呼び出し、ハンドオフ、ガードレール、カスタムイベントなど）の包括的な記録を収集します。[Traces ダッシュボード](https://platform.openai.com/traces)を使用して、開発中および本番環境でワークフローをデバッグ、可視化、監視できます。

!!!note

    トレーシングはデフォルトで有効です。トレーシングを無効にする方法は2つあります:

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定して、グローバルにトレーシングを無効にすることができます。
    2. [`agents.run.RunConfig.tracing_disabled`][] を `True` に設定して、単一の実行でトレーシングを無効にすることができます。

***OpenAI の API を使用している Zero Data Retention (ZDR) ポリシーの下で運営されている組織では、トレーシングは利用できません。***

## トレースとスパン

- **トレース** は「ワークフロー」の単一のエンドツーエンドの操作を表します。スパンで構成されています。トレースには以下のプロパティがあります:
  - `workflow_name`: 論理的なワークフローまたはアプリです。例: "Code generation" や "Customer service"。
  - `trace_id`: トレースの一意の ID。指定しない場合は自動生成されます。形式は `trace_<32_alphanumeric>` でなければなりません。
  - `group_id`: 同じ会話からの複数のトレースをリンクするためのオプションのグループ ID。例: チャットスレッド ID。
  - `disabled`: True の場合、トレースは記録されません。
  - `metadata`: トレースのオプションのメタデータ。
- **スパン** は開始時間と終了時間を持つ操作を表します。スパンには以下があります:
  - `started_at` と `ended_at` のタイムスタンプ。
  - 所属するトレースを表す `trace_id`
  - 親スパンを指す `parent_id`（ある場合）
  - スパンに関する情報を含む `span_data`。例: `AgentSpanData` はエージェントに関する情報を含み、`GenerationSpanData` は LLM の生成に関する情報を含みます。

## デフォルトのトレーシング

デフォルトで、SDK は以下をトレースします:

- `Runner.{run, run_sync, run_streamed}()` 全体が `trace()` でラップされます。
- エージェントが実行されるたびに、`agent_span()` でラップされます。
- LLM の生成は `generation_span()` でラップされます。
- 関数ツールの呼び出しはそれぞれ `function_span()` でラップされます。
- ガードレールは `guardrail_span()` でラップされます。
- ハンドオフは `handoff_span()` でラップされます。
- 音声入力（音声からテキスト）は `transcription_span()` でラップされます。
- 音声出力（テキストから音声）は `speech_span()` でラップされます。
- 関連する音声スパンは `speech_group_span()` の下に配置されることがあります。

デフォルトでは、トレースは "Agent trace" と名付けられています。`trace` を使用する場合、この名前を設定することができ、[`RunConfig`][agents.run.RunConfig] で名前やその他のプロパティを設定できます。

さらに、[カスタムトレースプロセッサー](#custom-tracing-processors)を設定して、トレースを他の宛先に送信することもできます（置き換えまたは二次的な宛先として）。

## 高レベルのトレース

時には、複数の `run()` 呼び出しを単一のトレースの一部にしたいことがあります。これを行うには、コード全体を `trace()` でラップします。

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

1. `Runner.run` への2つの呼び出しが `with trace()` でラップされているため、個々の実行は2つのトレースを作成するのではなく、全体のトレースの一部になります。

## トレースの作成

[`trace()`][agents.tracing.trace] 関数を使用してトレースを作成できます。トレースは開始と終了が必要です。以下の2つの方法があります:

1. **推奨**: トレースをコンテキストマネージャーとして使用します。つまり、`with trace(...) as my_trace` とします。これにより、トレースが自動的に適切なタイミングで開始および終了します。
2. 手動で [`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を呼び出すこともできます。

現在のトレースは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) を介して追跡されます。これにより、並行処理でも自動的に機能します。トレースを手動で開始/終了する場合は、`start()`/`finish()` に `mark_as_current` と `reset_current` を渡して現在のトレースを更新する必要があります。

## スパンの作成

さまざまな [`*_span()`][agents.tracing.create] メソッドを使用してスパンを作成できます。一般的に、スパンを手動で作成する必要はありません。カスタムスパン情報を追跡するための [`custom_span()`][agents.tracing.custom_span] 関数が利用可能です。

スパンは自動的に現在のトレースの一部となり、Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) を介して追跡される最も近い現在のスパンの下にネストされます。

## センシティブデータ

特定のスパンは潜在的にセンシティブなデータをキャプチャすることがあります。

`generation_span()` は LLM 生成の入力/出力を保存し、`function_span()` は関数呼び出しの入力/出力を保存します。これらにはセンシティブなデータが含まれる可能性があるため、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] を使用してそのデータのキャプチャを無効にすることができます。

同様に、音声スパンにはデフォルトで入力および出力音声の base64 エンコードされた PCM データが含まれます。この音声データのキャプチャを無効にするには、[`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] を設定します。

## カスタムトレーシングプロセッサー

トレーシングの高レベルアーキテクチャは次のとおりです:

- 初期化時に、トレースを作成する責任を持つグローバルな [`TraceProvider`][agents.tracing.setup.TraceProvider] を作成します。
- `TraceProvider` を [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] で構成し、トレース/スパンをバッチで [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] に送信し、OpenAI バックエンドにバッチでスパンとトレースをエクスポートします。

このデフォルトのセットアップをカスタマイズして、トレースを代替または追加のバックエンドに送信したり、エクスポーターの動作を変更したりするには、次の2つのオプションがあります:

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] を使用して、トレースとスパンが準備できたときに受け取る**追加の**トレースプロセッサーを追加できます。これにより、OpenAI のバックエンドにトレースを送信することに加えて、独自の処理を行うことができます。
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] を使用して、デフォルトのプロセッサーを独自のトレースプロセッサーに**置き換える**ことができます。これにより、`TracingProcessor` を含めない限り、トレースは OpenAI バックエンドに送信されません。

## 外部トレーシングプロセッサーリスト

- [Weights & Biases](https://weave-docs.wandb.ai/guides/integrations/openai_agents)
- [Arize-Phoenix](https://docs.arize.com/phoenix/tracing/integrations-tracing/openai-agents-sdk)
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