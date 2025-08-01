---
search:
  exclude: true
---
# トレーシング

Agents SDK にはトレーシング機能が組み込まれており、エージェント実行中に発生する LLM 生成、ツール呼び出し、ハンドオフ、ガードレール、さらにはカスタムイベントまで、イベントの詳細な記録を収集します。 [Traces ダッシュボード](https://platform.openai.com/traces) を利用すると、開発時および本番環境でワークフローをデバッグ、可視化、モニタリングできます。

!!!note

    トレーシングはデフォルトで有効になっています。無効にする方法は 2 つあります。

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定してグローバルに無効化する  
    2. 1 回の実行に対してのみ、[`agents.run.RunConfig.tracing_disabled`][] を `True` に設定する

***OpenAI の API を ZDR (Zero Data Retention) ポリシーで利用している組織では、トレーシングを利用できません。***

## Traces と Spans

- **Trace** は 1 つの「ワークフロー」のエンドツーエンド操作を表します。Trace は次のプロパティを持ちます。  
    - `workflow_name`: 論理的なワークフローまたはアプリ名。例: 「Code generation」や「Customer service」。  
    - `trace_id`: Trace の一意 ID。渡さない場合は自動生成されます。形式は `trace_<32_alphanumeric>` である必要があります。  
    - `group_id`: 任意のグループ ID。同じ会話からの複数 Trace を関連付けるために使用します。たとえばチャットスレッド ID など。  
    - `disabled`: `True` の場合、その Trace は記録されません。  
    - `metadata`: Trace に付与する任意のメタデータ。  
- **Span** は開始時刻と終了時刻を持つ操作を表します。Span には次があります。  
    - `started_at` と `ended_at` タイムスタンプ  
    - 所属する Trace を示す `trace_id`  
    - 親 Span を指す `parent_id` (存在する場合)  
    - Span の情報を含む `span_data`。例: `AgentSpanData` はエージェント情報、`GenerationSpanData` は LLM 生成情報など。  

## デフォルトのトレーシング

デフォルトでは、SDK は以下をトレースします。

- `Runner.{run, run_sync, run_streamed}()` 全体が `trace()` でラップされます
- エージェントが実行されるたびに `agent_span()` でラップ
- LLM 生成は `generation_span()` でラップ
- 関数ツール呼び出しは `function_span()` でラップ
- ガードレールは `guardrail_span()` でラップ
- ハンドオフは `handoff_span()` でラップ
- 音声入力 (speech-to-text) は `transcription_span()` でラップ
- 音声出力 (text-to-speech) は `speech_span()` でラップ
- 関連音声 Span は `speech_group_span()` の下に入る場合があります

Trace 名はデフォルトで「Agent workflow」です。`trace` を使用してこの名前を設定するか、[`RunConfig`][agents.run.RunConfig] で名前やその他プロパティを構成できます。

さらに、[カスタムトレースプロセッサー](#custom-tracing-processors) を設定して、別の宛先へ Trace を送信することもできます (置き換えや追加送信)。

## 高レベル Trace

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

1. `Runner.run` の 2 回の呼び出しが `with trace()` に包まれているため、個々の実行は 2 つの Trace を作成するのではなく、1 つの大きな Trace の一部になります。

## Trace の作成

[`trace()`][agents.tracing.trace] 関数を使って Trace を作成できます。Trace は開始と終了が必要で、方法は 2 通りあります。

1. **推奨**: コンテキストマネージャとして使用する (例: `with trace(...) as my_trace`)。適切なタイミングで自動的に開始・終了します。  
2. [`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を手動で呼び出す。

現在の Trace は Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で管理されています。これにより自動的に並行処理に対応します。Trace を手動で開始・終了する場合は、`start()`/`finish()` に `mark_as_current` と `reset_current` を渡して現在の Trace を更新してください。

## Span の作成

各種 [`*_span()`][agents.tracing.create] メソッドで Span を作成できます。通常、Span を手動で作成する必要はありません。カスタム情報を追跡するための [`custom_span()`][agents.tracing.custom_span] も利用できます。

Span は自動的に現在の Trace の一部となり、Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で追跡されている最も近い現在の Span の下にネストされます。

## 機微データ

一部の Span では機微データが記録される可能性があります。

`generation_span()` は LLM 生成の入力 / 出力を、`function_span()` は関数呼び出しの入力 / 出力を保存します。機微データを含む場合があるため、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] でデータの保存を無効化できます。

同様に、Audio Span にはデフォルトで入出力音声の base64 エンコード PCM データが含まれます。[`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] を設定して音声データの保存を無効化できます。

## カスタムトレースプロセッサー

トレーシングの高レベル構成は次のとおりです。

- 初期化時にグローバルな [`TraceProvider`][agents.tracing.setup.TraceProvider] を生成し、Trace を作成します。  
- `TraceProvider` に [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] を構成し、これが Span と Trace をバッチ送信で [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] に渡し、OpenAI バックエンドへエクスポートします。  

デフォルト設定をカスタマイズして別のバックエンドへ送信したり、エクスポーターの動作を変更したりするには次の 2 つの方法があります。

1. [`add_trace_processor()`][agents.tracing.add_trace_processor]  
   既存の送信に **追加** する形で Trace プロセッサーを登録できます。これにより、OpenAI バックエンドへの送信に加えて独自処理を実行可能です。  
2. [`set_trace_processors()`][agents.tracing.set_trace_processors]  
   デフォルトのプロセッサーを **置き換え** ます。OpenAI バックエンドに送信したい場合は、その処理を行う `TracingProcessor` を含める必要があります。  

## 非 OpenAI モデルでのトレーシング

非 OpenAI モデルでも、OpenAI API キーを使用することで Traces ダッシュボードでの無償トレーシングが可能です。トレーシングを無効化する必要はありません。

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

## Notes
- OpenAI Traces ダッシュボードで無償トレースを閲覧できます。

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