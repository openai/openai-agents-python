---
search:
  exclude: true
---
# トレーシング

Agents SDK には組み込みのトレーシングが含まれており、エージェント実行中のイベント（ LLM 生成、ツール呼び出し、ハンドオフ、ガードレール、さらに発生したカスタムイベント）を包括的に記録します。[Traces ダッシュボード](https://platform.openai.com/traces) を使用すると、開発中および本番環境でワークフローをデバッグ、可視化、監視できます。

!!!note

    トレーシングはデフォルトで有効です。一般的には次の 3 つの方法で無効化できます。

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定して、グローバルにトレーシングを無効化できます
    2. [`set_tracing_disabled(True)`][agents.set_tracing_disabled] を使って、コード上でグローバルにトレーシングを無効化できます
    3. [`agents.run.RunConfig.tracing_disabled`][] を `True` に設定して、単一の実行でトレーシングを無効化できます

***OpenAI の API を使用し、Zero Data Retention ( ZDR ) ポリシーの下で運用している組織では、トレーシングは利用できません。***

## Traces と spans

-   **Traces** は 1 つの「ワークフロー」のエンドツーエンドの単一操作を表します。これは Span で構成されます。Traces には次のプロパティがあります。
    -   `workflow_name`: 論理的なワークフローまたはアプリです。例: 「Code generation」や「Customer service」。
    -   `trace_id`: トレースの一意な ID です。指定しない場合は自動生成されます。形式は `trace_<32_alphanumeric>` である必要があります。
    -   `group_id`: 任意のグループ ID で、同じ会話内の複数のトレースを関連付けます。たとえば、チャットスレッド ID を使用できます。
    -   `disabled`: True の場合、そのトレースは記録されません。
    -   `metadata`: トレースの任意のメタデータです。
-   **Spans** は開始時刻と終了時刻を持つ操作を表します。Spans には次が含まれます。
    -   `started_at` と `ended_at` のタイムスタンプ。
    -   `trace_id`（所属するトレースを表します）
    -   `parent_id`（この Span の親 Span を指します。存在する場合）
    -   `span_data`（ Span に関する情報）。たとえば、`AgentSpanData` は Agent の情報、`GenerationSpanData` は LLM 生成の情報を含みます。

## デフォルトトレーシング

デフォルトで、 SDK は次をトレースします。

-   `Runner.{run, run_sync, run_streamed}()` 全体は `trace()` でラップされます。
-   エージェントが実行されるたびに、`agent_span()` でラップされます
-   LLM 生成は `generation_span()` でラップされます
-   関数ツール呼び出しはそれぞれ `function_span()` でラップされます
-   ガードレールは `guardrail_span()` でラップされます
-   ハンドオフは `handoff_span()` でラップされます
-   音声入力（ speech-to-text ）は `transcription_span()` でラップされます
-   音声出力（ text-to-speech ）は `speech_span()` でラップされます
-   関連する音声 Span は `speech_group_span()` の配下になる場合があります

デフォルトで、トレース名は「Agent workflow」です。`trace` を使用する場合はこの名前を設定できます。また、[`RunConfig`][agents.run.RunConfig] で名前やその他のプロパティを設定することもできます。

さらに、[カスタムトレースプロセッサー](#custom-tracing-processors) を設定して、トレースを他の送信先にプッシュできます（置き換えまたは副次的な送信先として）。

## 上位レベルトレース

場合によっては、`run()` の複数回呼び出しを 1 つのトレースの一部にしたいことがあります。これは、コード全体を `trace()` でラップすることで実現できます。

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

1. `Runner.run` の 2 回の呼び出しは `with trace()` でラップされているため、2 つのトレースを作成するのではなく、個々の実行が全体トレースの一部になります。

## トレース作成

[`trace()`][agents.tracing.trace] 関数を使用してトレースを作成できます。トレースは開始と終了が必要です。方法は 2 つあります。

1. **推奨**: トレースをコンテキストマネージャーとして使います（例: `with trace(...) as my_trace`）。これにより、適切なタイミングでトレースが自動的に開始・終了されます。
2. [`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を手動で呼び出すこともできます。

現在のトレースは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) を通じて追跡されます。これは並行処理でも自動的に機能することを意味します。トレースを手動で開始・終了する場合は、現在のトレースを更新するために `start()`/`finish()` に `mark_as_current` と `reset_current` を渡す必要があります。

## Span 作成

さまざまな [`*_span()`][agents.tracing.create] メソッドを使って Span を作成できます。一般に、Span を手動で作成する必要はありません。カスタム Span 情報を追跡するための [`custom_span()`][agents.tracing.custom_span] 関数も利用できます。

Span は自動的に現在のトレースの一部となり、最も近い現在の Span の配下にネストされます。これは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で追跡されます。

## 機微データ

一部の Span では、機微データが含まれる可能性があります。

`generation_span()` は LLM 生成の入出力を保存し、`function_span()` は関数呼び出しの入出力を保存します。これらには機微データが含まれる可能性があるため、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] でそのデータの収集を無効化できます。

同様に、Audio Span にはデフォルトで入力・出力音声の base64 エンコードされた PCM データが含まれます。[`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] を設定することで、この音声データの収集を無効化できます。

デフォルトでは、`trace_include_sensitive_data` は `True` です。アプリ実行前に環境変数 `OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA` を `true/1` または `false/0` に設定することで、コードなしでデフォルト値を設定できます。

## カスタムトレーシングプロセッサー

トレーシングの高レベルアーキテクチャは次のとおりです。

-   初期化時に、トレース作成を担うグローバル [`TraceProvider`][agents.tracing.setup.TraceProvider] を作成します。
-   `TraceProvider` に [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] を設定します。これはトレース / Span をバッチで [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] に送信し、さらに Span とトレースを OpenAI バックエンドへバッチでエクスポートします。

このデフォルト設定をカスタマイズして、別のまたは追加のバックエンドにトレースを送信したり、エクスポーターの動作を変更したりするには、次の 2 つの方法があります。

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] を使うと、準備できたトレースと Span を受け取る**追加の**トレースプロセッサーを追加できます。これにより、OpenAI バックエンドへの送信に加えて独自の処理を行えます。
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] を使うと、デフォルトプロセッサーを独自のトレースプロセッサーで**置き換え**できます。これは、`TracingProcessor` を含めない限り、トレースが OpenAI バックエンドへ送信されないことを意味します。


