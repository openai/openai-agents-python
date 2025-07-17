---
search:
  exclude: true
---
# トレーシング

Agents SDK には、エージェント実行中に発生するイベント ( LLM 生成、ツール呼び出し、ハンドオフ、ガードレール、さらにはカスタムイベント) を網羅的に記録する組み込みのトレーシング機能が含まれています。 [Traces dashboard](https://platform.openai.com/traces) を使用すると、開発時および本番環境でワークフローをデバッグ、可視化、監視できます。

!!!note

    トレーシングはデフォルトで有効です。無効化する方法は 2 つあります。

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定して、グローバルにトレーシングを無効化する  
    2. 単一の実行に対しては、[`agents.run.RunConfig.tracing_disabled`][] を `True` に設定する

***OpenAI の API を使用し Zero Data Retention ( ZDR ) ポリシーの下で運用している組織では、トレーシングは利用できません。***

## トレースとスパン

-   **トレース** は 1 つのワークフローにおけるエンドツーエンドの操作を表します。複数のスパンで構成され、以下のプロパティを持ちます。  
    -   `workflow_name` : 論理的なワークフローまたはアプリ名。例: 「コード生成」や「カスタマー サービス」  
    -   `trace_id` : トレースの一意 ID。渡さない場合は自動生成されます。形式は `trace_<32_alphanumeric>` である必要があります。  
    -   `group_id` : 省略可。会話内の複数トレースをリンクするグループ ID。たとえばチャットスレッド ID などに利用できます。  
    -   `disabled` : `True` の場合、このトレースは記録されません。  
    -   `metadata` : 省略可のメタデータ。  
-   **スパン** は開始時刻と終了時刻を持つ操作を表します。スパンには次のものがあります。  
    -   `started_at` と `ended_at` のタイムスタンプ  
    -   所属するトレースを示す `trace_id`  
    -   親スパンを指す `parent_id` (存在する場合)  
    -   スパンに関する情報を含む `span_data` 。例: `AgentSpanData` はエージェント情報、`GenerationSpanData` は LLM 生成情報など  

## デフォルトのトレーシング

デフォルトでは、SDK は以下をトレースします。

-    `Runner.{run, run_sync, run_streamed}()` 全体を `trace()` でラップ  
-   エージェントが実行されるたびに `agent_span()` でラップ  
-   LLM 生成を `generation_span()` でラップ  
-   関数ツール呼び出しを `function_span()` でラップ  
-   ガードレールを `guardrail_span()` でラップ  
-   ハンドオフを `handoff_span()` でラップ  
-   音声入力 ( speech-to-text ) を `transcription_span()` でラップ  
-   音声出力 ( text-to-speech ) を `speech_span()` でラップ  
-   関連する音声スパンは `speech_group_span()` の下に配置される場合があります  

デフォルトのトレース名は「Agent trace」です。`trace` を使用する際にこの名前を設定することも、[`RunConfig`][agents.run.RunConfig] で名前やその他のプロパティを構成することもできます。

さらに、[カスタム トレース プロセッサ](#custom-tracing-processors) を設定して、トレースを別の送信先へプッシュする (置き換えまたは追加) ことも可能です。

## 上位レベルのトレース

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

1. `with trace()` で 2 回の `Runner.run` をラップしているため、それぞれの実行は個別にトレースを作成せず、全体トレースに含まれます。

## トレースの作成

トレースは [`trace()`][agents.tracing.trace] 関数で作成できます。開始と終了が必要で、方法は 2 つあります。

1. **推奨**: コンテキストマネージャとして使用する ( 例: `with trace(...) as my_trace` ) 。これにより、トレースの開始と終了が自動で行われます。  
2. [`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を手動で呼び出す。  

現在のトレースは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で管理されるため、並行処理に自動対応します。トレースを手動で開始／終了する場合は、`start()` / `finish()` に `mark_as_current` と `reset_current` を渡して現在のトレースを更新してください。

## スパンの作成

各種 [`*_span()`][agents.tracing.create] メソッドでスパンを作成できます。通常、スパンを手動で作成する必要はありませんが、カスタム情報を追跡するための [`custom_span()`][agents.tracing.custom_span] も用意されています。

スパンは自動で現在のトレースに属し、最も近い現在のスパンの下にネストされます。この情報も Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で管理されます。

## 機微なデータ

一部のスパンでは機微なデータを取得する可能性があります。

`generation_span()` は LLM 生成の入力／出力を、`function_span()` は関数呼び出しの入力／出力を保存します。これらに機微なデータが含まれる場合は、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] を使って取得を無効化できます。

同様に、オーディオ スパンには入力／出力オーディオの base64 エンコード PCM データがデフォルトで含まれます。これを無効にする場合は、[`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] を設定してください。

## カスタム トレース プロセッサ

トレーシングの高レベル アーキテクチャは以下のとおりです。

-   初期化時にグローバルな [`TraceProvider`][agents.tracing.setup.TraceProvider] を生成し、トレースの作成を担当します。  
-   `TraceProvider` は [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] を使用してトレース／スパンをバッチ送信し、[`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] が OpenAI バックエンドへバッチでエクスポートします。  

デフォルト設定を変更し、別のバックエンドへ送信したりエクスポータの動作を変更したりするには、以下の 2 つの方法があります。

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] を使って **追加** のトレース プロセッサを登録し、トレース／スパンを同時に受け取って独自処理を行う ( OpenAI バックエンドへの送信に加えて利用可能 )  
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] を使ってデフォルトのプロセッサを **置き換え** 自前のトレース プロセッサに差し替える ( OpenAI バックエンドへ送信したい場合は、その処理を行う `TracingProcessor` を含める必要あり )  

## 外部トレース プロセッサ一覧

-   [Weights & Biases](https://weave-docs.wandb.ai/guides/integrations/openai_agents)  
-   [Arize-Phoenix](https://docs.arize.com/phoenix/tracing/integrations-tracing/openai-agents-sdk)  
-   [Future AGI](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/openai_agents)  
-   [MLflow ( self-hosted / OSS )](https://mlflow.org/docs/latest/tracing/integrations/openai-agent)  
-   [MLflow ( Databricks hosted )](https://docs.databricks.com/aws/en/mlflow/mlflow-tracing#-automatic-tracing)  
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