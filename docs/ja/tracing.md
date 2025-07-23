---
search:
  exclude: true
---
# トレーシング

Agents SDK にはビルトインのトレーシング機能が含まれており、エージェントの実行中に発生する LLM 生成、ツール呼び出し、ハンドオフ、ガードレール、さらにはカスタムイベントまでの詳細な履歴を収集します。Traces ダッシュボードを使用すると、開発時および本番環境でワークフローをデバッグ、可視化、モニタリングできます。

!!!note

    トレーシングはデフォルトで有効になっています。無効化する方法は 2 つあります:

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定してトレーシングをグローバルに無効化する  
    2. 単一の実行に対して [`agents.run.RunConfig.tracing_disabled`][] を `True` に設定してトレーシングを無効化する  

    ***OpenAI の API をゼロデータ保持 (ZDR) ポリシーの下で利用する組織では、トレーシングは利用できません。***

## トレースとスパン

- **Traces** は 1 度のワークフロー全体を表すエンドツーエンドの操作です。複数の Span で構成されます。Trace には次のプロパティがあります:  
    - `workflow_name`: 論理的なワークフローまたはアプリ名。例: “Code generation” や “Customer service”  
    - `trace_id`: Trace の一意 ID。指定しない場合は自動生成されます。形式は `trace_<32_alphanumeric>`  
    - `group_id`: 省略可。同一会話からの複数の Trace を関連付けるための ID。たとえばチャットスレッド ID など  
    - `disabled`: `True` の場合、この Trace は記録されません  
    - `metadata`: 省略可。Trace に付随するメタデータ  
- **Spans** は開始時刻と終了時刻を持つ操作を表します。Span には次の情報があります:  
    - `started_at` と `ended_at` タイムスタンプ  
    - `trace_id`: 所属する Trace の ID  
    - `parent_id`: 親 Span がある場合、その Span への参照  
    - `span_data`: Span に関する情報。例: `AgentSpanData` はエージェント、`GenerationSpanData` は LLM 生成などの情報を保持  

## デフォルトのトレーシング

デフォルトでは、SDK は次をトレースします:

- `Runner.{run, run_sync, run_streamed}()` 全体を `trace()` でラップ  
- エージェントが実行されるたびに `agent_span()` でラップ  
- LLM 生成を `generation_span()` でラップ  
- 関数ツール呼び出しを `function_span()` でラップ  
- ガードレールを `guardrail_span()` でラップ  
- ハンドオフを `handoff_span()` でラップ  
- 音声入力 (音声→テキスト) を `transcription_span()` でラップ  
- 音声出力 (テキスト→音声) を `speech_span()` でラップ  
- 関連する音声 Span は `speech_group_span()` の下にネストされる場合があります  

デフォルトでは Trace の名前は “Agent workflow” です。`trace` を使用してこの名前を設定するか、[`RunConfig`][agents.run.RunConfig] で名前や他のプロパティを構成できます。

さらに、[カスタムトレーシングプロセッサー](#custom-tracing-processors) を設定して、Trace を別の送信先にプッシュすることもできます (置き換えまたは追加送信先として)。

## より高レベルのトレース

複数回の `run()` 呼び出しを 1 つの Trace にまとめたい場合は、コード全体を `trace()` でラップします。

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

1. 2 回の `Runner.run` 呼び出しが `with trace()` でラップされているため、それぞれが個別の Trace を作成するのではなく、全体で 1 つの Trace になります。

## トレースの作成

[`trace()`][agents.tracing.trace] 関数を使って Trace を作成できます。Trace は開始と終了が必要で、次の 2 つの方法があります:

1. **推奨**: `with trace(...) as my_trace` のようにコンテキストマネージャーとして使用する。適切なタイミングで自動的に開始・終了します  
2. [`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を手動で呼び出す  

現在の Trace は Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) により管理され、並行処理でも自動で機能します。Trace を手動で開始・終了する場合は、`start()`/`finish()` に `mark_as_current` および `reset_current` を渡して現在の Trace を更新してください。

## スパンの作成

さまざまな [`*_span()`][agents.tracing.create] メソッドを使用して Span を作成できますが、通常は手動で作成する必要はありません。カスタム情報を記録したい場合は [`custom_span()`][agents.tracing.custom_span] を利用できます。

Span は自動的に現在の Trace に属し、最も近い現在の Span の下にネストされます。これも Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で追跡されます。

## 機密データ

一部の Span には機密データが含まれる可能性があります。

`generation_span()` は LLM の入力／出力を、`function_span()` は関数呼び出しの入力／出力を保存します。これらに機密データが含まれる場合は、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] で記録を無効化できます。

同様に、Audio Span にはデフォルトで base64 エンコードされた PCM データが含まれます。音声データの記録を停止したい場合は、[`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] を設定してください。

## カスタムトレーシングプロセッサー

トレーシングの高レベルアーキテクチャは次のとおりです:

- 初期化時に、Trace を生成するグローバルな [`TraceProvider`][agents.tracing.setup.TraceProvider] を作成  
- `TraceProvider` に [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] を設定し、Trace／Span をバッチで [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] へ送信。Exporter は OpenAI バックエンドへバッチ送信を行います  

デフォルト設定を変更して別のバックエンドへ送信したり、Exporter の挙動を変更したりするには次の 2 通りがあります:

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] を使用して **追加** の Trace プロセッサーを登録する。これにより、OpenAI バックエンドへの送信に加えて独自の処理を実行できます  
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] を使用してデフォルトのプロセッサーを **置き換える**。OpenAI バックエンドへ送信したい場合は、その機能を持つ `TracingProcessor` を自分で含める必要があります  

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