## 非 OpenAI モデルでのトレーシング

トレーシングを無効化しなくても、OpenAI 以外のモデルで OpenAI API キーを使用して OpenAI Traces ダッシュボードで無料トレーシングを有効化できます。アダプター選択とセットアップ時の注意点については、Models ガイドの [Third-party adapters](models/index.md#third-party-adapters) セクションを参照してください。

```python
import os
from agents import set_tracing_export_api_key, Agent, Runner
from agents.extensions.models.any_llm_model import AnyLLMModel

tracing_api_key = os.environ["OPENAI_API_KEY"]
set_tracing_export_api_key(tracing_api_key)

model = AnyLLMModel(
    model="your-provider/your-model-name",
    api_key="your-api-key",
)

agent = Agent(
    name="Assistant",
    model=model,
)
```

単一の実行にのみ別のトレーシングキーが必要な場合は、グローバルエクスポーターを変更する代わりに `RunConfig` 経由で渡してください。

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(tracing={"api_key": "sk-tracing-123"}),
)
```

## 追加メモ
- Openai Traces ダッシュボードで無料トレースを表示します。


## エコシステム統合

以下のコミュニティおよびベンダーの統合は、OpenAI Agents SDK のトレーシング機能をサポートしています。

### 外部トレーシングプロセッサー一覧

-   [Weights & Biases](https://weave-docs.wandb.ai/guides/integrations/openai_agents)
-   [Arize-Phoenix](https://docs.arize.com/phoenix/tracing/integrations-tracing/openai-agents-sdk)
-   [Future AGI](https://docs.futureagi.com/future-agi/products/observability/auto-instrumentation/openai_agents)
-   [MLflow (self-hosted/OSS)](https://mlflow.org/docs/latest/tracing/integrations/openai-agent)
-   [MLflow (Databricks hosted)](https://docs.databricks.com/aws/en/mlflow/mlflow-tracing#-automatic-tracing)
-   [Braintrust](https://braintrust.dev/docs/guides/traces/integrations#openai-agents-sdk)
-   [Pydantic Logfire](https://logfire.pydantic.dev/docs/integrations/llms/openai/#openai-agents)
-   [AgentOps](https://docs.agentops.ai/v1/integrations/agentssdk)
-   [Scorecard](https://docs.scorecard.io/docs/documentation/features/tracing#openai-agents-sdk-integration)
-   [Respan](https://respan.ai/docs/integrations/tracing/openai-agents-sdk)
-   [LangSmith](https://docs.smith.langchain.com/observability/how_to_guides/trace_with_openai_agents_sdk)
-   [Maxim AI](https://www.getmaxim.ai/docs/observe/integrations/openai-agents-sdk)
-   [Comet Opik](https://www.comet.com/docs/opik/tracing/integrations/openai_agents)
-   [Langfuse](https://langfuse.com/docs/integrations/openaiagentssdk/openai-agents)
-   [Langtrace](https://docs.langtrace.ai/supported-integrations/llm-frameworks/openai-agents-sdk)
-   [Okahu-Monocle](https://github.com/monocle2ai/monocle)
-   [Galileo](https://v2docs.galileo.ai/integrations/openai-agent-integration#openai-agent-integration)
-   [Portkey AI](https://portkey.ai/docs/integrations/agents/openai-agents)
-   [LangDB AI](https://docs.langdb.ai/getting-started/working-with-agent-frameworks/working-with-openai-agents-sdk)
-   [Agenta](https://docs.agenta.ai/observability/integrations/openai-agents)
-   [PostHog](https://posthog.com/docs/llm-analytics/installation/openai-agents)
-   [Traccia](https://traccia.ai/docs/integrations/openai-agents)
-   [PromptLayer](https://docs.promptlayer.com/languages/integrations#openai-agents-sdk)