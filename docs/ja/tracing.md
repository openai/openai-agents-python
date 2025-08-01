---
search:
  exclude: true
---
# トレーシング

Agents SDK にはトレーシング機能が組み込まれており、エージェント実行中の LLM 生成、ツール呼び出し、ハンドオフ、ガードレール、さらにはカスタムイベントまで、イベントの包括的な記録を収集します。 [Traces ダッシュボード](https://platform.openai.com/traces) を使用すると、開発中および本番環境でワークフローをデバッグ、可視化、監視できます。

!!!note

    トレーシングはデフォルトで有効になっています。無効にする方法は 2 つあります。

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定して、グローバルにトレーシングを無効化する  
    2. [`agents.run.RunConfig.tracing_disabled`][] を `True` に設定して、単一の実行でトレーシングを無効化する

***OpenAI の API を Zero Data Retention (ZDR) ポリシーで運用している組織では、トレーシングを利用できません。***

## トレースとスパン

- **トレース** は「ワークフロー」の単一のエンドツーエンド操作を表します。トレースはスパンで構成されます。トレースには以下のプロパティがあります。  
    - `workflow_name`：論理的なワークフローまたはアプリ名。例： "Code generation" や "Customer service"  
    - `trace_id`：トレースの一意 ID。指定しない場合は自動生成されます。形式は `trace_<32_alphanumeric>` である必要があります。  
    - `group_id`：任意のグループ ID。同じ会話からの複数トレースを関連付けるために使用します。例としてチャットスレッド ID など。  
    - `disabled`：`True` の場合、トレースは記録されません。  
    - `metadata`：トレースの任意メタデータ。  
- **スパン** は開始時刻と終了時刻を持つ操作を表します。スパンには以下があります。  
    - `started_at` と `ended_at` タイムスタンプ  
    - 所属するトレースを示す `trace_id`  
    - 親スパンを指す `parent_id`（存在する場合）  
    - スパンに関する情報を持つ `span_data`。例えば `AgentSpanData` はエージェント情報、`GenerationSpanData` は LLM 生成情報など。  

## デフォルトのトレーシング

デフォルトでは、SDK は以下をトレースします。

- `Runner.{run, run_sync, run_streamed}()` 全体が `trace()` でラップされます。  
- エージェントが実行されるたびに `agent_span()` でラップされます。  
- LLM 生成は `generation_span()` でラップされます。  
- 関数ツール呼び出しはそれぞれ `function_span()` でラップされます。  
- ガードレールは `guardrail_span()` でラップされます。  
- ハンドオフは `handoff_span()` でラップされます。  
- 音声入力 (speech-to-text) は `transcription_span()` でラップされます。  
- 音声出力 (text-to-speech) は `speech_span()` でラップされます。  
- 関連する音声スパンは `speech_group_span()` の下に配置される場合があります。  

トレース名はデフォルトで "Agent workflow" です。`trace` を使用して名前を設定するか、 [`RunConfig`][agents.run.RunConfig] で名前やその他のプロパティを構成できます。

さらに、[カスタムトレーシングプロセッサー](#custom-tracing-processors) を設定して、別の送信先にトレースをプッシュする（置き換えまたは追加送信先として）ことも可能です。

## 高レベルのトレース

複数回の `run()` 呼び出しを 1 つのトレースにまとめたい場合は、コード全体を `trace()` でラップします。

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

1. `with trace()` で 2 回の `Runner.run` 呼び出しをラップしているため、個々の実行は 2 つのトレースを作成せず、全体トレースの一部になります。

## トレースの作成

[`trace()`][agents.tracing.trace] 関数を使用してトレースを作成できます。トレースは開始と終了が必要で、方法は 2 つあります。

1. **推奨**：コンテキストマネージャとしてトレースを使用する（例： `with trace(...) as my_trace`）。これにより適切なタイミングで自動的に開始・終了します。  
2. [`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を手動で呼び出す。  

現在のトレースは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で追跡されます。これにより並行処理でも自動的に機能します。トレースを手動で開始・終了する場合は、`start()` と `finish()` に `mark_as_current` と `reset_current` を渡して現在のトレースを更新する必要があります。

## スパンの作成

各種 [`*_span()`][agents.tracing.create] メソッドでスパンを作成できますが、通常は手動で作成する必要はありません。カスタムスパン情報を追跡するために [`custom_span()`][agents.tracing.custom_span] が用意されています。

スパンは自動的に現在のトレースの一部となり、Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で追跡される最も近い現在のスパンの下にネストされます。

## 機微データ

一部のスパンは機微データを取得する可能性があります。

`generation_span()` は LLM 生成の入力/出力を、`function_span()` は関数呼び出しの入力/出力を保存します。これらに機微データが含まれる場合があるため、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] でデータ取得を無効化できます。

同様に、オーディオスパンはデフォルトで入力・出力オーディオの base64 エンコード PCM データを含みます。このオーディオデータ取得は [`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] を設定して無効化できます。

## カスタムトレーシングプロセッサー

トレーシングの高レベルアーキテクチャは以下のとおりです。

- 初期化時にグローバル [`TraceProvider`][agents.tracing.setup.TraceProvider] を作成し、トレース生成を担当します。  
- `TraceProvider` に [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] を設定し、トレース/スパンをバッチで [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] に送信します。エクスポーターはバッチで OpenAI バックエンドへスパンとトレースをエクスポートします。  

デフォルト設定をカスタマイズし、別のバックエンドへ送信したり、エクスポーター動作を変更したりするには 2 つの方法があります。

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] を使用して **追加** のトレースプロセッサーを登録し、トレース/スパンを受け取らせる。これにより、OpenAI バックエンドへの送信に加えて独自処理を行えます。  
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] を使用してデフォルトプロセッサーを **置き換え** ます。この場合、OpenAI バックエンドへトレースを送信するには、その機能を持つ `TracingProcessor` を含める必要があります。  

## 非 OpenAI モデルでのトレーシング

OpenAI API キーを使用して非 OpenAI モデルでもトレーシングを有効にでき、トレーシングを無効化する必要なく OpenAI Traces ダッシュボードで無料トレースを利用できます。

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

## 備考
- 無料トレースは OpenAI Traces ダッシュボードで確認できます。

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