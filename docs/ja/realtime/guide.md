---
search:
  exclude: true
---
# ガイド

このガイドでは、OpenAI Agents SDK の realtime 機能を用いて音声対応の AI エージェントを構築する方法を詳しく紹介します。

!!! warning "ベータ機能"
Realtime エージェントはベータ版です。実装の改善に伴い、重大な変更が入る可能性があります。

## 概要

Realtime エージェントは、会話フローを可能にし、音声とテキストの入力をリアルタイムで処理し、リアルタイム音声で応答します。OpenAI の Realtime API と永続的な接続を維持し、低遅延で自然な音声対話と、割り込みへの優雅な対応が可能です。

## アーキテクチャ

### 中核コンポーネント

realtime システムは、次の主要なコンポーネントで構成されます。

-   **RealtimeAgent**: instructions、tools、handoffs を設定したエージェント。
-   **RealtimeRunner**: 設定を管理します。`runner.run()` を呼び出してセッションを取得できます。
-   **RealtimeSession**: 単一の対話セッション。通常、 ユーザー が会話を開始するたびに作成し、会話が終了するまで維持します。
-   **RealtimeModel**: 基盤となるモデルインターフェース（一般的には OpenAI の WebSocket 実装）

### セッションフロー

典型的な realtime セッションは次のフローに従います。

1. instructions、tools、handoffs を指定して **RealtimeAgent を作成** します。
2. エージェントと設定オプションで **RealtimeRunner をセットアップ** します。
3. `await runner.run()` を使って **セッションを開始** します。これは RealtimeSession を返します。
4. `send_audio()` または `send_message()` を使って **音声またはテキストのメッセージを送信** します。
5. セッションを反復処理して **イベントをリッスン** します。イベントには音声出力、書き起こし、ツール呼び出し、ハンドオフ、エラーが含まれます。
6. ユーザー がエージェントの発話にかぶせたときに **割り込みを処理** します。これにより現在の音声生成は自動的に停止します。

セッションは会話履歴を保持し、realtime モデルとの永続的な接続を管理します。

## エージェント設定

RealtimeAgent は通常の Agent クラスと同様に動作しますが、いくつか重要な違いがあります。API の詳細は [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] の API リファレンスをご覧ください。

通常のエージェントとの主な違い:

-   モデル選択はエージェントレベルではなくセッションレベルで設定します。
-   structured outputs のサポートはありません（`outputType` はサポートされません）。
-   音声はエージェントごとに設定できますが、最初のエージェントが話し始めた後は変更できません。
-   それ以外の機能（tools、handoffs、instructions）は同様に動作します。

## セッション設定

### モデル設定

セッション設定では、基盤となる realtime モデルの挙動を制御できます。モデル名（例: `gpt-realtime`）、ボイス選択（alloy、echo、fable、onyx、nova、shimmer）、対応モダリティ（テキストおよび／または音声）を設定できます。音声フォーマットは入力・出力の両方で設定可能で、デフォルトは PCM16 です。

### 音声設定

音声設定では、セッションが音声の入出力をどのように処理するかを制御します。Whisper のようなモデルを使用した入力音声の書き起こし、言語設定、ドメイン固有用語の精度を高めるための書き起こしプロンプトを設定できます。発話区間検出（Turn detection）設定では、エージェントがいつ応答を開始・終了すべきかを制御し、音声活動検出のしきい値、無音時間、検出した発話の前後パディングなどを指定できます。

## ツールと関数

### ツールの追加

通常のエージェントと同様に、realtime エージェントは会話中に実行される 関数ツール をサポートします。

```python
from agents import function_tool

@function_tool
def get_weather(city: str) -> str:
    """Get current weather for a city."""
    # Your weather API logic here
    return f"The weather in {city} is sunny, 72°F"

@function_tool
def book_appointment(date: str, time: str, service: str) -> str:
    """Book an appointment."""
    # Your booking logic here
    return f"Appointment booked for {service} on {date} at {time}"

agent = RealtimeAgent(
    name="Assistant",
    instructions="You can help with weather and appointments.",
    tools=[get_weather, book_appointment],
)
```

## ハンドオフ

