---
search:
  exclude: true
---
# トレーシング

Agents SDK にはビルトインのトレーシング機能があり、エージェントの実行中に発生するイベント―― LLM 生成、ツール呼び出し、ハンドオフ、ガードレール、さらにカスタムイベントまで――を網羅的に記録します。開発時と本番環境の両方で [Traces dashboard](https://platform.openai.com/traces) を使用すると、ワークフローをデバッグ・可視化・モニタリングできます。

!!!note

    トレーシングはデフォルトで有効です。無効化する方法は次の 2 つです:

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定してグローバルに無効化する  
    2. 単一の実行に対しては [`agents.run.RunConfig.tracing_disabled`][] を `True` に設定する

***OpenAI の API を Zero Data Retention (ZDR) ポリシーで利用している組織では、トレーシングを利用できません。***

## トレースとスパン

-   **トレース** は 1 度のワークフロー全体を表します。複数のスパンで構成され、次のプロパティを持ちます:
    -   `workflow_name`: 論理的なワークフローまたはアプリ名。例: 「Code generation」や「Customer service」
    -   `trace_id`: トレースを一意に識別する ID。指定しない場合は自動生成されます。形式は `trace_<32_alphanumeric>` である必要があります。
    -   `group_id`: オプションのグループ ID。会話内の複数トレースを関連付けます。たとえばチャットスレッド ID など。
    -   `disabled`: `True` の場合、このトレースは記録されません。
    -   `metadata`: トレースに付随する任意のメタデータ。
-   **スパン** は開始時刻と終了時刻を持つ個々の処理を表します。スパンは以下を保持します:
    -   `started_at` と `ended_at` タイムスタンプ
    -   所属トレースを示す `trace_id`
    -   親スパンを指す `parent_id` (存在する場合)
    -   スパンに関する情報を格納する `span_data`。たとえば `AgentSpanData` にはエージェント情報が、`GenerationSpanData` には LLM 生成情報が含まれます。

## デフォルトのトレーシング

デフォルトで SDK は以下をトレースします:

-   `Runner.{run, run_sync, run_streamed}()` 全体を `trace()` でラップ
-   エージェントが実行されるたびに `agent_span()` でラップ
-   LLM 生成を `generation_span()` でラップ
-   関数ツール呼び出しを `function_span()` でラップ
-   ガードレールを `guardrail_span()` でラップ
-   ハンドオフを `handoff_span()` でラップ
-   音声入力 (speech‑to‑text) を `transcription_span()` でラップ
-   音声出力 (text‑to‑speech) を `speech_span()` でラップ
-   関連する音声スパンは `speech_group_span()` の下にネストされる場合があります

トレース名はデフォルトで「Agent trace」です。`trace` を使用して指定したり、[`RunConfig`][agents.run.RunConfig] で名前やその他のプロパティを設定できます。

さらに [カスタムトレーシングプロセッサー](#custom-tracing-processors) を設定して、トレースを別の送信先に出力（置き換えまたは追加）することも可能です。

## 上位レベルのトレース

複数回の `run()` 呼び出しを 1 つのトレースにまとめたい場合があります。その場合、コード全体を `trace()` でラップします。

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

1. `with trace()` で 2 つの `Runner.run` 呼び出しをラップしているため、それぞれが個別のトレースを作成せず、全体で 1 つのトレースになります。

## トレースの作成

[`trace()`][agents.tracing.trace] 関数を使ってトレースを作成できます。開始と終了が必要で、方法は 2 つあります。

1. **推奨**: `with trace(...) as my_trace` のようにコンテキストマネージャーとして使用する。開始と終了が自動で行われます。  
2. [`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を手動で呼び出す。

現在のトレースは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で管理されているため、並行処理でも自動で機能します。手動で開始／終了する場合は `start()`／`finish()` に `mark_as_current` と `reset_current` を渡して現在のトレースを更新してください。

## スパンの作成

各種 [`*_span()`][agents.tracing.create] メソッドでスパンを作成できます。一般的には手動で作成する必要はありません。カスタム情報を追跡するための [`custom_span()`][agents.tracing.custom_span] も利用できます。

スパンは自動的に現在のトレースの一部となり、最も近い現在のスパンの下にネストされます。これも Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で管理されています。

## 機密データ

一部のスパンでは機密データが収集される可能性があります。

`generation_span()` には LLM の入力と出力、`function_span()` には関数呼び出しの入力と出力が保存されます。これらに機密データが含まれる場合、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] を使用して記録を無効化できます。

同様に、音声スパンにはデフォルトで base64 エンコードされた PCM 音声データが含まれます。[`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] を設定して音声データの記録を無効化できます。

## カスタムトレーシングプロセッサー

トレーシングの高レベル構成は次のとおりです。

-   初期化時にグローバルな [`TraceProvider`][agents.tracing.setup.TraceProvider] を作成し、トレースを生成。
-   `TraceProvider` は [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] を用いてスパン／トレースをバッチ送信し、[`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] が OpenAI バックエンドへバッチでエクスポートします。

デフォルト設定を変更して別のバックエンドへ送信したり、エクスポーターの挙動を修正するには次の 2 通りがあります。

1. [`add_trace_processor()`][agents.tracing.add_trace_processor]  
   既定の送信に加え、**追加** のトレースプロセッサーを登録できます。これにより OpenAI バックエンドへの送信に加えて独自処理が可能です。  
2. [`set_trace_processors()`][agents.tracing.set_trace_processors]  
   既定のプロセッサーを置き換え、**独自** のトレースプロセッサーだけを使用します。OpenAI バックエンドへ送信する場合は、その機能を持つ `TracingProcessor` を含める必要があります。

## 外部トレーシングプロセッサー一覧

-   [Weights & Biases](https://weave-docs.wandb.ai/guides/integrations/openai_agents)
-   [Arize‑Phoenix](https://docs.arize.com/phoenix/tracing/integrations-tracing/openai-agents-sdk)
-   [MLflow (self‑hosted/OSS](https://mlflow.org/docs/latest/tracing/integrations/openai-agent)
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
-   [Okahu‑Monocle](https://github.com/monocle2ai/monocle)