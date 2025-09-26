---
search:
  exclude: true
---
# トレーシング

Agents SDK には組み込みのトレーシングがあり、エージェントの実行中に発生するイベント（ LLM 生成、ツール呼び出し、ハンドオフ、ガードレール、カスタムイベント）を包括的に記録します。 [Traces ダッシュボード](https://platform.openai.com/traces)を使用すると、開発時や本番環境でワークフローをデバッグ、可視化、監視できます。

!!!note

    トレーシングはデフォルトで有効です。無効にする方法は 2 つあります:

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定して、グローバルにトレーシングを無効化できます
    2. [`agents.run.RunConfig.tracing_disabled`][] を `True` に設定して、単一の実行に対してトレーシングを無効化できます

***OpenAI の API を使用し Zero Data Retention (ZDR) ポリシーで運用している組織では、トレーシングは利用できません。***

## トレースとスパン

- **トレース** は「ワークフロー」の単一のエンドツーエンド処理を表します。スパンで構成されます。トレースには次のプロパティがあります:
    - `workflow_name`: 論理的なワークフローやアプリです。例: "Code generation" や "Customer service"
    - `trace_id`: トレースの一意の ID。指定しない場合は自動生成されます。形式は `trace_<32_alphanumeric>` である必要があります。
    - `group_id`: オプションのグループ ID。会話内の複数のトレースを関連付けます。例: チャットスレッド ID を使うことができます。
    - `disabled`: True の場合、トレースは記録されません。
    - `metadata`: トレースのオプションのメタデータ。
- **スパン** は開始時刻と終了時刻を持つ処理を表します。スパンには次が含まれます:
    - `started_at` と `ended_at` タイムスタンプ
    - 所属するトレースを表す `trace_id`
    - このスパンの親スパン（存在する場合）を指す `parent_id`
    - スパンに関する情報である `span_data`。例えば、`AgentSpanData` はエージェントに関する情報、`GenerationSpanData` は LLM 生成に関する情報を含みます。

## デフォルトのトレーシング

デフォルトでは、 SDK は次をトレースします:

- 全体の `Runner.{run, run_sync, run_streamed}()` が `trace()` でラップされます。
- エージェントが実行されるたびに、`agent_span()` でラップされます
- LLM 生成は `generation_span()` でラップされます
- 関数ツールの呼び出しはそれぞれ `function_span()` でラップされます
- ガードレールは `guardrail_span()` でラップされます
- ハンドオフは `handoff_span()` でラップされます
- 音声入力（音声認識）は `transcription_span()` でラップされます
- 音声出力（テキスト読み上げ）は `speech_span()` でラップされます
- 関連する音声スパンは `speech_group_span()` の下に親付けされる場合があります

デフォルトでは、トレース名は "Agent workflow" です。`trace` を使用してこの名前を設定できます。または、[`RunConfig`][agents.run.RunConfig] で名前やその他のプロパティを構成できます。

さらに、[カスタム トレーシング プロセッサー](#custom-tracing-processors)を設定して、トレースを他の送信先へ送ることができます（置き換え、またはセカンダリ送信先として）。

## 上位レベルのトレース

`run()` への複数回の呼び出しを単一のトレースにまとめたい場合があります。これは、コード全体を `trace()` でラップすることで実現できます。

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

1. `Runner.run` への 2 回の呼び出しが `with trace()` でラップされているため、個々の実行は 2 つのトレースを作成するのではなく、全体のトレースの一部になります。

## トレースの作成

[`trace()`][agents.tracing.trace] 関数を使用してトレースを作成できます。トレースは開始と終了が必要です。方法は 2 つあります:

1. 推奨: トレースをコンテキストマネージャとして使用します（例: `with trace(...) as my_trace`）。これにより、適切なタイミングでトレースが自動的に開始・終了されます。
2. [`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を手動で呼び出すこともできます。

現在のトレースは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) を通じて追跡されます。これにより、自動的に並行処理で動作します。トレースを手動で開始/終了する場合は、現在のトレースを更新するために `start()`/`finish()` に `mark_as_current` および `reset_current` を渡す必要があります。

## スパンの作成

さまざまな [`*_span()`][agents.tracing.create] メソッドを使用してスパンを作成できます。一般に、スパンを手動で作成する必要はありません。カスタムのスパン情報を追跡するための [`custom_span()`][agents.tracing.custom_span] 関数が用意されています。

スパンは自動的に現在のトレースの一部となり、Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) によって追跡される、最も近い現在のスパンの下にネストされます。

## 機微データ

一部のスパンは機微なデータを収集する可能性があります。

`generation_span()` は LLM 生成の入出力を保存し、`function_span()` は関数呼び出しの入出力を保存します。これらには機微なデータが含まれる可能性があるため、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] によってそのデータの収集を無効化できます。

同様に、音声スパンにはデフォルトで入出力音声の base64 エンコードされた PCM データが含まれます。この音声データの収集は、[`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] を設定して無効化できます。

## カスタム トレーシング プロセッサー

トレーシングの高レベルなアーキテクチャは次のとおりです:

- 初期化時に、トレースの作成を担うグローバルな [`TraceProvider`][agents.tracing.setup.TraceProvider] を作成します。
- `TraceProvider` に [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] を構成し、バッチでトレース/スパンを [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] に送信します。これがスパンとトレースを OpenAI のバックエンドにバッチでエクスポートします。

このデフォルト設定をカスタマイズして、別のバックエンドに送信したり、追加のバックエンドへも送る、またはエクスポーターの動作を変更するには、次の 2 つの方法があります:

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] は、生成済みのトレースとスパンを受け取る「追加の」トレースプロセッサーを追加できます。これにより、 OpenAI のバックエンドへの送信に加えて、独自の処理を実行できます。
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] は、デフォルトのプロセッサーを独自のトレースプロセッサーに「置き換え」できます。つまり、 OpenAI のバックエンドにトレースを送信したい場合は、それを行う `TracingProcessor` を含めない限り、トレースは送信されません。


## OpenAI 以外のモデルでのトレーシング

トレーシングを無効化する必要なく、 OpenAI API キーを OpenAI 以外のモデルで使用して、 OpenAI Traces ダッシュボードで無料のトレーシングを有効にできます。

```python
import os
from agents import set_tracing_export_api_key, Agent, Runner
from agents.extensions.models.litellm_model import LitellmModel

tracing_api_key = os.environ["OPENAI_API_KEY"]
set_tracing_export_api_key(tracing_api_key)

model = LitellmModel(
    model="your-model-name",
    api_key="your-api-key",
)

agent = Agent(
    name="Assistant",
    model=model,
)
```

## 注意事項
- 無料のトレースは OpenAI Traces ダッシュボードで閲覧できます。


## 外部トレーシング プロセッサー一覧

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
- [Agenta](https://docs.agenta.ai/observability/integrations/openai-agents)