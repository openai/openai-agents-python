---
search:
  exclude: true
---
# トレーシング

 Agents SDK にはビルトインのトレーシング機能が含まれており、エージェント実行中の LLM 生成、ツール呼び出し、ハンドオフ、ガードレール、カスタムイベントまでを包括的に記録します。 [Traces ダッシュボード](https://platform.openai.com/traces) を使うことで、開発中や本番環境でワークフローをデバッグ・可視化・モニタリングできます。

!!!note

    トレーシングはデフォルトで有効です。無効化する方法は 2 つあります。

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定してグローバルに無効化する  
    2. 単一の実行で無効化する場合は [`agents.run.RunConfig.tracing_disabled`][] を `True` に設定する

***OpenAI の API を Zero Data Retention (ZDR) ポリシーで利用している組織では、トレーシングは利用できません。***

## トレースとスパン

-   **トレース** は 1 つの「ワークフロー」のエンドツーエンドの操作を表し、複数のスパンで構成されます。主なプロパティは以下のとおりです。  
    -   `workflow_name` : 論理的なワークフローまたはアプリ名。例: 「Code generation」や「Customer service」  
    -   `trace_id` : トレース固有の ID。指定しない場合は自動生成されます。形式は `trace_<32_alphanumeric>` である必要があります。  
    -   `group_id` : 省略可能なグループ ID。複数のトレースを同じ会話にひも付ける際に使用します。たとえばチャットスレッド ID など。  
    -   `disabled` : `True` の場合、このトレースは記録されません。  
    -   `metadata` : トレースに付随するメタデータ (任意)。  
-   **スパン** は開始時刻と終了時刻を持つ操作を表します。主なプロパティは以下のとおりです。  
    -   `started_at` と `ended_at` のタイムスタンプ  
    -   所属するトレースを表す `trace_id`  
    -   親スパンを指す `parent_id` (存在する場合)  
    -   スパンに関する情報を持つ `span_data` 。例: `AgentSpanData` はエージェント情報、 `GenerationSpanData` は LLM 生成情報など。  

## デフォルトのトレーシング

デフォルトでは SDK が以下をトレースします。

-   `Runner.{run, run_sync, run_streamed}()` 全体を `trace()` でラップ  
-   エージェント実行ごとに `agent_span()` でラップ  
-   LLM 生成を `generation_span()` でラップ  
-   関数ツール呼び出しを `function_span()` でラップ  
-   ガードレールを `guardrail_span()` でラップ  
-   ハンドオフを `handoff_span()` でラップ  
-   音声入力 (speech-to-text) を `transcription_span()` でラップ  
-   音声出力 (text-to-speech) を `speech_span()` でラップ  
-   関連する音声スパンは `speech_group_span()` の下にまとめられる場合があります  

デフォルトではトレース名は「Agent workflow」です。 `trace` を使用してこの名前を設定することも、 [`RunConfig`][agents.run.RunConfig] で名前やその他のプロパティを設定することもできます。

さらに、 [カスタムトレースプロセッサ](#custom-tracing-processors) を設定して、トレースを別の宛先へ送信（置換または追加）することも可能です。

## 上位レベルのトレース

複数回の `run()` 呼び出しを 1 つのトレースに含めたい場合は、コード全体を `trace()` でラップします。

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

1. `with trace()` で 2 回の `Runner.run` 呼び出しをラップしているため、個別にトレースを作成せず、全体が 1 つのトレースになります。

## トレースの作成

[`trace()`][agents.tracing.trace] 関数を使ってトレースを作成できます。トレースは開始と終了が必要で、方法は 2 つあります。

1. **推奨**: コンテキストマネージャとして使用する (例: `with trace(...) as my_trace`)。適切なタイミングで自動的に開始・終了します。  
2. [`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を手動で呼び出す。  

現在のトレースは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で管理されるため、並列処理でも自動的に機能します。トレースを手動で開始・終了する場合は、 `start()` / `finish()` に `mark_as_current` と `reset_current` を渡して現在のトレースを更新してください。

## スパンの作成

各種 [`*_span()`][agents.tracing.create] メソッドを使ってスパンを作成できますが、通常は手動で作成する必要はありません。カスタム情報を追跡する場合は [`custom_span()`][agents.tracing.custom_span] を利用できます。

スパンは自動的に現在のトレースに含まれ、最も近い現在のスパンの下にネストされます。これも Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で管理されます。

## 機微情報

一部のスパンは機微情報を含む可能性があります。

`generation_span()` は LLM 生成の入力／出力、 `function_span()` は関数呼び出しの入力／出力を保存します。これらに機微情報が含まれる場合は、 [`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] で記録を無効化できます。

同様に、音声スパンはデフォルトで入力・出力音声の base64 エンコード済み PCM データを含みます。 [`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] を設定して音声データの記録を無効化できます。

## カスタムトレースプロセッサ

トレーシングの高レベル構成は以下のとおりです。

-   初期化時にグローバルな [`TraceProvider`][agents.tracing.setup.TraceProvider] を作成し、トレースを生成する役割を担います。  
-   `TraceProvider` には [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] を設定し、スパンとトレースをバッチで [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] に送信します。 `BackendSpanExporter` は OpenAI バックエンドへバッチ送信を行います。  

このデフォルト設定を変更して別のバックエンドへ送信したり、エクスポーターの挙動を修正したい場合は次の 2 つの方法があります。

1. [`add_trace_processor()`][agents.tracing.add_trace_processor]  
   追加のトレースプロセッサを登録し、生成されたトレースとスパンを受け取らせます。OpenAI バックエンドへの送信に加えて独自処理を行いたい場合に使用します。  
2. [`set_trace_processors()`][agents.tracing.set_trace_processors]  
   デフォルトのプロセッサを置き換えて独自のトレースプロセッサを設定します。OpenAI バックエンドへ送信したい場合は、その機能を持つ `TracingProcessor` を含める必要があります。  

## 外部トレースプロセッサ一覧

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