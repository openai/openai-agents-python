# トレーシング

Agents SDK にはトレーシング機能が組み込まれており、エージェントの実行中に発生するイベント（ LLM 生成、ツール呼び出し、ハンドオフ、ガードレール、カスタムイベントなど）の包括的な記録を収集します。[Traces ダッシュボード](https://platform.openai.com/traces) を使用することで、開発中や本番環境でワークフローのデバッグ、可視化、監視が可能です。

!!!note

    トレーシングはデフォルトで有効になっています。トレーシングを無効にする方法は 2 つあります:

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定することで、グローバルにトレーシングを無効化できます。
    2. 単一の実行に対しては [`agents.run.RunConfig.tracing_disabled`][] を `True` に設定することで無効化できます。

***OpenAI の API を使用し、Zero Data Retention (ZDR) ポリシーの下で運用している組織では、トレーシングは利用できません。***

## トレースとスパン

-   **トレース** は 1 つの「ワークフロー」のエンドツーエンドの操作を表します。トレースはスパンで構成されます。トレースには以下のプロパティがあります:
    -   `workflow_name`: 論理的なワークフローやアプリ名です。例: "Code generation" や "Customer service" など。
    -   `trace_id`: トレースの一意な ID です。指定しない場合は自動生成されます。フォーマットは `trace_<32_alphanumeric>` である必要があります。
    -   `group_id`: オプションのグループ ID で、同じ会話からの複数のトレースをリンクするために使用します。例: チャットスレッド ID など。
    -   `disabled`: True の場合、このトレースは記録されません。
    -   `metadata`: トレースに付加するオプションのメタデータです。
-   **スパン** は開始時刻と終了時刻を持つ操作を表します。スパンには以下があります:
    -   `started_at` および `ended_at` タイムスタンプ
    -   所属するトレースを示す `trace_id`
    -   このスパンの親スパンを指す `parent_id`（存在する場合）
    -   スパンに関する情報を含む `span_data`。例: `AgentSpanData` はエージェントに関する情報、`GenerationSpanData` は LLM 生成に関する情報など。

## デフォルトのトレーシング

デフォルトでは、 SDK は以下をトレースします:

-   `Runner.{run, run_sync, run_streamed}()` 全体が `trace()` でラップされます。
-   エージェントが実行されるたびに `agent_span()` でラップされます。
-   LLM 生成は `generation_span()` でラップされます。
-   関数ツール呼び出しはそれぞれ `function_span()` でラップされます。
-   ガードレールは `guardrail_span()` でラップされます。
-   ハンドオフは `handoff_span()` でラップされます。
-   音声入力（音声からテキスト）は `transcription_span()` でラップされます。
-   音声出力（テキストから音声）は `speech_span()` でラップされます。
-   関連する音声スパンは `speech_group_span()` の下にまとめられる場合があります。

デフォルトでは、トレース名は "Agent trace" です。`trace` を使用する場合、この名前を設定できます。また、[`RunConfig`][agents.run.RunConfig] で名前やその他のプロパティを設定することも可能です。

さらに、[カスタムトレースプロセッサー](#custom-tracing-processors) を設定して、トレースを他の宛先に送信することもできます（置き換えや追加の宛先として）。

## より高レベルのトレース

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

1. 2 回の `Runner.run` 呼び出しが `with trace()` でラップされているため、個々の実行は 2 つのトレースを作成するのではなく、全体のトレースの一部となります。

## トレースの作成

[`trace()`][agents.tracing.trace] 関数を使ってトレースを作成できます。トレースは開始と終了が必要です。方法は 2 つあります:

1. **推奨**: トレースをコンテキストマネージャーとして使用します。例: `with trace(...) as my_trace`。これにより、トレースの開始と終了が自動的に行われます。
2. [`trace.start()`][agents.tracing.Trace.start] および [`trace.finish()`][agents.tracing.Trace.finish] を手動で呼び出すこともできます。

現在のトレースは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で管理されています。これにより、自動的に並行処理にも対応します。トレースを手動で開始・終了する場合は、`start()`/`finish()` に `mark_as_current` および `reset_current` を渡して現在のトレースを更新する必要があります。

## スパンの作成

さまざまな [`*_span()`][agents.tracing.create] メソッドを使ってスパンを作成できます。通常、スパンを手動で作成する必要はありません。カスタムスパン情報を追跡するための [`custom_span()`][agents.tracing.custom_span] 関数も用意されています。

スパンは自動的に現在のトレースの一部となり、最も近い現在のスパンの下にネストされます。これは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で管理されています。

## 機微なデータ

一部のスパンは、機微なデータを記録する場合があります。

`generation_span()` は LLM 生成の入力・出力を保存し、`function_span()` は関数呼び出しの入力・出力を保存します。これらには機微なデータが含まれる場合があるため、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] でそのデータの記録を無効化できます。

同様に、音声スパンはデフォルトで入力・出力音声の base64 エンコード PCM データを含みます。[`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] を設定することで、この音声データの記録を無効化できます。

## カスタムトレースプロセッサー

トレーシングの高レベルなアーキテクチャは以下の通りです:

-   初期化時にグローバルな [`TraceProvider`][agents.tracing.setup.TraceProvider] を作成し、トレースの生成を担当します。
-   `TraceProvider` は [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] で構成されており、トレースやスパンをバッチで [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] に送信します。これにより、スパンやトレースが OpenAI バックエンドにバッチでエクスポートされます。

このデフォルト設定をカスタマイズし、トレースを別のバックエンドや追加のバックエンドに送信したり、エクスポーターの動作を変更したりするには、2 つの方法があります:

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] を使うと、**追加の** トレースプロセッサーを追加できます。これにより、トレースやスパンが準備できた時点で独自の処理を行うことができ、OpenAI バックエンドへの送信に加えて独自の処理が可能です。
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] を使うと、デフォルトのプロセッサーを独自のトレースプロセッサーに**置き換える**ことができます。この場合、OpenAI バックエンドにトレースが送信されるのは、`TracingProcessor` を含めた場合のみです。

## 外部トレースプロセッサー一覧

-   [Weights & Biases](https://weave-docs.wandb.ai/guides/integrations/openai_agents)
-   [Arize-Phoenix](https://docs.arize.com/phoenix/tracing/integrations-tracing/openai-agents-sdk)
-   [MLflow (self-hosted/OSS](https://mlflow.org/docs/latest/tracing/integrations/openai-agent)
-   [MLflow (Databricks hosted](https://docs.databricks.com/aws/en/mlflow/mlflow-tracing#-automatic-tracing)
-   [Braintrust](https://braintrust.dev/docs/guides/traces/integrations#openai-agents-sdk)
-   [Pydantic Logfire](https://logfire.pydantic.dev/docs/integrations/llms/openai/#openai-agents)
-   [AgentOps](https://docs.agentops.ai/v1/integrations/agentssdk)
-   [Scorecard](https://docs.scorecard.io/docs/documentation/features/tracing#openai-agents-sdk-integration)
-   [Keywords AI](https://docs.keywordsai.co/integration/development-frameworks/openai-agent)
-   [LangSmith](https://docs.smith.langchain.com/observability/how_to_guides/trace_with_openai_agents_sdk)
-   [Maxim AI](https://www.getmaxim.ai/docs/observe/integrations/openai-agents-sdk)
-   [Comet Opik](https://www.comet.com/docs/opik/tracing/integrations/openai_agents)
-   [Langfuse](https://langfuse.com/docs/integrations/openaiagentssdk/openai-agents)
-   [Langtrace](https://docs.langtrace.ai/supported-integrations/llm-frameworks/openai-agents-sdk)
-   [Okahu-Monocle](https://github.com/monocle2ai/monocle)