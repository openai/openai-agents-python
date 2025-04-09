# トレーシング

Agents SDK には組み込みのトレーシング機能があり、エージェントの実行中に発生するイベント（LLM の生成、ツール呼び出し、ハンドオフ、ガードレール、さらにはカスタムイベントまで）を包括的に記録します。[Traces ダッシュボード](https://platform.openai.com/traces) を使用して、開発中および本番環境でワークフローのデバッグ、可視化、監視が可能です。

!!!note

    トレーシングはデフォルトで有効になっています。トレーシングを無効にする方法は 2 つあります。

    1. 環境変数 `OPENAI_AGENTS_DISABLE_TRACING=1` を設定して、グローバルにトレーシングを無効化できます。
    2. 個別の実行でトレーシングを無効化するには、[`agents.run.RunConfig.tracing_disabled`][] を `True` に設定します。

***OpenAI の API を使用してゼロデータ保持（ZDR）ポリシーで運用している組織の場合、トレーシングは利用できません。***

## トレースとスパン

- **トレース（Traces）** は、ワークフローの単一のエンドツーエンド操作を表します。トレースは複数のスパン（Spans）で構成されます。トレースには以下のプロパティがあります。
    - `workflow_name`：論理的なワークフローまたはアプリ名。例：「Code generation」や「Customer service」。
    - `trace_id`：トレースの一意な ID。指定しない場合は自動生成されます。形式は `trace_<32_alphanumeric>` である必要があります。
    - `group_id`：同じ会話からの複数のトレースを関連付けるためのオプションのグループ ID。例えば、チャットスレッド ID を使用できます。
    - `disabled`：True の場合、このトレースは記録されません。
    - `metadata`：トレースに関するオプションのメタデータ。
- **スパン（Spans）** は、開始時刻と終了時刻を持つ操作を表します。スパンには以下が含まれます。
    - `started_at` と `ended_at` のタイムスタンプ。
    - 所属するトレースを示す `trace_id`。
    - 親スパンを指す `parent_id`（存在する場合）。
    - スパンに関する情報を含む `span_data`。例えば、`AgentSpanData` はエージェントに関する情報を、`GenerationSpanData` は LLM の生成に関する情報を含みます。

## デフォルトのトレーシング

デフォルトでは、SDK は以下をトレースします。

- `Runner.{run, run_sync, run_streamed}()` 全体が `trace()` でラップされます。
- エージェントが実行されるたびに `agent_span()` でラップされます。
- LLM の生成は `generation_span()` でラップされます。
- 関数ツールの呼び出しはそれぞれ `function_span()` でラップされます。
- ガードレールは `guardrail_span()` でラップされます。
- ハンドオフは `handoff_span()` でラップされます。
- 音声入力（音声からテキスト）は `transcription_span()` でラップされます。
- 音声出力（テキストから音声）は `speech_span()` でラップされます。
- 関連する音声スパンは `speech_group_span()` の下にまとめられる場合があります。

デフォルトでは、トレース名は「Agent trace」です。`trace` を使用する場合、この名前を設定できます。また、[`RunConfig`][agents.run.RunConfig] を使用して名前やその他のプロパティを設定できます。

さらに、[カスタムトレースプロセッサ](#custom-tracing-processors) を設定して、トレースを他の宛先に送信することも可能です（置き換えまたは追加の宛先として）。

## 上位レベルのトレース

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

1. 2 回の `Runner.run` 呼び出しが `with trace()` でラップされているため、個別の実行はそれぞれ別のトレースを作成するのではなく、全体のトレースの一部になります。

## トレースの作成

トレースを作成するには [`trace()`][agents.tracing.trace] 関数を使用します。トレースは開始と終了が必要です。以下の 2 つの方法があります。

1. **推奨**：コンテキストマネージャとして使用します（例：`with trace(...) as my_trace`）。これによりトレースの開始と終了が自動的に行われます。
2. 手動で [`trace.start()`][agents.tracing.Trace.start] と [`trace.finish()`][agents.tracing.Trace.finish] を呼び出すこともできます。

現在のトレースは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) を介して追跡されます。これにより、並行処理でも自動的に動作します。トレースを手動で開始・終了する場合、`start()` と `finish()` に `mark_as_current` と `reset_current` を渡して現在のトレースを更新する必要があります。

## スパンの作成

スパンを作成するには、各種の [`*_span()`][agents.tracing.create] メソッドを使用します。通常、スパンを手動で作成する必要はありません。カスタムスパン情報を追跡するために [`custom_span()`][agents.tracing.custom_span] 関数も利用可能です。

スパンは自動的に現在のトレースに属し、最も近い現在のスパンの下にネストされます。これは Python の [`contextvar`](https://docs.python.org/3/library/contextvars.html) を介して追跡されます。

## 機密データ

一部のスパンは機密データを含む可能性があります。

`generation_span()` は LLM 生成の入出力を、`function_span()` は関数呼び出しの入出力を保存します。これらには機密データが含まれる可能性があるため、[`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] を使用してデータの取得を無効化できます。

同様に、音声スパンはデフォルトで音声データを base64 エンコードされた PCM データとして含みます。[`VoicePipelineConfig.trace_include_sensitive_audio_data`][agents.voice.pipeline_config.VoicePipelineConfig.trace_include_sensitive_audio_data] を設定して音声データの取得を無効化できます。

## カスタムトレーシングプロセッサ

トレーシングの高レベルアーキテクチャは以下の通りです。

- 初期化時にグローバルな [`TraceProvider`][agents.tracing.setup.TraceProvider] を作成します。
- `TraceProvider` はトレースを OpenAI バックエンドに送信する [`BatchTraceProcessor`][agents.tracing.processors.BatchTraceProcessor] と [`BackendSpanExporter`][agents.tracing.processors.BackendSpanExporter] で構成されます。

このデフォルト設定をカスタマイズするには、以下の 2 つの方法があります。

1. [`add_trace_processor()`][agents.tracing.add_trace_processor] で追加のプロセッサを追加できます。
2. [`set_trace_processors()`][agents.tracing.set_trace_processors] でデフォルトのプロセッサを置き換えることができます。

## 外部トレーシングプロセッサ一覧

（省略：原文のまま）