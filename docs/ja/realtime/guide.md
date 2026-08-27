---
search:
  exclude: true
---
# リアルタイムエージェントガイド

このガイドでは、OpenAI Agents SDK のリアルタイムレイヤーが OpenAI Realtime API にどのように対応しているか、および Python SDK がその上にどのような追加動作を提供するかを説明します。

!!! note "はじめに"

    デフォルトの Python パスを使用する場合は、まず[クイックスタート](quickstart.md)をお読みください。アプリでサーバー側 WebSocket と SIP のどちらを使用すべきか検討している場合は、[リアルタイムトランスポート](transport.md)をお読みください。ブラウザーの WebRTC トランスポートは Python SDK に含まれていません。

## 概要 {#overview}

リアルタイムエージェントは Realtime API への長時間接続を維持するため、モデルは各ターンで新しいリクエストを開始し直すことなく、テキストとオーディオの段階的な処理、オーディオ出力のストリーミング、ツールの呼び出し、中断への対応を行えます。

SDK の主要コンポーネントは次のとおりです。

-   **RealtimeAgent**: 1 つのリアルタイム専門エージェントに対する指示、ツール、出力ガードレール、ハンドオフ
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

デフォルトでは、`RealtimeRunner` は `OpenAIRealtimeWebSocketModel` を使用するため、デフォルトの Python パスは Realtime API へのサーバー側 WebSocket 接続です。別の `RealtimeModel` を渡した場合も、接続の仕組みは変更できますが、同じセッションライフサイクルとエージェント機能が適用されます。

Realtime API サーバーがデフォルトの WebSocket 接続を正常に閉じると、モデルトランスポートは `disconnected` の [`RealtimeModelConnectionStatusEvent`][agents.realtime.model_events.RealtimeModelConnectionStatusEvent] を生成し、続いて [`RealtimeModelEndOfStreamEvent`][agents.realtime.model_events.RealtimeModelEndOfStreamEvent] を生成します。`RealtimeSession` は両方を `raw_model_event` 内で転送し、すでにキューに入っているイベントを処理した後、例外を発生させずに非同期反復を終了します。呼び出し元が開始した `session.close()` では、これらのサーバー切断イベントは合成されません。予期しない WebSocket 障害は、通常のサーバー切断として反復を終了するのではなく、引き続きセッションの例外処理パスを通ります。

## エージェントとセッションの設定 {#agent-and-session-configuration}

`RealtimeAgent` は、通常の `Agent` 型よりも意図的に対象範囲が狭くなっています。

-   モデルの選択はエージェント単位ではなく、セッションレベルで設定します。
-   structured outputs には対応していません。
-   音声は設定できますが、セッションが音声オーディオを生成した後は変更できません。
-   指示、関数ツール、ハンドオフ、フック、出力ガードレールはすべて引き続き機能します。

`RealtimeSessionModelSettings` は、新しいネスト形式の `audio` 設定と、従来のフラット形式のエイリアスの両方に対応しています。新しいコードではネスト形式を優先し、新しいリアルタイムエージェントでは `gpt-realtime-2.1` から始めてください。

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

便利なセッションレベルの設定には、次のものがあります。

-   `audio.input.format`、`audio.output.format`
-   `audio.input.transcription`
-   `audio.input.noise_reduction`
-   `audio.input.turn_detection`
-   `audio.output.voice`、`audio.output.speed`
-   `output_modalities`
-   `tool_choice`
-   `prompt`
-   `tracing`

`RealtimeRunner(config=...)` で利用できる便利な実行レベルの設定には、次のものがあります。

-   `async_tool_calls`
-   `output_guardrails`
-   `guardrails_settings.debounce_text_length`
-   `tool_error_formatter`
-   `tracing_disabled`

型付き API の全体については、[`RealtimeRunConfig`][agents.realtime.config.RealtimeRunConfig] および [`RealtimeSessionModelSettings`][agents.realtime.config.RealtimeSessionModelSettings] を参照してください。

### 入力文字起こし設定 {#input-transcription-settings}

入力の文字起こしは `audio.input.transcription` で設定します。低レイテンシーの段階的な文字起こしには `gpt-live-transcribe` を使用します。オーディオターンのコミット後に文字起こしを開始する必要がある場合、またはアプリケーションで検出言語の出力が必要な場合は、WebSocket 経由で `gpt-transcribe` を使用します。Agents SDK は、モデル固有の GA 文字起こし設定をネストされたセッション設定で転送します。

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

