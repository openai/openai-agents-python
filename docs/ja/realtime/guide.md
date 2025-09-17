---
search:
  exclude: true
---
# ガイド

このガイドでは、OpenAI Agents SDK の realtime 機能を使って音声対応の AI エージェントを構築する方法を詳しく説明します。

!!! warning "Beta feature"
Realtime エージェントはベータ版です。実装の改善に伴い、非互換の変更が入る可能性があります。

## 概要

Realtime エージェントは、音声とテキストの入力をリアルタイムに処理し、リアルタイム音声で応答する会話フローを可能にします。OpenAI の Realtime API との永続的な接続を維持し、低遅延で自然な音声対話と、割り込みへの優雅な対応を実現します。

## アーキテクチャ

### コアコンポーネント

realtime システムは、次の主要コンポーネントで構成されます。

-  **RealtimeAgent**: instructions、tools、handoffs で構成されたエージェントです。
-  **RealtimeRunner**: 設定を管理します。`runner.run()` を呼び出してセッションを取得できます。
-  **RealtimeSession**: 単一の対話セッションです。通常、ユーザーが会話を開始するたびに 1 つ作成し、会話が終了するまで存続させます。
-  **RealtimeModel**: 基盤となるモデルインターフェイスです（通常は OpenAI の WebSocket 実装）。

### セッションフロー

一般的な realtime セッションは次の流れに従います。

1. **RealtimeAgent を作成** し、instructions、tools、handoffs を設定します。
2. **RealtimeRunner をセットアップ** し、エージェントと設定オプションを指定します。
3. **セッションを開始** します。`await runner.run()` を使用すると RealtimeSession が返ります。
4. **音声またはテキストメッセージを送信** します。`send_audio()` または `send_message()` を使用します。
5. **イベントをリッスン** します。セッションを反復処理して、音声出力、文字起こし、ツール呼び出し、ハンドオフ、エラーなどのイベントを受け取ります。
6. **割り込みに対応** します。ユーザーがエージェントの発話に重ねて話した場合、現在の音声生成は自動的に停止します。

セッションは会話履歴を保持し、realtime モデルとの永続的な接続を管理します。

## エージェントの設定

RealtimeAgent は通常の Agent クラスと同様に動作しますが、いくつか重要な違いがあります。API の詳細は、[`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] の API リファレンスをご覧ください。

通常のエージェントとの主な違い:

-  モデルの選択はエージェントではなくセッション単位で設定します。
-  structured output はサポートされません（`outputType` は非対応）。
-  音声はエージェント単位で設定できますが、最初のエージェントが話し始めた後は変更できません。
-  その他の機能（tools、handoffs、instructions）は同様に動作します。

## セッションの設定

### モデル設定

セッション設定では、基盤となる realtime モデルの動作を制御できます。モデル名（`gpt-realtime` など）、音声選択（ alloy、echo、fable、onyx、nova、shimmer ）、対応モダリティ（テキストおよび/または音声）を設定できます。音声フォーマットは入力・出力の両方で設定可能で、既定は PCM16 です。

### 音声設定

音声設定では、セッションの音声入力・出力の扱いを制御します。Whisper などのモデルを使った入力音声の文字起こし、言語設定、特定領域の用語に対する精度向上のための文字起こしプロンプトを設定できます。ターン検出の設定では、音声活動検出のしきい値、無音時間、検出した発話の前後のパディングなどにより、エージェントがいつ応答を開始・終了するかを制御します。

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

ハンドオフにより、専門特化したエージェント間で会話を引き継げます。

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

セッションはイベントをストリーミングし、セッションオブジェクトを反復処理してリッスンできます。イベントには、音声出力チャンク、文字起こし結果、ツール実行の開始・終了、エージェントのハンドオフ、エラーが含まれます。主に対応すべきイベントは以下です。

-  **audio**: エージェントの応答からの raw な音声データ
-  **audio_end**: エージェントの発話が終了しました
-  **audio_interrupted**: ユーザーがエージェントを割り込みました
-  **tool_start/tool_end**: ツール実行のライフサイクル
-  **handoff**: エージェントのハンドオフが発生しました
-  **error**: 処理中にエラーが発生しました

イベントの詳細は、[`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

realtime エージェントでサポートされるのは出力ガードレールのみです。パフォーマンス低下を避けるため、これらのガードレールはデバウンスされ、リアルタイム生成中に（毎語ではなく）定期的に実行されます。既定のデバウンス長は 100 文字ですが、変更可能です。

ガードレールは `RealtimeAgent` に直接アタッチするか、セッションの `run_config` を通じて提供できます。両方の経路からのガードレールは併用されます。

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

ガードレールが作動すると、`guardrail_tripped` イベントが発行され、エージェントの現在の応答を中断できます。デバウンス動作により、安全性とリアルタイム性能要件のバランスを取ります。テキスト エージェントと異なり、realtime エージェントはガードレールが作動しても例外は発生させません。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使って音声をセッションへ送信するか、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使ってテキストを送信します。

音声出力については、`audio` イベントをリッスンして、お好みの音声ライブラリで再生してください。ユーザーがエージェントを割り込んだ際に直ちに再生を停止し、キューにある音声をクリアできるよう、`audio_interrupted` イベントも必ず監視してください。

## モデルへの直接アクセス

基盤となるモデルにアクセスして、カスタムリスナーの追加や高度な操作を行えます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続をより低レベルに制御する高度なユースケースに向けて、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェイスへ直接アクセスできます。

## コード例

動作する完全なコード例は、[examples/realtime directory](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。UI コンポーネントあり/なしのデモが含まれます。