---
search:
  exclude: true
---
# トレーシング

Agents SDK にはビルトインのトレーシング機能が含まれており、エージェントの実行中に発生する LLM 生成、ツール呼び出し、ハンドオフ、ガードレール、カスタムイベントなどの包括的なイベント履歴を収集します。[Traces ダッシュボード](https://platform.openai.com/traces) を使用すると、開発時や本番環境でワークフローをデバッグ、可視化、モニタリングできます。

!!!note

    トレーシングはデフォルトで有効です。無効にする方法は 2 つあります。

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定して、グローバルにトレーシングを無効化する  
    2. 1 回の実行だけ無効にする場合は [`agents.run.RunConfig.tracing_disabled`][] を `True` に設定する

***OpenAI の API を Zero Data Retention (ZDR) ポリシーで運用している組織では、トレーシングは利用できません。***

## トレースとスパン

-   **トレース (Trace)** は 1 回の「ワークフロー」のエンドツーエンドの操作を表します。複数のスパンで構成されます。トレースには以下のプロパティがあります。  
    -   `workflow_name`：論理的なワークフローまたはアプリ名。例：「Code generation」や「Customer service」  
    -   `trace_id`：トレースごとの一意 ID。指定しない場合は自動生成されます。形式は `trace_<32_alphanumeric>`  
    -   `group_id`：オプションのグループ ID。同じ会話から発生した複数のトレースを紐付けるために使用します。例としてチャットスレッド ID など  
    -   `disabled`：`True` の場合、このトレースは記録されません  
    -   `metadata`：トレースの任意メタデータ  
-   **スパン (Span)** は開始時刻と終了時刻を持つ操作を表します。スパンには以下があります。  
    -   `started_at` と `ended_at` タイムスタンプ  
    -   `trace_id`：所属するトレースを示します  
    -   `parent_id`：このスパンの親スパンを指します（存在する場合）  
    -   `span_data`：スパンに関する情報。例として `AgentSpanData` はエージェント情報、`GenerationSpanData` は LLM 生成情報など  

## デフォルトのトレーシング

デフォルトでは、SDK は以下をトレースします。

-   `Runner.{run, run_sync, run_streamed}()` 全体が `trace()` でラップされます  
-   エージェントが実行されるたびに `agent_span()` でラップされます  
-   LLM 生成は `generation_span()` でラップされます  
-   関数ツール呼び出しはそれぞれ `function_span()` でラップされます  
-   ガードレールは `guardrail_span()` でラップされます  
-   ハンドオフは `handoff_span()` でラップされます  
-   音声入力 (speech-to-text) は `transcription_span()` でラップされます  
-   音声出力 (text-to-speech) は `speech_span()` でラップされます  
-   関連する音声スパンは `speech_group_span()` の下にネストされる場合があります  

デフォルトでは、トレース名は "Agent workflow" です。`trace` を使用して名前を設定するか、[`RunConfig`][agents.run.RunConfig] で名前やその他のプロパティを構成できます。

さらに、[カスタムトレースプロセッサー](#custom-tracing-processors) を設定して、別の送信先にトレースを送信する（置き換え、または追加送信）ことも可能です。

## 上位レベルのトレース

複数回の `run()` 呼び出しを 1 つのトレースとしてまとめたい場合があります。その場合は、コード全体を `trace()` でラップします。

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

1. `Runner.run` の 2 回の呼び出しが `with trace()` でラップされているため、それぞれが個別のトレースを生成するのではなく、全体で 1 つのトレースになります。

## トレースの作成

[`trace()`][agents.tracing.trace] 関数を使ってトレースを作成できます。トレースは開始と終了が必要で、方法は 2 つあります。

1. **推奨**：コンテキストマネージャとして使用し、`with trace(...) as my_trace` とする。適切なタイミングで自動的に開始・終了します。  
2. [`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を手動で呼び出すことも可能です。  

現在のトレースは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で管理されています。これにより自動的に並列処理へ対応します。トレースを手動で開始・終了する場合は、`start()` / `finish()` に `mark_as_current` と `reset_current` を渡して現在のトレースを更新してください。

## スパンの作成

さまざまな [`*_span()`][agents.tracing.create] メソッドを使ってスパンを作成できます。通常は手動でスパンを作成する必要はありません。カスタムスパン情報を追跡するための [`custom_span()`][agents.tracing.custom_span] も利用できます。

スパンは自動的に現在のトレースに含まれ、Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) により追跡される最も近い現在のスパンの下にネストされます。

## 機微データ

一部のスパンは機微データを含む可能性があります。

`generation_span()` は LLM 生成の入力/出力を保存し、`function_span()` は関数呼び出しの入力/出力を保存します。これらに機微データが含まれる場合があるため、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] でデータ収集を無効化できます。

同様に、オーディオスパンはデフォルトで入力と出力の base64 エンコードされた PCM データを含みます。[`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] を設定して、オーディオデータの収集を無効化できます。

## カスタムトレーシングプロセッサー

トレーシングの高レベルアーキテクチャは以下のとおりです。

-   初期化時にグローバルな [`TraceProvider`][agents.tracing.setup.TraceProvider] を作成し、トレースの生成を担当します。  
-   `TraceProvider` には [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] を設定し、スパン/トレースをバッチで [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] に送信します。Exporter はスパンとトレースをバッチで OpenAI バックエンドへ送信します。

デフォルト設定をカスタマイズし、別のバックエンドへ送信したり Exporter の挙動を変更したりするには、以下の 2 つの方法があります。

1. [`add_trace_processor()`][agents.tracing.add_trace_processor]：**追加**のトレースプロセッサーを登録します。これにより、OpenAI バックエンドへの送信に加えて独自処理が可能です。  
2. [`set_trace_processors()`][agents.tracing.set_trace_processors]：デフォルトプロセッサーを **置き換え** ます。OpenAI バックエンドへ送信したい場合は、その機能を持つ `TracingProcessor` を含める必要があります。

## 外部トレーシングプロセッサー一覧

-   [Weights & Biases](https://weave-docs.wandb.ai/guides/integrations/openai_agents)  
-   [Arize-Phoenix](https://docs.arize.com/phoenix/tracing/integrations-tracing/openai-agents-sdk)  
-   [Future AGI](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/openai_agents)  
-   [MLflow (self-hosted/OSS)](https://mlflow.org/docs/latest/tracing/integrations/openai-agent)  
-   [MLflow (Databricks hosted)](https://docs.databricks.com/aws/en/mlflow/mlflow-tracing#-automatic-tracing)  
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
-   [Galileo](https://v2docs.galileo.ai/integrations/openai-agent-integration#openai-agent-integration)  
-   [Portkey AI](https://portkey.ai/docs/integrations/agents/openai-agents)  
-   [LangDB AI](https://docs.langdb.ai/getting-started/working-with-agent-frameworks/working-with-openai-agents-sdk)