`gpt-live-transcribe` では、`prompt` に自由形式の録音コンテキスト、`keywords` にオーディオ内に含まれる可能性があるリテラル用語、`languages` に想定される入力言語を指定します。このモデルでは、単数形の `language` ではなく複数形の `languages` を使用します。両方のフィールドを送信しないでください。

この SDK で固定されている OpenAI クライアントのバージョンは、`delay` を `gpt-realtime-whisper` と組み合わせた場合にのみ対応しています。そのモデルのレイテンシーと精度のトレードオフは、次のように設定します。

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

`delay` 設定には、`minimal`、`low`、`medium`、`high`、`xhigh` のいずれかを指定できます。値を小さくすると部分的なテキストが早く生成される可能性があり、値を大きくすると文字起こしモデルに与えられるオーディオコンテキストが増え、認識精度が向上する可能性があります。各レベルのタイミングが固定されていると想定せず、代表的なオーディオを使用してベンチマークしてください。

WebSocket 経由の Realtime セッションで `gpt-transcribe` を使用するのは、コミットされたオーディオターンの後に文字起こしを開始する必要がある場合、またはアプリケーションで検出言語の出力が必要な場合に限ります。モデルは、以前に文字起こしされたターンをコンテキストとして自動的に使用します。`gpt-transcribe` 完了イベントは、検出された言語を `languages` 出力フィールドで報告します。この出力フィールドは、上記の想定言語を指定する入力フィールド `gpt-live-transcribe` とは異なります。

