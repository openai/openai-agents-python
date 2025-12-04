---
search:
  exclude: true
---
# ガイド

このガイドでは、 OpenAI Agents SDK の リアルタイム 機能を使って音声対応の AI エージェントを構築する方法を詳しく説明します。

!!! warning "ベータ機能"
Realtime エージェントはベータ版です。実装の改善に伴い、破壊的変更が発生する可能性があります。

## 概要

Realtime エージェントは、会話フローを可能にし、音声とテキストの入力を リアルタイム で処理し、 リアルタイム 音声で応答します。 OpenAI の Realtime API との永続的な接続を維持し、低遅延で自然な音声会話と割り込みへの柔軟な対応を実現します。

## アーキテクチャ

### コアコンポーネント

リアルタイム システムはいくつかの主要なコンポーネントで構成されます。

-   **RealtimeAgent**: instructions、tools、handoffs で構成されたエージェント。
-   **RealtimeRunner**: 設定を管理します。`runner.run()` を呼び出してセッションを取得できます。
-   **RealtimeSession**: 単一の対話セッション。通常は ユーザー が会話を開始するたびに作成し、会話が終わるまで維持します。
-   **RealtimeModel**: 基盤となるモデル インターフェース（通常は OpenAI の WebSocket 実装）

### セッションフロー

一般的な リアルタイム セッションは次のフローに従います。

1. instructions、tools、handoffs を指定して **RealtimeAgent を作成** します。
2. エージェントと設定オプションで **RealtimeRunner をセットアップ** します。
3. `await runner.run()` を使って **セッションを開始** し、RealtimeSession を受け取ります。
4. `send_audio()` または `send_message()` を使って **音声またはテキストメッセージを送信** します。
5. セッションを反復処理して **イベントをリッスン** します。イベントには音声出力、文字起こし、ツール呼び出し、ハンドオフ、エラーが含まれます。
6. ユーザー がエージェントの発話に被せて話したときの **割り込み処理** を行います。これにより現在の音声生成は自動的に停止します。

セッションは会話履歴を保持し、 リアルタイム モデルとの永続接続を管理します。

## エージェント設定

RealtimeAgent は通常の Agent クラスと同様に動作しますが、いくつか重要な違いがあります。 API の詳細は [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] の参照をご覧ください。

通常のエージェントとの主な違い:

-   モデルの選択はエージェント レベルではなくセッション レベルで設定します。
-   structured outputs はサポートされません（`outputType` はサポートされません）。
-   音声はエージェントごとに設定できますが、最初のエージェントが話し始めた後は変更できません。
-   tools、handoffs、instructions などの他の機能は同様に動作します。

## セッション設定

### モデル設定

セッション設定では、基盤となる リアルタイム モデルの動作を制御できます。モデル名（`gpt-realtime` など）、ボイス選択（alloy、echo、fable、onyx、nova、shimmer）、対応モダリティ（テキストおよび/または音声）を設定できます。音声フォーマットは入力と出力の両方に設定でき、デフォルトは PCM16 です。

### 音声設定

音声設定では、セッションが音声入力と出力をどのように処理するかを制御します。Whisper のようなモデルを使った入力音声の文字起こし、言語設定、専門用語の精度を高める文字起こしプロンプトを指定できます。ターン検出設定では、音声活動検出のしきい値、無音の継続時間、検出された発話の前後のパディングなど、エージェントが応答を開始・停止するタイミングを制御できます。

## ツールと関数

### ツールの追加

通常のエージェントと同様に、 リアルタイム エージェントは会話中に実行される 関数ツール をサポートします。

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

ハンドオフにより、専門のエージェント間で会話を転送できます。

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

セッションはイベントを ストリーミング し、セッションオブジェクトを反復処理することでリッスンできます。イベントには、音声出力チャンク、文字起こし結果、ツール実行の開始と終了、エージェントのハンドオフ、エラーが含まれます。主に扱うべきイベントは次のとおりです。

-   **audio**: エージェントの応答からの生の音声データ
-   **audio_end**: エージェントの発話が終了
-   **audio_interrupted**: ユーザー によるエージェントの割り込み
-   **tool_start/tool_end**: ツール実行のライフサイクル
-   **handoff**: エージェントのハンドオフが発生
-   **error**: 処理中にエラーが発生

イベントの詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

Realtime エージェントでサポートされるのは出力ガードレールのみです。パフォーマンス問題を避けるため、これらのガードレールはデバウンスされ、（毎語ではなく）定期的に実行されます。デフォルトのデバウンス長は 100 文字ですが、設定可能です。

ガードレールは `RealtimeAgent` に直接アタッチするか、セッションの `run_config` から提供できます。両方のソースのガードレールが一緒に実行されます。

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

ガードレールがトリガーされると、`guardrail_tripped` イベントが生成され、エージェントの現在の応答を中断できます。デバウンス動作は、安全性と リアルタイム パフォーマンス要件のバランスを取るのに役立ちます。テキストエージェントと異なり、Realtime エージェントはガードレールが作動しても Exception を送出しません。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使用して音声をセッションに送信するか、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用してテキストを送信します。

音声出力については、`audio` イベントをリッスンし、好みの音声ライブラリで音声データを再生します。ユーザー がエージェントを割り込んだ場合に即座に再生を停止し、キュー済みの音声をクリアできるよう、`audio_interrupted` イベントも必ずリッスンしてください。

## SIP 連携

[Realtime Calls API](https://platform.openai.com/docs/guides/realtime-sip) 経由で着信する電話に リアルタイム エージェントを接続できます。SDK は [`OpenAIRealtimeSIPModel`][agents.realtime.openai_realtime.OpenAIRealtimeSIPModel] を提供しており、SIP 上でメディアをネゴシエートしながら同じエージェントフローを再利用します。

使用するには、モデルインスタンスを runner に渡し、セッション開始時に SIP の `call_id` を指定します。コール ID は、着信を通知する Webhook により送信されます。

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

発信者が電話を切ると、SIP セッションは終了し、 リアルタイム 接続は自動的にクローズされます。完全なテレフォニーの code examples は [`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip) を参照してください。

## 直接モデルアクセス

基盤となるモデルにアクセスして、カスタムリスナーの追加や高度な操作を実行できます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続を低レベルで制御する必要がある高度なユースケース向けに、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## 例

完全な動作サンプルは、UI コンポーネントの有無によるデモを含む [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) をご覧ください。