---
search:
  exclude: true
---
# トレーシング

Agents SDK にはトレーシングが組み込まれており、エージェントの実行中に発生する LLM 生成、関数ツール呼び出し、ハンドオフ、ガードレール、さらにカスタムイベントまでを含む包括的なイベント履歴を収集します。 [Traces ダッシュボード](https://platform.openai.com/traces) を使用すると、開発中および本番環境でワークフローをデバッグ・可視化・モニタリングできます。

!!!note

    トレーシングはデフォルトで有効になっています。無効化する方法は 2 つあります。

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定して、グローバルにトレーシングを無効化する  
    2. 単一の実行に対しては [`agents.run.RunConfig.tracing_disabled`][] を `True` に設定する

***OpenAI の API を使用し、Zero Data Retention (ZDR) ポリシーを採用している組織では、トレーシングは利用できません。***

## トレースとスパン

-   **トレース** は 1 回のワークフロー全体のエンドツーエンド操作を表します。スパンで構成されます。トレースには次のプロパティがあります。  
    -   `workflow_name`: 論理的なワークフローまたはアプリ名。例: 「Code generation」や「Customer service」。  
    -   `trace_id`: トレースの一意 ID。指定しない場合は自動生成されます。形式は `trace_<32_alphanumeric>` である必要があります。  
    -   `group_id`: 省略可能なグループ ID。同一会話からの複数トレースをリンクするために使用します。たとえばチャットスレッド ID を使用できます。  
    -   `disabled`: `True` の場合、このトレースは記録されません。  
    -   `metadata`: 省略可能なメタデータ。  
-   **スパン** は開始時刻と終了時刻を持つ操作を表します。スパンには次のものがあります。  
    -   `started_at` と `ended_at` のタイムスタンプ  
    -   属するトレースを示す `trace_id`  
    -   親スパンを指す `parent_id` (存在する場合)  
    -   スパンに関する情報を保持する `span_data`。たとえば `AgentSpanData` はエージェント情報、`GenerationSpanData` は LLM 生成情報などを含みます。  

## デフォルトのトレーシング

デフォルトで SDK は以下をトレースします。

-   `Runner.{run, run_sync, run_streamed}()` 全体を `trace()` でラップ  
-   エージェントが実行されるたびに `agent_span()` でラップ  
-   LLM 生成を `generation_span()` でラップ  
-   関数ツール呼び出しをそれぞれ `function_span()` でラップ  
-   ガードレールを `guardrail_span()` でラップ  
-   ハンドオフを `handoff_span()` でラップ  
-   音声入力 (speech-to-text) を `transcription_span()` でラップ  
-   音声出力 (text-to-speech) を `speech_span()` でラップ  
-   関連する音声スパンは `speech_group_span()` の下に配置される場合があります  

デフォルトでは、トレース名は「Agent workflow」です。`trace` を使用してこの名前を設定するか、[`RunConfig`][agents.run.RunConfig] で名前やその他プロパティを構成できます。

さらに、[カスタムトレーシングプロセッサー](#custom-tracing-processors) を設定して、トレースを他の送信先へ送ることもできます (置き換え、または追加送信)。

## 上位レベルのトレース

複数回の `run()` 呼び出しを 1 つのトレースにまとめたい場合があります。その場合は、コード全体を `trace()` でラップしてください。

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

1. `Runner.run` への 2 回の呼び出しが `with trace()` でラップされているため、個別トレースは作成されず、全体で 1 つのトレースになります。

## トレースの作成

[`trace()`][agents.tracing.trace] 関数でトレースを作成できます。トレースには開始と終了が必要で、方法は 2 つあります。

1. **推奨**: `with trace(...) as my_trace` のように context manager として使用する。開始と終了が自動で行われます。  
2. [`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を手動で呼び出す。  

現在のトレースは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で管理されるため、並行処理でも自動的に機能します。トレースを手動で開始・終了する場合は、`start()`/`finish()` に `mark_as_current` と `reset_current` を渡して現在のトレースを更新する必要があります。

## スパンの作成

さまざまな [`*_span()`][agents.tracing.create] メソッドでスパンを作成できますが、通常は手動で作成する必要はありません。カスタムスパン情報を追跡するには [`custom_span()`][agents.tracing.custom_span] を利用できます。

スパンは自動的に現在のトレースに含まれ、最も近い現在のスパンの下にネストされます。これも Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で追跡しています。

## 機密データ

一部のスパンは機密データを含む可能性があります。

`generation_span()` は LLM の入力／出力を、`function_span()` は関数呼び出しの入力／出力を保存します。これらが機密情報を含む場合は、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] で記録を無効化できます。

同様に、オーディオスパンはデフォルトで base64 エンコードされた PCM データ (入力・出力音声) を含みます。音声データの記録を無効化するには、[`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] を設定してください。

## カスタムトレーシングプロセッサー

トレーシングの高レベルアーキテクチャは次のとおりです。

-   初期化時にグローバルな [`TraceProvider`][agents.tracing.setup.TraceProvider] を作成し、トレースの生成を担当させます。  
-   `TraceProvider` に [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] を設定し、バッチ単位でスパンとトレースを [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] に送信します。このエクスポーターが OpenAI バックエンドへバッチ送信します。  

デフォルト設定をカスタマイズして、別のバックエンドへ送信したり、エクスポーターの動作を変更したりするには、次の 2 通りがあります。

1. [`add_trace_processor()`][agents.tracing.add_trace_processor]  
   追加のトレースプロセッサーを登録し、トレース／スパンが準備完了した時点で受け取ります。OpenAI バックエンドへ送信しつつ独自処理を実行する場合に使用します。  
2. [`set_trace_processors()`][agents.tracing.set_trace_processors]  
   デフォルトのプロセッサーを **置き換え** ます。OpenAI バックエンドへトレースが送信されなくなるので、必要に応じて送信用の `TracingProcessor` を含めてください。  

## 外部トレーシングプロセッサー一覧

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