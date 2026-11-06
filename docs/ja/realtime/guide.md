---
search:
  exclude: true
---
# リアルタイムエージェントガイド

このガイドでは、OpenAI Agents SDK のリアルタイムレイヤーが OpenAI Realtime API にどのように対応するか、および Python SDK がその上に追加する動作について説明します。

!!! note "はじめに"

    デフォルトの Python パスを使用する場合は、まず [クイックスタート](quickstart.md)をお読みください。アプリでサーバー側 WebSocket と SIP のどちらを使用すべきかを検討している場合は、[リアルタイムトランスポート](transport.md)をお読みください。ブラウザーの WebRTC トランスポートは Python SDK に含まれていません。

## 概要 {#overview}

リアルタイムエージェントは Realtime API への長時間接続を維持するため、モデルはターンごとに新しいリクエストを開始し直すことなく、テキストと音声を段階的に処理し、音声出力をストリーミングし、ツールを呼び出し、中断を処理できます。

SDK の主なコンポーネントは次のとおりです。

-   **RealtimeAgent**: 1 つのリアルタイムスペシャリストに対する指示、ツール、出力ガードレール、ハンドオフ
-   **RealtimeRunner**: 開始エージェントをリアルタイムトランスポートに接続するセッションファクトリー
-   **RealtimeSession**: 入力の送信、イベントの受信、履歴の追跡、ツールの実行を行うライブセッション
-   **RealtimeModel**: トランスポートの抽象化。デフォルトは OpenAI のサーバー側 WebSocket 実装です。

## セッションのライフサイクル {#session-lifecycle}

一般的なリアルタイムセッションは次のようになります。

1. 1 つ以上の `RealtimeAgent` を作成します。
2. 開始エージェントを指定して `RealtimeRunner` を作成します。
3. `await runner.run()` を呼び出して `RealtimeSession` を取得します。
4. `async with session:` または `await session.enter()` を使用してセッションに入ります。
5. `send_message()` または `send_audio()` を使用してユーザー入力を送信します。
6. 会話が終了するまでセッションイベントを反復処理します。

テキストのみの実行とは異なり、`runner.run()` は最終的な実行結果をすぐには生成しません。代わりに、ローカル履歴、バックグラウンドでのツール実行、ガードレールの状態、アクティブなエージェント設定をトランスポートレイヤーと同期し続けるライブセッションオブジェクトを返します。

デフォルトでは、`RealtimeRunner` は `OpenAIRealtimeWebSocketModel` を使用するため、デフォルトの Python パスでは Realtime API へのサーバー側 WebSocket 接続が使用されます。別の `RealtimeModel` を渡した場合も、接続の仕組みは変更できますが、同じセッションライフサイクルとエージェント機能が適用されます。

Realtime API サーバーがデフォルトの WebSocket 接続を正常に閉じると、モデルトランスポートは `disconnected` の [`RealtimeModelConnectionStatusEvent`][agents.realtime.model_events.RealtimeModelConnectionStatusEvent] を発行し、その後に [`RealtimeModelEndOfStreamEvent`][agents.realtime.model_events.RealtimeModelEndOfStreamEvent] を発行します。`RealtimeSession` は両方を `raw_model_event` 内で転送し、すでにキューに入っているイベントを処理し終えてから、例外を発生させずに非同期イテレーションを終了します。呼び出し元が開始した `session.close()` では、これらのサーバー切断イベントは合成されません。予期しない WebSocket 障害は、通常のサーバー切断としてイテレーションを終了するのではなく、引き続きセッションの例外処理経路を通ります。

## エージェントとセッションの設定 {#agent-and-session-configuration}

`RealtimeAgent` は、通常の `Agent` 型よりも意図的に対象範囲が限定されています。

-   モデルの選択は、エージェント単位ではなくセッションレベルで設定します。
-   structured outputs はサポートされていません。
-   音声は設定できますが、セッションが発話音声を生成した後は変更できません。
-   指示、関数ツール、ハンドオフ、フック、出力ガードレールはすべて引き続き機能します。

`RealtimeSessionModelSettings` は、新しいネスト形式の `audio` 設定と、従来のフラットなエイリアスの両方をサポートしています。新しいコードではネスト形式を推奨し、新しいリアルタイムエージェントには `gpt-realtime-2.1` から始めてください。

```python
runner = RealtimeRunner(
    starting_agent=agent,
    config={
        "model_settings": {
            "model_name": "gpt-realtime-2.1",
            "audio": {
                "input": {
                    "format": "pcm16",
                    "transcription": {"model": "gpt-4o-mini-transcribe"},
                    "turn_detection": {"type": "semantic_vad", "interrupt_response": True},
                },
                "output": {"format": "pcm16", "voice": "ash"},
            },
            "tool_choice": "auto",
        }
    },
)
```

