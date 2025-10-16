---
search:
  exclude: true
---
# トレーシング

Agents SDK には組み込みのトレーシングが含まれており、エージェントの実行中に発生するイベント（ LLM 生成、ツール呼び出し、ハンドオフ、ガードレール、さらにはカスタムイベント）を包括的に記録します。[Traces ダッシュボード](https://platform.openai.com/traces)を使用すると、開発中や本番環境でワークフローをデバッグ、可視化、監視できます。

!!!note

    トレーシングはデフォルトで有効です。無効化する方法は 2 つあります。

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定して、トレーシングをグローバルに無効化できます
    2. 1 回の実行に対してのみ無効化するには、[`agents.run.RunConfig.tracing_disabled`][] を `True` に設定します

***OpenAI の API を使用し、Zero Data Retention (ZDR) ポリシーの下で運用している組織では、トレーシングは利用できません。***

## トレースとスパン

- **Traces** は「ワークフロー」の単一のエンドツーエンド操作を表します。スパンで構成されます。トレースには次のプロパティがあります。
    - `workflow_name`: 論理的なワークフローまたはアプリです。例: "Code generation" や "Customer service"。
    - `trace_id`: トレースの一意な ID。指定しない場合は自動生成されます。形式は `trace_<32_alphanumeric>` である必要があります。
    - `group_id`: 任意のグループ ID。同じ会話からの複数のトレースを関連付けるために使用します。例えばチャットスレッド ID を利用できます。
    - `disabled`: True の場合、このトレースは記録されません。
    - `metadata`: トレースの任意のメタデータ。
- **Spans** は開始時刻と終了時刻を持つ操作を表します。スパンには次の情報があります。
    - `started_at` と `ended_at` タイムスタンプ
    - `trace_id`（所属するトレースを表します）
    - `parent_id`（このスパンの親スパンがある場合、その参照）
    - `span_data`（スパンに関する情報）。例えば、`AgentSpanData` はエージェントに関する情報、`GenerationSpanData` は LLM 生成に関する情報を含みます。

## デフォルトのトレーシング

デフォルトで、SDK は次をトレースします。

- `Runner.{run, run_sync, run_streamed}()` 全体が `trace()` でラップされます。
- エージェントが実行されるたびに、`agent_span()` でラップされます
- LLM の生成は `generation_span()` でラップされます
- 関数ツールの呼び出しはそれぞれ `function_span()` でラップされます
- ガードレールは `guardrail_span()` でラップされます
- ハンドオフは `handoff_span()` でラップされます
- 音声入力（音声認識、speech-to-text）は `transcription_span()` でラップされます
- 音声出力（音声合成、text-to-speech）は `speech_span()` でラップされます
- 関連する音声スパンは `speech_group_span()` の下に親子付けされる場合があります

デフォルトでは、トレース名は "Agent workflow" です。`trace` を使用する場合はこの名前を設定できますし、[`RunConfig`][agents.run.RunConfig] で名前やその他のプロパティを設定することもできます。

さらに、[カスタムトレースプロセッサー](#custom-tracing-processors) をセットアップして、トレースを別の送信先へ送る（置き換えや追加の送信先として）ことができます。

## 上位レベルのトレース

`run()` を複数回呼び出す場合でも、それらを 1 つのトレースにまとめたいことがあります。その場合は、コード全体を `trace()` でラップします。

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

[`trace()`][agents.tracing.trace] 関数を使用してトレースを作成できます。トレースは開始と終了が必要です。方法は次の 2 つです。

1. 推奨: トレースをコンテキストマネージャとして使用します（例: `with trace(...) as my_trace`）。これにより、適切なタイミングでトレースが自動的に開始・終了します。
2. [`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を手動で呼び出すこともできます。

現在のトレースは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) によって追跡されます。これにより、並行処理でも自動的に機能します。トレースを手動で開始/終了する場合は、現在のトレースを更新するために `start()`/`finish()` に `mark_as_current` と `reset_current` を渡す必要があります。

## スパンの作成

さまざまな [`*_span()`][agents.tracing.create] メソッドを使用してスパンを作成できます。一般的に、スパンを手動で作成する必要はありません。カスタムスパン情報を追跡するための [`custom_span()`][agents.tracing.custom_span] 関数も利用できます。

スパンは自動的に現在のトレースの一部となり、Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で追跡される、最も近い現在のスパンの下にネストされます。

## センシティブデータ

一部のスパンは、機微なデータを取得する可能性があります。

`generation_span()` は LLM 生成の入力/出力を保存し、`function_span()` は関数呼び出しの入力/出力を保存します。これらには機微なデータが含まれる可能性があるため、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] を使用してそれらのデータの取得を無効化できます。

同様に、音声スパンにはデフォルトで入力および出力音声の base64 エンコードされた PCM データが含まれます。この音声データの取得は、[`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] を設定して無効化できます。

## カスタムトレーシングプロセッサー

トレーシングのハイレベルなアーキテクチャは次のとおりです。

- 初期化時に、トレースの作成を担うグローバルな [`TraceProvider`][agents.tracing.setup.TraceProvider] を作成します。
- `TraceProvider` を [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] で構成し、これがトレース/スパンをバッチで [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] に送信します。`BackendSpanExporter` は OpenAI のバックエンドにスパンとトレースをバッチでエクスポートします。

このデフォルト設定をカスタマイズして、別のバックエンドへ送信したり、追加のバックエンドへ送信したり、エクスポーターの動作を変更するには、次の 2 つの方法があります。

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] は、トレースやスパンの準備ができたタイミングで受け取る、**追加の** トレースプロセッサーを追加できます。これにより、OpenAI のバックエンドへ送信するのに加えて、独自の処理を行えます。
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] は、デフォルトのプロセッサーを独自のトレースプロセッサーに**置き換える**ことができます。つまり、OpenAI のバックエンドにトレースを送信する `TracingProcessor` を含めない限り、トレースはバックエンドに送信されません。

## OpenAI 以外のモデルでのトレーシング

OpenAI の API キーを OpenAI 以外のモデルで使用して、トレーシングを無効化することなく、OpenAI Traces ダッシュボードで無料のトレーシングを有効にできます。

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

## 注意
- OpenAI Traces ダッシュボードで無料のトレースを表示します。

## 外部トレーシングプロセッサー一覧

- [Weights & Biases](https://weave-docs.wandb.ai/guides/integrations/openai_agents)
- [Arize-Phoenix](https://docs.arize.com/phoenix/tracing/integrations-tracing/openai-agents-sdk)
- [Future AGI](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/openai_agents)
- [MLflow（セルフホスト/OSS）](https://mlflow.org/docs/latest/tracing/integrations/openai-agent)
- [MLflow（Databricks ホスト）](https://docs.databricks.com/aws/en/mlflow/mlflow-tracing#-automatic-tracing)
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