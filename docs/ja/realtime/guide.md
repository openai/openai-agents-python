---
search:
  exclude: true
---
# ガイド

このガイドでは、OpenAI Agents SDK の realtime 機能を用いた音声対応 AI エージェントの構築方法を詳しく解説します。

!!! warning "Beta feature"
Realtime エージェントはベータ版です。実装改善に伴い、互換性が失われる可能性があります。

## 概要

Realtime エージェントは、音声とテキスト入力をリアルタイムで処理し、音声で応答する会話フローを実現します。OpenAI の Realtime API との永続接続を維持し、低遅延かつ自然な音声対話を可能にし、割り込みにもスムーズに対応します。

## アーキテクチャ

### 主要コンポーネント

Realtime システムは次の主要コンポーネントで構成されます。

-   **RealtimeAgent**: instructions、tools、handoffs を設定したエージェント。
-   **RealtimeRunner**: 設定を管理します。`runner.run()` を呼び出してセッションを取得します。
-   **RealtimeSession**: 1 回の対話セッション。ユーザーが会話を開始するたびに作成し、会話が終了するまで保持します。
-   **RealtimeModel**: 基盤となるモデル インターフェース (通常は OpenAI の WebSocket 実装)。

### セッションフロー

典型的な realtime セッションは次の流れで進行します。

1. **RealtimeAgent** を instructions、tools、handoffs と共に作成する  
2. エージェントと設定オプションを用いて **RealtimeRunner** を準備する  
3. `await runner.run()` で **セッションを開始** し、RealtimeSession を取得する  
4. `send_audio()` または `send_message()` で **音声またはテキストを送信** する  
5. セッションをイテレートして **イベントを監視** する — 音声出力、文字起こし、ツール呼び出し、ハンドオフ、エラーなど  
6. ユーザーが話し始めたら **割り込みを処理** し、現在の音声生成を自動で停止させる  

セッションは会話履歴を保持し、realtime モデルとの永続接続を管理します。

## エージェント設定

RealtimeAgent は通常の Agent クラスと似ていますが、いくつかの重要な違いがあります。詳細は [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] API リファレンスをご覧ください。

主な違い:

-   モデル選択はエージェントではなくセッションレベルで設定します。
-   structured outputs (`outputType`) はサポートされません。
-   音声はエージェント単位で設定できますが、最初のエージェントが発話した後は変更できません。
-   tools、handoffs、instructions などその他の機能は同じ方法で動作します。

## セッション設定

### モデル設定

セッション設定では、基盤となる realtime モデルの挙動を制御できます。モデル名 (例: `gpt-4o-realtime-preview`)、音声 (alloy、echo、fable、onyx、nova、shimmer)、対応モダリティ (text/audio) を指定可能です。入出力の音声フォーマットは両方とも設定でき、デフォルトは  PCM16 です。

### オーディオ設定

オーディオ設定では、音声入力と出力の扱いを制御します。Whisper などのモデルで入力音声を文字起こししたり、言語を指定したり、専門用語の認識精度を高める transcription prompt を提供できます。ターン検出では、音声アクティビティ検出のしきい値、無音時間、前後のパディングなどを調整し、エージェントが発話を開始・終了すべきタイミングを制御します。

## ツールと関数

### ツールの追加

通常のエージェントと同様、realtime エージェントでも会話中に実行される function tools を利用できます。

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

ハンドオフを使用すると、会話を専門化されたエージェント間で引き継げます。

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

セッションはイベントをストリーミングします。セッションオブジェクトをイテレートしてイベントを受け取ります。主なイベントは以下のとおりです。

-   **audio**: エージェントの応答としての raw 音声データ
-   **audio_end**: エージェントの発話が終了
-   **audio_interrupted**: ユーザーがエージェントを割り込み
-   **tool_start/tool_end**: ツール実行の開始・終了
-   **handoff**: エージェント間のハンドオフ発生
-   **error**: 処理中にエラー発生

詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

Realtime エージェントでは出力ガードレールのみがサポートされています。パフォーマンス低下を防ぐため、ガードレールはデバウンスされ、一定間隔 (デフォルトは 100 文字) ごとに評価されます。

ガードレールが発動すると `guardrail_tripped` イベントが生成され、エージェントの現在の返答を中断する場合があります。デバウンスにより、安全性とリアルタイム性能のバランスを取っています。テキストエージェントと異なり、ガードレール発動時に Exception はスローされません。

## オーディオ処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] で音声を、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] でテキストを送信できます。

音声出力を処理するには `audio` イベントを受信し、お好みのオーディオライブラリで再生してください。ユーザーが割り込んだ際には `audio_interrupted` イベントを監視し、即座に再生を停止してキューに残る音声をクリアしてください。

## モデルへの直接アクセス

カスタムリスナーの追加や高度な操作を行うため、基盤モデルに直接アクセスできます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、低レベルで接続を制御したい高度なユースケース向けに [`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースを直接利用できます。

## コード例

完全な動作例は、[examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。UI あり・なしのデモが含まれています。