有用なセッションレベルの設定には、次のものがあります。

-   `audio.input.format`、`audio.output.format`
-   `audio.input.transcription`
-   `audio.input.noise_reduction`
-   `audio.input.turn_detection`
-   `audio.output.voice`、`audio.output.speed`
-   `output_modalities`
-   `tool_choice`
-   `prompt`
-   `tracing`

`RealtimeRunner(config=...)` で有用な実行レベルの設定には、次のものがあります。

-   `async_tool_calls`
-   `output_guardrails`
-   `guardrails_settings.debounce_text_length`
-   `tool_error_formatter`
-   `tracing_disabled`

型付きインターフェースの全体については、[`RealtimeRunConfig`][agents.realtime.config.RealtimeRunConfig] および [`RealtimeSessionModelSettings`][agents.realtime.config.RealtimeSessionModelSettings] を参照してください。

### 入力文字起こしの設定 {#input-transcription-settings}

入力文字起こしは `audio.input.transcription` で設定します。低レイテンシーの段階的な文字起こしには `gpt-live-transcribe` を使用します。音声ターンがコミットされた後に文字起こしを開始する場合、またはアプリケーションで検出言語の出力が必要な場合は、WebSocket 経由で `gpt-transcribe` を使用します。Agents SDK は、ネストされたセッション設定内でモデル固有の GA 文字起こし設定を転送します。

```python
runner = RealtimeRunner(
    starting_agent=agent,
    config={
        "model_settings": {
            "audio": {
                "input": {
                    "transcription": {
                        "model": "gpt-live-transcribe",
                        "prompt": "A support call about the OpenAI Agents SDK.",
                        "keywords": ["RunState", "MCPServerManager"],
                        "languages": ["en", "ja"],
                    },
                    "turn_detection": None,
                }
            }
        }
    },
)
```

`gpt-live-transcribe` では、`prompt` に自由形式の録音コンテキストを指定し、`keywords` に音声内で出現する可能性があるリテラル語句を列挙し、`languages` に想定される入力言語を列挙します。このモデルでは、単数形の `language` ではなく複数形の `languages` を使用します。両方のフィールドを送信しないでください。

この SDK が固定している OpenAI クライアントのバージョンでは、`delay` は `gpt-realtime-whisper` との組み合わせでのみサポートされます。このモデルのレイテンシーと精度のトレードオフは、次のように設定します。

```python
runner = RealtimeRunner(
    starting_agent=agent,
    config={
        "model_settings": {
            "audio": {
                "input": {
                    "transcription": {
                        "model": "gpt-realtime-whisper",
                        "delay": "low",
                    },
                    "turn_detection": None,
                }
            }
        }
    },
)
```

`delay` 設定には、`minimal`、`low`、`medium`、`high`、または `xhigh` を指定できます。低い値では部分テキストがより早く生成される可能性があり、高い値では文字起こしモデルにより多くの音声コンテキストが提供され、認識精度が向上する可能性があります。各レベルのタイミングが固定されていると想定せず、代表的な音声を使用してベンチマークしてください。

Realtime セッションで WebSocket 経由の `gpt-transcribe` を使用するのは、コミットされた音声ターンの後に文字起こしを開始する場合、またはアプリケーションで検出言語の出力が必要な場合に限ります。このモデルは、以前に文字起こしされたターンをコンテキストとして自動的に使用します。`gpt-transcribe` 完了イベントは、その `languages` 出力フィールドで検出言語を報告します。この出力フィールドは、上記の `gpt-live-transcribe` 想定言語入力とは異なります。

