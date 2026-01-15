---
search:
  exclude: true
---
# ガイド

このガイドでは、 OpenAI Agents SDK の realtime 機能を使って音声対応の AI エージェントを構築する方法を詳しく説明します。

!!! warning "ベータ機能"
Realtime エージェントはベータ版です。実装の改善に伴い、後方互換性のない変更が入る可能性があります。

## 概要

Realtime エージェントは、音声とテキストの入力をリアルタイムに処理し、リアルタイム音声で応答する双方向の会話フローを実現します。 OpenAI の Realtime API との永続接続を維持し、低遅延で自然な音声対話と、割り込みへのスムーズな対応を可能にします。

## アーキテクチャ

### コアコンポーネント

realtime システムは、次の主要コンポーネントで構成されます。

-   **RealtimeAgent**: instructions、ツール、ハンドオフで構成されたエージェントです。
-   **RealtimeRunner**: 設定を管理します。`runner.run()` を呼び出すとセッションを取得できます。
-   **RealtimeSession**: 単一の対話セッションです。通常、 ユーザー が会話を開始するたびに作成し、会話が終了するまで維持します。
-   **RealtimeModel**: 基盤となるモデルのインターフェース（通常は OpenAI の WebSocket 実装）です。

### セッションフロー

典型的な realtime セッションは次のフローに従います。

1. instructions、ツール、ハンドオフを用いて **RealtimeAgent を作成** します。
2. エージェントと設定オプションを用いて **RealtimeRunner をセットアップ** します。
3. `await runner.run()` を使って **セッションを開始** し、 RealtimeSession を受け取ります。
4. `send_audio()` または `send_message()` を使って **音声またはテキストのメッセージを送信** します。
5. セッションを反復処理して **イベントをリッスン** します。イベントには音声出力、文字起こし、ツール呼び出し、ハンドオフ、エラーなどが含まれます。
6. ユーザー がエージェントの発話に被せたときに **割り込みを処理** します。現在の音声生成は自動で停止します。

セッションは会話履歴を保持し、 realtime モデルとの永続接続を管理します。

## エージェントの設定

RealtimeAgent は、通常の Agent クラスと同様に動作しますが、いくつか重要な違いがあります。 API の詳細は [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] のリファレンスをご覧ください。

通常のエージェントとの差分:

-   モデルの選択はエージェント レベルではなくセッション レベルで設定します。
-   structured outputs はサポートされていません（`outputType` は未対応）。
-   音声はエージェントごとに設定できますが、最初のエージェントが発話した後は変更できません。
-   ツール、ハンドオフ、instructions などのその他の機能は同様に動作します。

## セッションの設定

### モデル設定

セッション設定では、基盤となる realtime モデルの動作を制御できます。モデル名（例: `gpt-realtime`）、音声（alloy、echo、fable、onyx、nova、shimmer）の選択、サポートするモダリティ（テキストや音声）を設定できます。音声フォーマットは入力・出力ともに設定可能で、既定は PCM16 です。

### 音声設定

音声設定では、セッションが音声入力と出力をどのように処理するかを制御します。 Whisper のようなモデルを使って入力音声の文字起こしを設定し、言語の優先設定や、ドメイン固有用語の精度を高めるための文字起こし用プロンプトを指定できます。ターン検出設定では、音声活動検出のしきい値、無音時間、検出された発話前後のパディングなど、エージェントが応答を開始・停止すべきタイミングを調整できます。

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

ハンドオフにより、会話を専門のエージェント間で引き継ぐことができます。

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

セッションはイベントを ストリーミング し、セッションオブジェクトを反復処理してリッスンできます。イベントには、音声出力チャンク、文字起こし結果、ツール実行の開始・終了、エージェント間のハンドオフ、エラーなどが含まれます。特に処理すべき主なイベントは以下です。

-   **audio**: エージェントの応答からの Raw 音声データ
-   **audio_end**: エージェントの発話が完了
-   **audio_interrupted**: ユーザー がエージェントを割り込み
-   **tool_start/tool_end**: ツール実行のライフサイクル
-   **handoff**: エージェントのハンドオフが発生
-   **error**: 処理中にエラーが発生

イベントの詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

Realtime エージェントでは出力の ガードレール のみサポートされます。パフォーマンス低下を避けるため、これらのガードレールはデバウンスされ、（毎語ではなく）定期的に実行されます。既定のデバウンス長は 100 文字ですが、設定可能です。

ガードレールは `RealtimeAgent` に直接アタッチするか、セッションの `run_config` を介して提供できます。両方のソースから提供されたガードレールは併用されます。

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

ガードレールがトリガーされると、`guardrail_tripped` イベントが生成され、エージェントの現在の応答を中断することがあります。デバウンス動作により、安全性とリアルタイム性能要件のバランスを取ります。テキストエージェントとは異なり、realtime エージェントはガードレール発火時に例外を発生させることは **ありません**。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使って音声を、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使ってテキストをセッションへ送信します。

音声出力については、`audio` イベントをリッスンし、好みの音声ライブラリで再生してください。ユーザー がエージェントを割り込んだ際にすぐ再生を止め、キュー済みの音声をクリアできるよう、`audio_interrupted` イベントも必ず監視してください。

## SIP 連携

[Realtime Calls API](https://platform.openai.com/docs/guides/realtime-sip) 経由で着信した電話に realtime エージェントを接続できます。 SDK は [`OpenAIRealtimeSIPModel`][agents.realtime.openai_realtime.OpenAIRealtimeSIPModel] を提供しており、 SIP 上でメディアをネゴシエートしつつ、同じエージェントフローを再利用します。

使用するには、モデルインスタンスを runner に渡し、セッション開始時に SIP の `call_id` を指定します。 Call ID は、着信を通知する Webhook から配信されます。

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

発信者が電話を切ると、 SIP セッションは終了し、 realtime 接続は自動的にクローズされます。完全なテレフォニーの例は [`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip) を参照してください。

## モデルへの直接アクセス

基盤となるモデルにアクセスして、カスタムリスナーの追加や高度な操作を実行できます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続を低レベルで制御する必要がある高度なユースケース向けに、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## コード例

完全に動作するサンプルは、 UI コンポーネントあり・なしのデモを含む [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。