---
search:
  exclude: true
---
# トレーシング

Agents SDKにはトレーシング機能が組み込まれており、エージェントの実行中に発生するイベント（LLMの生成、ツール呼び出し、ハンドオフ、ガードレール、さらには発生したカスタムイベント）の包括的な記録を収集します。 [Traces ダッシュボード](https://platform.openai.com/traces) を使って、開発中および本番環境でワークフローのデバッグ、可視化、監視ができます。

!!!note

    トレーシングは既定で有効です。無効にする方法は 2 つあります:

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定して、トレーシングをグローバルに無効化できます
    2. 単一の実行に対しては、[`agents.run.RunConfig.tracing_disabled`][] を `True` に設定して無効化できます

***OpenAIの API を使用し、 Zero Data Retention（ZDR）ポリシーのもとで運用している組織では、トレーシングは利用できません。***

## トレースとスパン

-   **トレース** は、1 つの「ワークフロー」のエンドツーエンドの処理を表します。スパンで構成されます。トレースには次のプロパティがあります:
    -   `workflow_name`: 論理的なワークフローまたはアプリです。例: "Code generation" や "Customer service"
    -   `trace_id`: トレースの一意の ID。指定しない場合は自動生成されます。形式は `trace_<32_alphanumeric>` である必要があります。
    -   `group_id`: 同一の会話からの複数トレースを関連付けるための任意のグループ ID。たとえばチャットスレッドの ID など
    -   `disabled`: True の場合、このトレースは記録されません。
    -   `metadata`: トレースの任意のメタデータ
-   **スパン** は、開始時刻と終了時刻を持つ処理を表します。スパンには次が含まれます:
    -   `started_at` と `ended_at` のタイムスタンプ
    -   所属するトレースを表す `trace_id`
    -   親スパン（存在する場合）を指す `parent_id`
    -   スパンに関する情報である `span_data`。たとえば、`AgentSpanData` はエージェントに関する情報を、`GenerationSpanData` は LLMの生成に関する情報を含みます。

## 既定のトレーシング

既定では、Agents SDKは次をトレースします:

-   全体の `Runner.{run, run_sync, run_streamed}()` が `trace()` でラップされます
-   エージェントが実行されるたびに、`agent_span()` でラップされます
-   LLMの生成は `generation_span()` でラップされます
-   関数ツールの呼び出しは、それぞれ `function_span()` でラップされます
-   ガードレールは `guardrail_span()` でラップされます
-   ハンドオフは `handoff_span()` でラップされます
-   音声入力（speech-to-text）は `transcription_span()` でラップされます
-   音声出力（text-to-speech）は `speech_span()` でラップされます
-   関連する音声スパンは、`speech_group_span()` の配下にまとめられる場合があります

既定では、トレース名は「Agent workflow」です。`trace` を使う場合にこの名前を設定でき、または [`RunConfig`][agents.run.RunConfig] で名前やその他のプロパティを構成できます。

加えて、[custom trace processors](#custom-tracing-processors) を設定して、トレースを他の送信先へ送信できます（置き換え先、またはセカンダリの送信先として）。

## より高レベルのトレース

複数回の `run()` 呼び出しを 1 つのトレースに含めたい場合があります。その場合は、コード全体を `trace()` でラップします。

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

1. `with trace()` で 2 回の `Runner.run` 呼び出しをラップしているため、個々の実行は 2 つのトレースを作成するのではなく、全体のトレースの一部になります。

## トレースの作成

[`trace()`][agents.tracing.trace] 関数でトレースを作成できます。トレースは開始と終了が必要で、方法は 2 つあります:

1. 推奨: トレースをコンテキストマネージャーとして使用します（例: `with trace(...) as my_trace`）。これにより、適切なタイミングで自動的に開始・終了されます。
2. また、[`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を手動で呼び出すこともできます。

現在のトレースは、 Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) を介して追跡されます。これは、並行処理でも自動的に機能することを意味します。手動でトレースを開始/終了する場合は、現在のトレースを更新するために、`start()`/`finish()` に `mark_as_current` と `reset_current` を渡す必要があります。

## スパンの作成

各種の [`*_span()`][agents.tracing.create] メソッドでスパンを作成できます。通常、スパンを手動で作成する必要はありません。カスタムのスパン情報を追跡するための [`custom_span()`][agents.tracing.custom_span] も利用できます。

スパンは自動的に現在のトレースの一部となり、 Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) によって追跡される、最も近い現在のスパンの配下にネストされます。

## 機密データ

一部のスパンでは、機密になり得るデータが取得される場合があります。

`generation_span()` は LLMの生成の入力と出力を保存し、`function_span()` は関数呼び出しの入力と出力を保存します。これらには機密データが含まれる可能性があるため、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] でそのデータの取得を無効化できます。

同様に、音声関連のスパンには、既定で入力音声と出力音声の base64 でエンコードされた PCM データが含まれます。[`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] を設定して、この音声データの取得を無効化できます。

## カスタムトレーシングプロセッサー

トレーシングの高レベルなアーキテクチャは次のとおりです:

-   初期化時に、グローバルな [`TraceProvider`][agents.tracing.setup.TraceProvider] を作成します。これはトレースの作成を担当します。
-   `TraceProvider` を [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] で構成し、トレース/スパンをバッチで [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] に送信します。`BackendSpanExporter` は、これらのスパンとトレースを OpenAIバックエンドへバッチでエクスポートします。

既定のセットアップをカスタマイズして、別のバックエンドや追加のバックエンドにトレースを送信したり、エクスポーターの挙動を変更したりするには、次の 2 つの方法があります:

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] は、トレースやスパンが準備でき次第受け取る、追加のトレースプロセッサーを追加できます。これにより、OpenAIのバックエンドにトレースを送信するのに加えて、独自の処理を行えます。
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] は、既定のプロセッサーを独自のトレースプロセッサーに置き換えられます。これにより、該当の処理を行う TracingProcessor を含めない限り、トレースは OpenAIバックエンドに送信されません。

## OpenAI以外のモデルでのトレーシング

トレーシングを無効化することなく、 OpenAI Traces ダッシュボードで無料のトレーシングを有効にするために、OpenAIの API キーを OpenAI以外のモデルで使用できます。

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
- 無料のトレースは OpenAI Traces ダッシュボードで表示できます。

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
-   [LangDB AI](https://docs.langdb.ai/getting-started/working-with-agent-frameworks/working-with-openai-agents-sdk)