---
search:
  exclude: true
---
# トレーシング

Agents SDK にはトレーシング機能が組み込まれており、エージェント実行中に発生する LLM 生成、tool 呼び出し、handoffs、guardrail、カスタムイベントなどの包括的な記録を収集します。[Traces ダッシュボード](https://platform.openai.com/traces) を使用すると、開発時および本番環境でワークフローをデバッグ・可視化・監視できます。

!!!note

    Tracing はデフォルトで有効になっています。無効化する方法は 2 つあります。

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定してグローバルに無効化する  
    2. 単一の実行で無効化する場合は [`agents.run.RunConfig.tracing_disabled`][] を `True` に設定する

***OpenAI の API を Zero Data Retention (ZDR) ポリシーで利用している組織では、トレーシングは利用できません。***

## トレースとスパン

-   **トレース (Trace)** は 1 回のワークフローのエンドツーエンドの操作を表します。トレースは複数のスパンで構成され、以下のプロパティを持ちます。  
    -   `workflow_name`: 論理的なワークフローまたはアプリ名。例: "Code generation" や "Customer service"  
    -   `trace_id`: トレースの一意 ID。指定しない場合は自動生成されます。形式は `trace_<32_alphanumeric>` でなければなりません。  
    -   `group_id`: 会話内の複数トレースを関連付ける任意のグループ ID。たとえばチャットスレッド ID など。  
    -   `disabled`: True の場合、このトレースは記録されません。  
    -   `metadata`: トレースに付与する任意のメタデータ。  
-   **スパン (Span)** は開始時刻と終了時刻を持つ操作を表します。スパンは以下を持ちます。  
    -   `started_at` と `ended_at` タイムスタンプ  
    -   所属するトレースを示す `trace_id`  
    -   親スパンを指す `parent_id` (存在する場合)  
    -   スパン情報を保持する `span_data`。例として `AgentSpanData` はエージェント情報を、`GenerationSpanData` は LLM 生成情報を保持します。  

## デフォルトのトレーシング

デフォルトでは SDK が次をトレースします。

-   `Runner.{run, run_sync, run_streamed}()` 全体を `trace()` でラップ
-   エージェント実行ごとに `agent_span()` でラップ
-   LLM 生成を `generation_span()` でラップ
-   function tool 呼び出しを `function_span()` でラップ
-   guardrail を `guardrail_span()` でラップ
-   handoffs を `handoff_span()` でラップ
-   音声入力 (speech-to-text) を `transcription_span()` でラップ
-   音声出力 (text-to-speech) を `speech_span()` でラップ
-   関連する音声スパンは `speech_group_span()` の下にネストされる場合があります

デフォルトのトレース名は "Agent workflow" です。`trace` を使用して名前を設定するか、[`RunConfig`][agents.run.RunConfig] で名前やその他のプロパティを設定できます。

さらに、[カスタムトレースプロセッサ](#custom-tracing-processors) を設定し、別の送信先へトレースをプッシュすることも可能です (置換または追加先として)。

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

1. `Runner.run` への 2 回の呼び出しが `with trace()` に包まれているため、個別のトレースを作成せず 1 つのトレース内に含まれます。

## トレースの作成

[`trace()`][agents.tracing.trace] 関数を使用してトレースを作成できます。トレースは開始と終了が必要で、方法は 2 つあります。

1. **推奨**: トレースをコンテキストマネージャとして使用する (例: `with trace(...) as my_trace`)。自動的に開始と終了が行われます。  
2. 手動で [`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を呼び出す方法。

現在のトレースは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で追跡され、並行処理でも自動的に機能します。手動で開始/終了する場合は、`start()`/`finish()` に `mark_as_current` と `reset_current` を渡して現在のトレースを更新してください。

## スパンの作成

各種 [`*_span()`][agents.tracing.create] メソッドでスパンを作成できますが、通常は手動で作成する必要はありません。カスタム情報を追跡するための [`custom_span()`][agents.tracing.custom_span] も用意されています。

スパンは自動的に現在のトレースに属し、Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で追跡されている最も近い現在のスパンの下にネストされます。

## 機微データ

一部のスパンは機微データを記録する可能性があります。

`generation_span()` は LLM 生成の入出力を保存し、`function_span()` は function 呼び出しの入出力を保存します。機微データを含む可能性があるため、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] でこれらのデータの記録を無効化できます。

同様に、Audio スパンはデフォルトで入出力音声の base64 エンコードされた PCM データを含みます。[`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] を設定し、音声データの記録を無効化できます。

## カスタムトレースプロセッサ

トレーシングの高レベルアーキテクチャは次のとおりです。

-   初期化時にグローバル [`TraceProvider`][agents.tracing.setup.TraceProvider] を作成し、トレースを生成します。  
-   `TraceProvider` は [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] を用いてスパンとトレースをバッチ送信し、[`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] が OpenAI バックエンドへバッチでエクスポートします。  

このデフォルト設定を変更し、別のバックエンドへ送信したりエクスポータの挙動を変更したりするには、次の 2 つの方法があります。

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] を用いて **追加** のトレースプロセッサを登録します。これにより OpenAI バックエンドへの送信に加えて独自の処理を実行できます。  
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] を用いて既定のプロセッサを **置換** します。OpenAI バックエンドへ送信したい場合は、その機能を持つ `TracingProcessor` を含めてください。

## 外部トレースプロセッサ一覧

-   [Weights & Biases](https://weave-docs.wandb.ai/guides/integrations/openai_agents)
-   [Arize-Phoenix](https://docs.arize.com/phoenix/tracing/integrations-tracing/openai-agents-sdk)
-   [Future AGI](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/openai_agents)
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
-   [Galileo](https://v2docs.galileo.ai/integrations/openai-agent-integration#openai-agent-integration)
-   [Portkey AI](https://portkey.ai/docs/integrations/agents/openai-agents)
-   [LangDB AI](https://docs.langdb.ai/getting-started/working-with-agent-frameworks/working-with-openai-agents-sdk)