`audio.input.turn_detection` を `None` に設定すると、自動ターン検出が無効になります。その場合、アプリケーションは音声ターンをコミットし、[手動レスポンス制御](#manual-response-control)の説明に従ってレスポンス作成を制御する必要があります。モデルの動作、検証ルール、レイテンシーに関するガイダンスについては、OpenAI API の [Realtime 文字起こしガイド](https://developers.openai.com/api/docs/guides/realtime-transcription)を参照してください。

## 入出力 {#inputs-and-outputs}

### テキストと構造化ユーザーメッセージ {#text-and-structured-user-messages}

プレーンテキストまたは構造化されたリアルタイムメッセージには、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用します。

```python
from agents.realtime import RealtimeUserInputMessage

await session.send_message("Summarize what we discussed so far.")

message: RealtimeUserInputMessage = {
    "type": "message",
    "role": "user",
    "content": [
        {"type": "input_text", "text": "Describe this image."},
        {"type": "input_image", "image_url": image_data_url, "detail": "high"},
    ],
}
await session.send_message(message)
```

構造化メッセージは、リアルタイムの会話に画像入力を含めるための主な方法です。[`examples/realtime/app/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app/server.py) の Web デモ例では、この方法で `input_image` メッセージを転送します。

### 音声入力 {#audio-input}

raw 音声バイトをストリーミングするには、[`session.send_audio()`][agents.realtime.session.RealtimeSession.send_audio] を使用します。

```python
await session.send_audio(audio_bytes)
```

サーバー側のターン検出が無効になっている場合は、ターンの境界を指定する必要があります。高レベルの便利な方法は次のとおりです。

```python
await session.send_audio(audio_bytes, commit=True)
```

より低レベルの制御が必要な場合は、`input_audio_buffer.commit` などの Realtime API クライアントイベントを、基盤となるモデルトランスポート経由で直接送信することもできます。

### 手動レスポンス制御 {#manual-response-control}

`session.send_message()` は、高レベルの経路を使用してユーザー入力を送信し、レスポンスを開始します。一部の設定では、raw 音声のバッファリングによって同じ処理が自動的に行われるとは **限りません**。

Realtime API レベルでの手動ターン制御とは、`turn_detection` を `null` に設定する `session.update` イベントを送信した後、自分で `input_audio_buffer.commit` と `response.create` を送信することを意味します。

ターンを手動で管理している場合は、モデルトランスポート経由で raw クライアントイベントを送信できます。

```python
from agents.realtime.model_inputs import RealtimeModelSendRawMessage

await session.model.send_event(
    RealtimeModelSendRawMessage(
        message={
            "type": "response.create",
        }
    )
)
```

このパターンは、次の場合に役立ちます。

-   `turn_detection` が無効で、モデルが応答するタイミングを決定したい場合
-   レスポンスをトリガーする前にユーザー入力を検査または制限したい場合
-   帯域外レスポンスにカスタムプロンプトが必要な場合

[`examples/realtime/twilio_sip/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip/server.py) の SIP コード例では、開始時の挨拶を強制するために raw の `response.create` を使用しています。

## イベント、履歴、中断 {#events-history-and-interruptions}

`RealtimeSession` は高レベルの SDK イベントを発行し、必要な場合には raw モデルイベントも引き続き転送します。

重要なセッションイベントには、次のものがあります。

-   `audio`、`audio_end`、`audio_interrupted`
-   `agent_start`、`agent_end`
-   `tool_start`、`tool_end`、`tool_approval_required`
-   `handoff`
-   `history_added`、`history_updated`
-   `guardrail_tripped`
-   `input_audio_timeout_triggered`
-   `error`
-   `raw_model_event`

UI の状態に最も有用なイベントは、通常 `history_added` と `history_updated` です。これらは、ユーザーメッセージ、アシスタントメッセージ、ツール呼び出しを含むセッションのローカル履歴を、`RealtimeItem` オブジェクトとして公開します。

### 使用量の集計 {#usage-accounting}

完了したモデルレスポンスに使用量が含まれている場合、SDK の OpenAI `RealtimeModel` トランスポートは、`raw_model_event` 内で [`RealtimeModelUsageEvent`][agents.realtime.model_events.RealtimeModelUsageEvent] を発行します。その `usage` フィールドには、そのレスポンスのトークン数が含まれます。また、`input_tokens_details` と `output_tokens_details` では、モダリティ別の内訳を任意で確認できます。

セッションは、各レスポンスの使用量を共有の [`RunContextWrapper.usage`][agents.run_context.RunContextWrapper.usage] にも追加します。ライブセッションの累積使用量を確認するには、`agent_end` など、その後に発生する高レベルイベントの `event.info.context.usage` から読み取ります。

```python
from agents.realtime import RealtimeModelUsageEvent

async for event in session:
    if event.type == "raw_model_event" and isinstance(event.data, RealtimeModelUsageEvent):
        response_usage = event.data.usage
        print("Response tokens:", response_usage.total_tokens)
        print("Input modalities:", event.data.input_tokens_details)
        print("Output modalities:", event.data.output_tokens_details)
    elif event.type == "agent_end":
        session_usage = event.info.context.usage
        print("Session tokens:", session_usage.total_tokens)
```

使用量は、モデルプロバイダーが完了レスポンスに含めた場合にのみ報告されます。累積値には、その `RealtimeSession` が受信したレスポンスが含まれます。セッションをまたいだ合計ではありません。

### 中断と再生追跡 {#interruptions-and-playback-tracking}

ユーザーがアシスタントを中断すると、セッションは `audio_interrupted` を発行し、ユーザーが実際に聞いた内容とサーバー側の会話が一致するように履歴を更新します。

低レイテンシーのローカル再生では、多くの場合、デフォルトの再生トラッカーで十分です。リモート再生や遅延再生、特に電話通信のシナリオでは、生成されたすべての音声がすでに再生されたと想定するのではなく、実際の再生位置で中断されたレスポンスが切り詰められるように、[`RealtimePlaybackTracker`][agents.realtime.model.RealtimePlaybackTracker] を使用してください。

[`examples/realtime/twilio/twilio_handler.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio/twilio_handler.py) の Twilio コード例で、このパターンを確認できます。

## ツール、承認、ハンドオフ、ガードレール {#tools-approvals-handoffs-and-guardrails}

### 関数ツール {#function-tools}

リアルタイムエージェントは、ライブ会話中の関数ツールをサポートします。

```python
from agents.decorators import tool


@tool
def get_weather(city: str) -> str:
    """Get current weather for a city."""
    return f"The weather in {city} is sunny, 72F."


agent = RealtimeAgent(
    name="Assistant",
    instructions="You can answer weather questions.",
    tools=[get_weather],
)
```

### ツールの承認 {#tool-approvals}

関数ツールでは、実行前に人間による承認を必須にできます。その場合、セッションは `tool_approval_required` を発行し、`approve_tool_call()` または `reject_tool_call()` が呼び出されるまでツールの実行を一時停止します。

ツールに入力ガードレールも設定されている場合、それらのガードレールは承認後、実行の直前に実行されます。承認イベントの発行前にガードレールを実行するには、`RealtimeRunner(..., config={"tool_execution": {"pre_approval_tool_input_guardrails": True}})` を指定してランナーを作成します。この承認前チェックに合格した呼び出しも、承認後かつ実行前に再度チェックされます。

```python
async for event in session:
    if event.type == "tool_approval_required":
        await session.approve_tool_call(event.call_id)
```

具体的なサーバー側の承認ループについては、[`examples/realtime/app/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app/server.py) を参照してください。Human-in-the-loop のドキュメントでも、[Human in the loop](../human_in_the_loop.md) からこのフローを参照しています。

### ハンドオフ {#handoffs}

リアルタイムハンドオフにより、あるエージェントはライブ会話を別のスペシャリストに引き継げます。

```python
from agents.realtime import RealtimeAgent, realtime_handoff

billing_agent = RealtimeAgent(
    name="Billing Support",
    instructions="You specialize in billing issues.",
)

main_agent = RealtimeAgent(
    name="Customer Service",
    instructions="Triage the request and hand off when needed.",
    handoffs=[
        realtime_handoff(
            billing_agent,
            tool_description_override="Transfer to billing support",
        )
    ],
)
```

ハンドオフとして直接使用される `RealtimeAgent` オブジェクトは自動的にラップされます。また、`realtime_handoff(...)` を使用すると、名前、説明、検証、コールバック、可用性をカスタマイズできます。リアルタイムハンドオフは、通常のハンドオフの `input_filter` をサポートして **いません**。

### ガードレール {#guardrails}

リアルタイムエージェントは、エージェントのレスポンスに対する出力ガードレールと、関数ツール呼び出しに対する入力ガードレールをサポートします。出力ガードレールのチェックはデバウンスされます。各チェックは、部分的な差分ごとではなく、蓄積された出力テキストと音声文字起こしの差分に対して実行され、例外を発生させる代わりに `guardrail_tripped` を発行します。1 つの差分につき、スケジュールされるチェックは最大 1 回です。その差分が複数の `debounce_text_length` 境界を越えた場合、SDK は後続の小さな差分の後に追いつくためのチェックをスケジュールするのではなく、次の境界を越えたすべての境界より先に進めます。

```python
from agents.guardrail import GuardrailFunctionOutput, OutputGuardrail


def sensitive_data_check(context, agent, output):
    return GuardrailFunctionOutput(
        tripwire_triggered="password" in output,
        output_info=None,
    )


agent = RealtimeAgent(
    name="Assistant",
    instructions="...",
    output_guardrails=[OutputGuardrail(guardrail_function=sensitive_data_check)],
)
```

リアルタイム出力ガードレールが音声文字起こしで作動すると、セッションはアクティブなレスポンスを中断し、`response.cancel` を強制し、`guardrail_tripped` を発行します。また、トリガーされたガードレールの名前を含むフォローアップのユーザーメッセージを送信し、モデルが代替レスポンスを生成できるようにします。トリップワイヤーが作動した時点ですでに一部の音声がバッファリングされている可能性があるため、音声プレーヤーは引き続き `audio_interrupted` を待ち受け、ローカル再生を直ちに停止する必要があります。組み込みの OpenAI Realtime トランスポートでは、チェック対象のレスポンスが終了した後にガードレールのチェックが完了した場合、セッションはそのレスポンスのバッファリング済み再生のみを中断し、後から開始されたレスポンスはキャンセルしません。テキストのみの出力の場合、セッションは代わりにレスポンススコープの `response.cancel` を送信します。停止すべき音声再生がないため、`audio_interrupted` は発行しません。組み込みの OpenAI Realtime モデルを使用する場合、テキストのみの経路でも同じ `guardrail_tripped` イベントとフォローアップのユーザーメッセージが発行されます。

カスタムの `RealtimeModel` トランスポートでは、同じソーススコープの音声中断動作を提供するために、`RealtimeModelSendInterrupt.response_id` と `playback_only` を尊重する必要があります。また、テキストのみの出力経路で復旧メッセージをサポートするには、`RealtimeModel.send_event_if()` もオーバーライドする必要があります。実装では、トランスポートが実際にイベントをコミットする境界で、指定された条件を再チェックするか、条件チェックとイベントのコミットを一緒に直列化する必要があります。デフォルト実装では、復旧メッセージを安全にスキップします。これは、条件を一度チェックしてからイベントを別途送信すると、そのチェックとイベントのコミットの間に別のレスポンスが開始される可能性があるためです。レスポンスのキャンセルと `guardrail_tripped` イベントは引き続き発生します。

## SIP と電話通信 {#sip-and-telephony}

Python SDK には、[`OpenAIRealtimeSIPModel`][agents.realtime.openai_realtime.OpenAIRealtimeSIPModel] を介した第一級の SIP 接続フローが含まれています。

Realtime Calls API 経由で着信があり、生成された `call_id` にエージェントセッションを接続する場合に使用します。

```python
from agents.realtime import RealtimeRunner
from agents.realtime.openai_realtime import OpenAIRealtimeSIPModel

runner = RealtimeRunner(starting_agent=agent, model=OpenAIRealtimeSIPModel())

async with await runner.run(
    model_config={
        "call_id": call_id_from_webhook,
    }
) as session:
    async for event in session:
        ...
```

先に通話を受け入れ、受け入れペイロードをエージェントから導出されたセッション設定と一致させる必要がある場合は、`OpenAIRealtimeSIPModel.build_initial_session_payload(...)` を使用します。完全なフローは [`examples/realtime/twilio_sip/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip/server.py) で確認できます。

## 低レベルアクセスとカスタムエンドポイント {#low-level-access-and-custom-endpoints}

`session.model` を介して、基盤となるトランスポートオブジェクトにアクセスできます。

次のものが必要な場合に使用します。

-   `session.model.add_listener(...)` を介したカスタムリスナー
-   `response.create` や `session.update` などの raw クライアントイベント
-   `model_config` を介したカスタムの `url`、`headers`、または `api_key` の処理
-   既存のリアルタイム通話への `call_id` 接続

`RealtimeModelConfig` は次をサポートします。

-   `api_key`
-   `url`
-   `headers`
-   `initial_model_settings`
-   `playback_tracker`
-   `call_id`

このリポジトリに含まれる `call_id` コード例は SIP です。より広範な Realtime API では、一部のサーバー側制御フローに `call_id` も使用しますが、ここでは Python コード例としてパッケージ化されていません。

Azure OpenAI に接続する場合は、GA Realtime エンドポイント URL と明示的なヘッダーを渡します。例:

```python
session = await runner.run(
    model_config={
        "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
        "headers": {"api-key": "<your-azure-api-key>"},
    }
)
```

トークンベースの認証では、`headers` にベアラートークンを使用します。

```python
session = await runner.run(
    model_config={
        "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
        "headers": {"authorization": f"Bearer {token}"},
    }
)
```

`headers` を渡した場合、SDK は `Authorization` を自動的に追加しません。リアルタイムエージェントでは、従来のベータパス（`/openai/realtime?api-version=...`）を使用しないでください。

## 関連資料 {#further-reading}

-   [リアルタイムトランスポート](transport.md)
-   [クイックスタート](quickstart.md)
-   [OpenAI Realtime の会話](https://developers.openai.com/api/docs/guides/realtime-conversations/)
-   [OpenAI Realtime のサーバー側制御](https://developers.openai.com/api/docs/guides/realtime-server-controls/)
-   [`examples/realtime`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime)