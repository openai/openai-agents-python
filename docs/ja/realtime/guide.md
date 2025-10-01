---
search:
  exclude: true
---
# ガイド

このガイドでは、 OpenAI Agents SDK のリアルタイム機能を用いて音声対応の AI エージェントを構築する方法を詳しく説明します。

!!! warning "ベータ機能"
リアルタイムエージェントはベータ版です。実装の改善に伴い、破壊的変更が発生する可能性があります。

## 概要

リアルタイムエージェントは、会話の流れを実現し、音声およびテキスト入力をリアルタイムで処理し、リアルタイム音声で応答します。 OpenAI の Realtime API との持続的な接続を維持し、低遅延で自然な音声対話と、割り込みへの優雅な対応を可能にします。

## アーキテクチャ

### 中核コンポーネント

リアルタイムシステムは次の主要コンポーネントから成ります。

-   **RealtimeAgent**: instructions、tools、ハンドオフを設定したエージェント。
-   **RealtimeRunner**: 設定を管理します。`runner.run()` を呼び出してセッションを取得できます。
-   **RealtimeSession**: 単一の対話セッション。通常、 ユーザー が会話を開始するたびに 1 つ作成し、会話が終了するまで維持します。
-   **RealtimeModel**: 基盤となるモデルインターフェース（一般的には OpenAI の WebSocket 実装）

### セッションフロー

一般的なリアルタイムセッションは以下の流れに従います。

1. instructions、tools、ハンドオフを指定して **RealtimeAgent を作成** します。
2. エージェントと設定オプションで **RealtimeRunner をセットアップ** します。
3. `await runner.run()` を使用して **セッションを開始** し、 RealtimeSession が返されます。
4. `send_audio()` または `send_message()` を使って **音声またはテキストメッセージを送信** します。
5. セッションを反復処理して **イベントをリッスン** します。イベントには音声出力、文字起こし、ツール呼び出し、ハンドオフ、エラーなどが含まれます。
6. ユーザー がエージェントの発話に被せて話す際の **割り込みを処理** します。現在の音声生成は自動的に停止します。

セッションは会話履歴を保持し、リアルタイムモデルとの永続接続を管理します。

## エージェント構成

RealtimeAgent は通常の Agent クラスと同様に動作しますが、いくつか重要な違いがあります。完全な API の詳細は、[`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] の API リファレンスを参照してください。

通常のエージェントとの主な違い:

-   モデル選択はエージェントレベルではなくセッションレベルで設定します。
-   structured outputs のサポートはありません（`outputType` はサポートされません）。
-   声質（ボイス）はエージェントごとに設定できますが、最初のエージェントが話し始めた後は変更できません。
-   ツール、ハンドオフ、instructions などその他の機能は同様に動作します。

## セッション構成

### モデル設定

セッション構成では、基盤となるリアルタイムモデルの動作を制御できます。モデル名（`gpt-realtime` など）、ボイス選択（alloy、echo、fable、onyx、nova、shimmer）、および対応モダリティ（テキストおよび/または音声）を設定できます。音声フォーマットは入力と出力の両方に対して設定でき、既定は PCM16 です。

### 音声設定

音声設定は、セッションが音声入力と出力をどのように扱うかを制御します。Whisper などのモデルを使った入力音声の文字起こし、言語設定、特定領域の用語精度を高めるための文字起こしプロンプトを設定できます。ターン検出設定では、エージェントがいつ応答を開始・停止すべきかを制御し、音声活動検出のしきい値、無音時間、検出された発話の前後パディングなどのオプションがあります。

## ツールと関数

### ツールの追加

通常のエージェントと同様に、リアルタイムエージェントは会話中に実行される 関数ツール をサポートします。

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

セッションはイベントをストリーミングし、セッションオブジェクトを反復処理してリッスンできます。イベントには音声出力チャンク、文字起こし結果、ツール実行の開始と終了、エージェントのハンドオフ、エラーなどが含まれます。特に処理すべき主要イベントは次のとおりです。

-   **audio**: エージェントの応答からの Raw 音声データ
-   **audio_end**: エージェントの発話が終了
-   **audio_interrupted**: ユーザー がエージェントを割り込み
-   **tool_start/tool_end**: ツール実行のライフサイクル
-   **handoff**: エージェントのハンドオフが発生
-   **error**: 処理中にエラーが発生

完全なイベントの詳細は、[`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

リアルタイムエージェントでは出力ガードレールのみサポートされます。パフォーマンス問題を避けるため、これらのガードレールはデバウンスされ、リアルタイム生成中に（毎語ではなく）定期的に実行されます。既定のデバウンス長は 100 文字ですが、設定可能です。

ガードレールは `RealtimeAgent` に直接アタッチするか、セッションの `run_config` を通じて提供できます。両方のソースのガードレールは併用されます。

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

ガードレールがトリガーされると、`guardrail_tripped` イベントが生成され、エージェントの現在の応答を割り込む場合があります。デバウンス動作により、安全性とリアルタイム性能要件のバランスを取ります。テキストエージェントと異なり、リアルタイムエージェントではガードレールが作動しても Exception は発生しません。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使って音声をセッションに送信するか、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使ってテキストを送信します。

音声出力については、`audio` イベントをリッスンし、任意の音声ライブラリで再生してください。ユーザー がエージェントを割り込んだ際には、`audio_interrupted` イベントを必ずリッスンして、再生を即座に停止し、キュー済みの音声をクリアしてください。

## 直接のモデルアクセス

基盤となるモデルにアクセスし、カスタムリスナーを追加したり高度な操作を実行したりできます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続を低レベルで制御する必要がある高度なユースケース向けに、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## コード例

動作する完全なコード例は、 UI コンポーネントの有無それぞれのデモを含む [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。