### ハンドオフの作成

ハンドオフにより、専門化されたエージェント間で会話を移譲できます。

```python
from agents.realtime import realtime_handoff

# Specialized agents
billing_agent = RealtimeAgent(
    name="Billing Support",
    instructions="You specialize in billing and payment issues.",
)

technical_agent = RealtimeAgent(
    name="Technical Support",
    instructions="You handle technical troubleshooting.",
)

# Main agent with handoffs
main_agent = RealtimeAgent(
    name="Customer Service",
    instructions="You are the main customer service agent. Hand off to specialists when needed.",
    handoffs=[
        realtime_handoff(billing_agent, tool_description="Transfer to billing support"),
        realtime_handoff(technical_agent, tool_description="Transfer to technical support"),
    ]
)
```

## イベント処理

セッションはイベントをストリーミングし、セッションオブジェクトを反復処理してリッスンできます。イベントには、音声出力チャンク、書き起こし結果、ツール実行の開始と終了、エージェントのハンドオフ、エラーが含まれます。特に処理すべき主要イベントは次のとおりです。

-   **audio**: エージェントの応答からの raw 音声データ
-   **audio_end**: エージェントの発話完了
-   **audio_interrupted**: ユーザー がエージェントを割り込んだ
-   **tool_start/tool_end**: ツール実行のライフサイクル
-   **handoff**: エージェントのハンドオフが発生
-   **error**: 処理中にエラーが発生

イベントの詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] をご覧ください。

## ガードレール

Realtime エージェントでは出力ガードレールのみがサポートされています。これらのガードレールはデバウンスされ、リアルタイム生成中のパフォーマンス問題を避けるため、（すべての単語ごとではなく）一定間隔で実行されます。デフォルトのデバウンス長は 100 文字ですが、設定可能です。

ガードレールは `RealtimeAgent` に直接アタッチするか、セッションの `run_config` を介して提供できます。両方のソースのガードレールは併せて実行されます。

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

ガードレールがトリガーされると、`guardrail_tripped` イベントが生成され、エージェントの現在の応答を中断する場合があります。デバウンス動作により、安全性とリアルタイム性能要件のバランスを取ります。テキストエージェントと異なり、realtime エージェントはガードレール発動時に例外をスローしません。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] で音声を、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] でテキストをセッションに送信します。

音声出力を再生するには、`audio` イベントをリッスンし、任意の音声ライブラリでデータを再生してください。ユーザー がエージェントを割り込んだ際に即座に再生を停止し、キュー済み音声をクリアできるよう、`audio_interrupted` イベントのリッスンも忘れないでください。

## SIP 連携

[Realtime Calls API](https://platform.openai.com/docs/guides/realtime-sip) を介して着信する電話に realtime エージェントを接続できます。SDK には [`OpenAIRealtimeSIPModel`][agents.realtime.openai_realtime.OpenAIRealtimeSIPModel] が用意されており、SIP 上でメディアをネゴシエートしつつ、同じエージェントフローを再利用します。

使用するには、ランナーにモデルインスタンスを渡し、セッション開始時に SIP の `call_id` を指定します。コール ID は、着信を通知する Webhook によって送信されます。

```python
from agents.realtime import RealtimeAgent, RealtimeRunner
from agents.realtime.openai_realtime import OpenAIRealtimeSIPModel

runner = RealtimeRunner(
    starting_agent=agent,
    model=OpenAIRealtimeSIPModel(),
)

async with await runner.run(
    model_config={
        "call_id": call_id_from_webhook,
        "initial_model_settings": {
            "turn_detection": {"type": "semantic_vad", "interrupt_response": True},
        },
    },
) as session:
    async for event in session:
        ...
```

発信者が電話を切ると、SIP セッションは終了し、realtime 接続は自動的に閉じられます。完全なテレフォニーのサンプルは [`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip) を参照してください。

## モデルへの直接アクセス

基盤となるモデルへアクセスして、カスタムリスナーの追加や高度な操作を実行できます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、高度なユースケースで接続をより低レベルに制御するための [`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## コード例

完全な動作サンプルについては、UI コンポーネントの有無それぞれのデモを含む [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。