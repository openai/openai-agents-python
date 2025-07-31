---
search:
  exclude: true
---
# トレーシング

Agents SDK には組み込みのトレーシング機能があり、 エージェント実行中に発生する LLM 生成、ツール呼び出し、ハンドオフ、ガードレール、カスタムイベントなどを包括的に記録します。 [Traces ダッシュボード](https://platform.openai.com/traces) を使用すると、開発および本番環境でワークフローをデバッグ・可視化・監視できます。

!!!note

    トレーシングはデフォルトで有効になっています。無効化する方法は 2 つあります。

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定してグローバルに無効化する
    2. 1 回の実行に対して [`agents.run.RunConfig.tracing_disabled`][] を `True` に設定して無効化する

***OpenAI の Zero Data Retention (ZDR) ポリシー下で API を利用している組織では、トレーシングは利用できません。***

## トレースとスパン

- **トレース** は 1 つの「ワークフロー」の end-to-end 操作を表します。複数のスパンで構成されます。トレースには以下のプロパティがあります。
    - `workflow_name`：論理的なワークフローまたはアプリ名。例： `Code generation` や `Customer service` など
    - `trace_id`：トレースの一意 ID。渡さなかった場合は自動生成されます。形式は `trace_<32_alphanumeric>` である必要があります。
    - `group_id`：オプションのグループ ID。同じ会話の複数トレースをリンクする目的で使用します。たとえばチャットスレッド ID など。
    - `disabled`： `True` の場合、このトレースは記録されません。
    - `metadata`：トレースに対するオプションのメタデータ
- **スパン** は開始時刻と終了時刻を持つ操作を表します。スパンには次があります。
    - `started_at` と `ended_at` のタイムスタンプ
    - 所属するトレースを示す `trace_id`
    - 親スパンを指す `parent_id`（存在する場合）
    - スパンに関する情報を格納する `span_data`。たとえば `AgentSpanData` にはエージェントに関する情報、`GenerationSpanData` には LLM 生成に関する情報が含まれます。

## デフォルトのトレーシング

デフォルトでは、SDK は以下をトレースします。

- `Runner.{run, run_sync, run_streamed}()` 全体が `trace()` でラップされる
- エージェントが実行されるたびに `agent_span()` でラップされる
- LLM 生成は `generation_span()` でラップされる
- 関数ツール呼び出しはそれぞれ `function_span()` でラップされる
- ガードレールは `guardrail_span()` でラップされる
- ハンドオフは `handoff_span()` でラップされる
- 音声入力（speech-to-text）は `transcription_span()` でラップされる
- 音声出力（text-to-speech）は `speech_span()` でラップされる
- 関連する音声スパンは `speech_group_span()` の下に親子関係を持つ場合がある

デフォルトでは、トレース名は "Agent workflow" です。 `trace` を使用して名前を設定するか、[`RunConfig`][agents.run.RunConfig] で名前やその他プロパティを構成できます。

さらに、[カスタムトレースプロセッサー](#custom-tracing-processors) を設定して、トレースを別の宛先に送信（置き換えまたは追加）することも可能です。

## より高レベルのトレース

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

1. 2 回の `Runner.run` 呼び出しが `with trace()` でラップされているため、それぞれが個別のトレースを作成する代わりに 1 つのトレースに含まれます。

## トレースの作成

[`trace()`][agents.tracing.trace] 関数でトレースを作成できます。トレースは開始と終了が必要で、次の 2 通りの方法があります。

1. **推奨**：コンテキストマネージャとして使用（例： `with trace(...) as my_trace`）。適切なタイミングで自動的に開始・終了します。
2. [`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を手動で呼び出す。

現在のトレースは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で管理されています。そのため並行処理でも自動的に機能します。トレースを手動で開始・終了する場合は、`start()` と `finish()` に `mark_as_current` と `reset_current` を渡して現在のトレースを更新する必要があります。

## スパンの作成

さまざまな [`*_span()`][agents.tracing.create] メソッドを使用してスパンを作成できますが、通常は手動で作成する必要はありません。カスタムスパン情報を追跡するための [`custom_span()`][agents.tracing.custom_span] も用意されています。

スパンは自動的に現在のトレースに含まれ、最も近い現在のスパンの下にネストされます。こちらも Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で管理されています。

## センシティブデータ

特定のスパンは機密情報を含む可能性があります。

`generation_span()` は LLM 生成の入力／出力を、`function_span()` は関数呼び出しの入力／出力を保存します。これらに機密データが含まれる場合があるため、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] でデータ取得を無効化できます。

同様に、Audio スパンは既定で入力と出力オーディオの base64 エンコード PCM データを含みます。このオーディオデータの取得は [`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] の設定で無効化できます。

## カスタムトレーシングプロセッサー

トレーシングの高レベルアーキテクチャは次のとおりです。

- 初期化時に、グローバルな [`TraceProvider`][agents.tracing.setup.TraceProvider] を作成し、トレースの生成を担当します。
- `TraceProvider` に [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] を設定し、バッチで [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] にトレース／スパンを送信します。この Exporter が OpenAI バックエンドへバッチ送信します。

デフォルト設定をカスタマイズして別または追加のバックエンドへ送信したり、Exporter の挙動を変更したりするには、次の 2 つの方法があります。

1. [`add_trace_processor()`][agents.tracing.add_trace_processor]  
   既定のプロセッサに **追加** してトレースプロセッサーを登録します。これにより OpenAI バックエンドへの送信に加え、独自処理が可能です。
2. [`set_trace_processors()`][agents.tracing.set_trace_processors]  
   既定のプロセッサを **置き換え** て独自トレースプロセッサーを設定します。この場合、OpenAI バックエンドへ送信したい場合は、その機能を持つ `TracingProcessor` を含める必要があります。

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