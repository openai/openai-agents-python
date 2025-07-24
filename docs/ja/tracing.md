---
search:
  exclude: true
---
# トレーシング

Agents SDK には組み込みのトレーシング機能があり、エージェント実行中のイベントを包括的に記録します： LLM 生成、ツール呼び出し、ハンドオフ、ガードレール、さらにカスタムイベントまで取得します。 [Traces ダッシュボード](https://platform.openai.com/traces) を使用すると、開発中や本番環境でワークフローをデバッグ・可視化・監視できます。

!!!note

    トレーシングはデフォルトで有効になっています。無効化する方法は 2 つあります。

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定して、グローバルにトレーシングを無効化する  
    2. 単一の実行に対しては [`agents.run.RunConfig.tracing_disabled`][] を `True` に設定する

***OpenAI の API を Zero Data Retention (ZDR) ポリシーで利用している組織では、トレーシングは利用できません。***

## トレースとスパン

- **トレース** は 1 回のエンドツーエンドの「ワークフロー」操作を表し、複数のスパンで構成されます。トレースには次のプロパティがあります：  
    - `workflow_name`：論理的なワークフローまたはアプリ名。例：`"Code generation"` や `"Customer service"`  
    - `trace_id`：トレースの一意の ID。指定しない場合は自動生成されます。形式は `trace_<32_alphanumeric>`  
    - `group_id`：オプションのグループ ID。同じ会話からの複数トレースを関連付けるために使用します。例としてチャットスレッド ID を利用できます。  
    - `disabled`：`True` の場合、このトレースは記録されません。  
    - `metadata`：トレースに付随するオプションのメタデータ。  
- **スパン** は開始時刻と終了時刻を持つ操作を表します。スパンには次の情報があります：  
    - `started_at` と `ended_at` のタイムスタンプ  
    - 所属するトレースを示す `trace_id`  
    - 親スパンを指す `parent_id`（存在する場合）  
    - スパンに関する情報を持つ `span_data`。例：`AgentSpanData` はエージェント情報、`GenerationSpanData` は LLM 生成情報など。  

## デフォルトのトレーシング

デフォルトで SDK は次をトレースします。

- `Runner.{run, run_sync, run_streamed}()` 全体を `trace()` でラップ  
- エージェント実行ごとに `agent_span()` でラップ  
- LLM 生成を `generation_span()` でラップ  
- 関数ツール呼び出しを `function_span()` でラップ  
- ガードレールを `guardrail_span()` でラップ  
- ハンドオフを `handoff_span()` でラップ  
- 音声入力（音声→テキスト）を `transcription_span()` でラップ  
- 音声出力（テキスト→音声）を `speech_span()` でラップ  
- 関連する音声スパンは `speech_group_span()` の下にネストされる場合があります  

デフォルトではトレース名は `"Agent workflow"` です。`trace` を使用して名前を設定するか、[`RunConfig`][agents.run.RunConfig] で名前やその他プロパティを設定できます。

さらに、[カスタムトレースプロセッサー](#custom-tracing-processors) を設定して、トレースを別の送信先へ転送（置き換えまたは追加の送信先として）することもできます。

## 上位レベルのトレース

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

1. 2 回の `Runner.run` 呼び出しが `with trace()` でラップされているため、個別のトレースは作成されず、両方の実行が 1 つのトレースに含まれます。

## トレースの作成

[`trace()`][agents.tracing.trace] 関数を使用してトレースを作成できます。トレースは開始と終了が必要で、方法は 2 通りあります。

1. **推奨**：コンテキストマネージャーとして使用する（例：`with trace(...) as my_trace`）。開始と終了が自動で行われます。  
2. [`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を手動で呼び出す。  

現在のトレースは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で管理されるため、並行処理でも自動で動作します。トレースを手動で開始／終了する場合は、`start()`／`finish()` に `mark_as_current` と `reset_current` を渡して現在のトレースを更新してください。

## スパンの作成

各種 [`*_span()`][agents.tracing.create] メソッドでスパンを作成できますが、手動作成は通常不要です。カスタム情報を追跡したい場合は [`custom_span()`][agents.tracing.custom_span] を利用してください。

スパンは自動的に現在のトレースに含まれ、最も近い現在のスパンの下にネストされます。この情報も Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) で管理されます。

## 機密データ

一部のスパンは機密性の高いデータを記録する可能性があります。

`generation_span()` は LLM の入出力を、`function_span()` は関数呼び出しの入出力を保存します。機密データが含まれる場合は、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] で記録を無効化できます。

同様に、オーディオスパンはデフォルトで base64 エンコードされた PCM データ（入力および出力音声）を含みます。これを無効化するには、[`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] を設定してください。

## カスタムトレーシングプロセッサー

トレーシングの高レベルアーキテクチャは次のとおりです。

- 初期化時に、トレースを作成するグローバル [`TraceProvider`][agents.tracing.setup.TraceProvider] を生成します。  
- `TraceProvider` に [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] を設定し、スパン／トレースをバッチで [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] へ送信し、OpenAI バックエンドへエクスポートします。  

デフォルト設定をカスタマイズして別のバックエンドへ送信したり、エクスポーターの挙動を変更したりする場合は次の 2 つの方法があります。

1. [`add_trace_processor()`][agents.tracing.add_trace_processor]  
   追加のトレースプロセッサーを登録し、生成されたトレース／スパンを受け取って独自処理を行います。OpenAI バックエンドへの送信に加えて処理を追加できます。  
2. [`set_trace_processors()`][agents.tracing.set_trace_processors]  
   デフォルトのプロセッサーを置き換えます。OpenAI バックエンドへ送信したい場合は、その機能を持つ `TracingProcessor` を自分で含める必要があります。  

## 外部トレーシングプロセッサー一覧

- Weights & Biases
- Arize-Phoenix
- Future AGI
- MLflow (self-hosted/OSS
- MLflow (Databricks hosted
- Braintrust
- Pydantic Logfire
- AgentOps
- Scorecard
- Keywords AI
- LangSmith
- Maxim AI
- Comet Opik
- Langfuse
- Langtrace
- Okahu-Monocle
- Galileo
- Portkey AI