`audio.input.turn_detection` を `None` に設定すると、自動ターン検出が無効になります。その場合、アプリケーションは[手動レスポンス制御](#manual-response-control)の説明に従って、オーディオターンをコミットし、レスポンスの作成を制御する必要があります。モデルの動作、検証ルール、レイテンシーのガイダンスについては、OpenAI API の [Realtime 文字起こしガイド](https://developers.openai.com/api/docs/guides/realtime-transcription)を参照してください。

## 入出力 {#inputs-and-outputs}

### テキストと構造化されたユーザーメッセージ {#text-and-structured-user-messages}

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

構造化メッセージは、リアルタイム会話に画像入力を含めるための主要な方法です。[`examples/realtime/app/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app/server.py) のサンプル Web デモでは、`input_image` メッセージをこの方法で転送します。

### オーディオ入力 {#audio-input}

raw オーディオバイトをストリーミングするには、[`session.send_audio()`][agents.realtime.session.RealtimeSession.send_audio] を使用します。

```python
await session.send_audio(audio_bytes)
```

サーバー側のターン検出が無効な場合は、ターンの境界を指定する必要があります。高レベルの便利な方法は次のとおりです。

```python
await session.send_audio(audio_bytes, commit=True)
```

より低レベルの制御が必要な場合は、`input_audio_buffer.commit` などの Realtime API クライアントイベントを、基盤となるモデルトランスポート経由で直接送信することもできます。

### 手動レスポンス制御 {#manual-response-control}

`session.send_message()` は高レベルのパスを使用してユーザー入力を送信し、レスポンスを開始します。一部の設定では、raw オーディオのバッファリングだけでは同じ処理が **自動的には** 行われません。

Realtime API レベルでの手動ターン制御では、`turn_detection` を `null` に設定する `session.update` イベントを送信した後、`input_audio_buffer.commit` と `response.create` を自分で送信します。

ターンを手動で管理する場合は、モデルトランスポート経由で raw クライアントイベントを送信できます。

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

-   `turn_detection` が無効で、モデルが応答するタイミングを決めたい場合
-   レスポンスを開始する前にユーザー入力を検査または制御したい場合
-   帯域外レスポンス用のカスタムプロンプトが必要な場合

[`examples/realtime/twilio_sip/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip/server.py) の SIP コード例では、raw の `response.create` を使用して最初の挨拶を強制的に生成します。

## イベント、履歴、中断 {#events-history-and-interruptions}

`RealtimeSession` は高レベルの SDK イベントを生成すると同時に、必要に応じて raw モデルイベントも転送します。

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

UI の状態管理に最も役立つイベントは、通常 `history_added` と `history_updated` です。これらは、ユーザーメッセージ、アシスタントメッセージ、ツール呼び出しを含むセッションのローカル履歴を `RealtimeItem` オブジェクトとして公開します。

### 使用量の集計 {#usage-accounting}

完了したモデルレスポンスに使用量が含まれる場合、SDK の OpenAI `RealtimeModel` トランスポートは、`raw_model_event` 内で [`RealtimeModelUsageEvent`][agents.realtime.model_events.RealtimeModelUsageEvent] を生成します。その `usage` フィールドにはそのレスポンスのトークン数が含まれ、`input_tokens_details` と `output_tokens_details` には任意のモダリティ別内訳が含まれます。

また、セッションは各レスポンスの使用量を共有の [`RunContextWrapper.usage`][agents.run_context.RunContextWrapper.usage] に加算します。ライブセッションの累積使用量を確認するには、`agent_end` など、その後に発生する高レベルイベントの `event.info.context.usage` から読み取ります。

```python
from agents.realtime import RealtimeModelUsageEvent

async for event in session:
    if event.type == "raw_model_event" and isinstance(
        event.data, RealtimeModelUsageEvent
    ):
        response_usage = event.data.usage
        print("Response tokens:", response_usage.total_tokens)
        print("Input modalities:", event.data.input_tokens_details)
        print("Output modalities:", event.data.output_tokens_details)
    elif event.type == "agent_end":
        session_usage = event.info.context.usage
        print("Session tokens:", session_usage.total_tokens)
```

使用量は、モデルプロバイダーが完了したレスポンスに使用量を含めた場合にのみ報告されます。累積値は、その `RealtimeSession` が受信したレスポンスを対象とし、複数のセッションをまたぐ合計値ではありません。

### 中断と再生トラッキング {#interruptions-and-playback-tracking}

ユーザーがアシスタントを中断すると、セッションは `audio_interrupted` を生成し、ユーザーが実際に聞いた内容とサーバー側の会話が一致するように履歴を更新します。

低レイテンシーのローカル再生では、多くの場合、デフォルトの再生トラッカーで十分です。リモート再生や遅延再生、特にテレフォニーでは、生成されたすべてのオーディオがすでに再生されたと想定するのではなく、実際の再生位置で中断されたレスポンスを切り詰めるために、[`RealtimePlaybackTracker`][agents.realtime.model.RealtimePlaybackTracker] を使用します。

[`examples/realtime/twilio/twilio_handler.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio/twilio_handler.py) の Twilio コード例で、このパターンを確認できます。

## ツール、承認、ハンドオフ、ガードレール {#tools-approvals-handoffs-and-guardrails}

### 関数ツール {#function-tools}

リアルタイムエージェントは、ライブ会話中の関数ツールに対応しています。

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

### ツール承認 {#tool-approvals}

関数ツールでは、実行前に人間による承認を必須にできます。その場合、セッションは `tool_approval_required` を生成し、`approve_tool_call()` または `reject_tool_call()` を呼び出すまでツールの実行を一時停止します。

ツールに入力ガードレールも設定されている場合、承認後、実行の直前にそのガードレールが実行されます。承認イベントが生成される前に入力ガードレールを実行するには、`RealtimeRunner(..., config={"tool_execution": {"pre_approval_tool_input_guardrails": True}})` を指定してランナーを作成します。この承認前チェックを通過した呼び出しも、実行前に承認後のチェックが再度行われます。

```python
async for event in session:
    if event.type == "tool_approval_required":
        await session.approve_tool_call(event.call_id)
```

具体的なサーバー側の承認ループについては、[`examples/realtime/app/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app/server.py) を参照してください。Human-in-the-loop のドキュメントでも、[Human in the loop](../human_in_the_loop.md)でこのフローを参照しています。

### ハンドオフ {#handoffs}

リアルタイムハンドオフを使用すると、あるエージェントから別の専門エージェントへライブ会話を転送できます。

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

ハンドオフとして直接使用される `RealtimeAgent` オブジェクトは自動的にラップされます。また、`realtime_handoff(...)` を使用すると、名前、説明、検証、コールバック、可用性をカスタマイズできます。リアルタイムハンドオフは、通常のハンドオフの `input_filter` には対応していません。

### ガードレール {#guardrails}

リアルタイムエージェントは、エージェントのレスポンスに対する出力ガードレールと、関数ツール呼び出しに対する入力ガードレールに対応しています。出力ガードレールのチェックにはデバウンスが適用されます。各チェックは、部分的な差分ごとではなく、蓄積された出力テキストとオーディオ文字起こしの差分に対して実行され、例外を発生させる代わりに `guardrail_tripped` を生成します。

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

リアルタイム出力ガードレールがオーディオ文字起こしで作動すると、セッションはアクティブなレスポンスを中断し、`response.cancel` を強制的に実行し、`guardrail_tripped` を生成します。さらに、作動したガードレールの名前を示すフォローアップのユーザーメッセージを送信し、モデルが代替レスポンスを生成できるようにします。トリップワイヤーが作動した時点ですでに一部のオーディオがバッファリングされている可能性があるため、オーディオプレーヤーでは引き続き `audio_interrupted` を監視し、ローカル再生を直ちに停止する必要があります。組み込みの OpenAI Realtime トランスポートでは、チェック対象のレスポンスが終了した後にガードレールのチェックが完了した場合、セッションはそのレスポンスのバッファリング済み再生だけを中断し、後から開始されたレスポンスはキャンセルしません。テキストのみの出力では、代わりにレスポンススコープの `response.cancel` を送信します。停止すべきオーディオ再生がないため、`audio_interrupted` は生成しません。組み込みの OpenAI Realtime モデルを使用する場合、テキストのみのパスでも同じ `guardrail_tripped` イベントとフォローアップのユーザーメッセージが生成されます。

カスタムの `RealtimeModel` トランスポートでは、同じ発生元スコープのオーディオ中断動作を提供するために、`RealtimeModelSendInterrupt.response_id` と `playback_only` を遵守する必要があります。また、テキストのみの出力パスで復旧メッセージに対応するには、`RealtimeModel.send_event_if()` をオーバーライドする必要があります。実装では、トランスポートが実際にイベントをコミットする境界で指定された条件を再確認するか、条件チェックとイベントのコミットを直列化する必要があります。デフォルト実装は復旧メッセージを安全にスキップします。条件を一度確認してからイベントを別途送信すると、その確認とイベントのコミットの間に別のレスポンスが開始される可能性があるためです。レスポンスのキャンセルと `guardrail_tripped` イベントは引き続き発生します。

## SIP とテレフォニー {#sip-and-telephony}

Python SDK には、[`OpenAIRealtimeSIPModel`][agents.realtime.openai_realtime.OpenAIRealtimeSIPModel] を介した第一級の SIP 接続フローが含まれています。

Realtime Calls API 経由で着信し、生成された `call_id` にエージェントセッションを接続する場合に使用します。

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

先に通話を受け入れる必要があり、その受け入れペイロードをエージェントから導出されたセッション設定と一致させたい場合は、`OpenAIRealtimeSIPModel.build_initial_session_payload(...)` を使用します。完全なフローは [`examples/realtime/twilio_sip/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip/server.py) で確認できます。

## 低レベルアクセスとカスタムエンドポイント {#low-level-access-and-custom-endpoints}

`session.model` を介して、基盤となるトランスポートオブジェクトにアクセスできます。

これは、次のものが必要な場合に使用します。

-   `session.model.add_listener(...)` を介したカスタムリスナー
-   `response.create` や `session.update` などの raw クライアントイベント
-   `model_config` を介したカスタムの `url`、`headers`、`api_key` 処理
-   既存のリアルタイム通話への `call_id` 接続

`RealtimeModelConfig` は、次のものに対応しています。

-   `api_key`
-   `url`
-   `headers`
-   `initial_model_settings`
-   `playback_tracker`
-   `call_id`

このリポジトリに同梱されている `call_id` コード例は SIP です。より広範な Realtime API では、一部のサーバー側制御フローに `call_id` も使用しますが、ここでは Python コード例としてパッケージ化されていません。

Azure OpenAI に接続する場合は、GA Realtime エンドポイント URL と明示的なヘッダーを渡します。次に例を示します。

```python
session = await runner.run(
    model_config={
        "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
        "headers": {"api-key": "<your-azure-api-key>"},
    }
)
```

トークンベースの認証では、`headers` に Bearer トークンを使用します。

```python
session = await runner.run(
    model_config={
        "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
        "headers": {"authorization": f"Bearer {token}"},
    }
)
```

`headers` を渡した場合、SDK は `Authorization` を自動的には追加しません。リアルタイムエージェントでは、従来のベータパス（`/openai/realtime?api-version=...`）を使用しないでください。

## 関連資料 {#further-reading}

-   [リアルタイムトランスポート](transport.md)
-   [クイックスタート](quickstart.md)
-   [OpenAI Realtime の会話](https://developers.openai.com/api/docs/guides/realtime-conversations/)
-   [OpenAI Realtime のサーバー側制御](https://developers.openai.com/api/docs/guides/realtime-server-controls/)
-   [`examples/realtime`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime)