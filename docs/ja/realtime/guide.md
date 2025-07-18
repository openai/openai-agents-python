---
search:
  exclude: true
---
# ガイド

このガイドでは、OpenAI Agents SDK の realtime 機能を使って音声対応の AI エージェントを構築する方法を詳しく解説します。

!!! warning "ベータ機能"
Realtime エージェントはベータ版です。実装の改善に伴い、破壊的変更が発生する可能性があります。

## 概要

Realtime エージェントは、音声とテキスト入力をリアルタイムで処理し、音声で応答する会話フローを実現します。OpenAI の Realtime API と永続的に接続を保ち、低レイテンシで自然な音声対話を可能にし、割り込みにもスムーズに対応します。

## アーキテクチャ

### 主要コンポーネント

realtime システムは、以下の主要コンポーネントで構成されています。

-   **RealtimeAgent**: instructions、tools、handoffs で設定されるエージェント。  
-   **RealtimeRunner**: 設定を管理します。`runner.run()` を呼び出してセッションを取得できます。  
-   **RealtimeSession**: 単一の対話セッション。ユーザーが会話を開始するたびに作成し、会話が終了するまで保持します。  
-   **RealtimeModel**: 基盤となるモデルインターフェース（通常は OpenAI の WebSocket 実装）。  

### セッションフロー

典型的な realtime セッションは次のように進行します。

1. **RealtimeAgent** を instructions、tools、handoffs で作成します。  
2. **RealtimeRunner** をエージェントと設定オプションでセットアップします。  
3. `await runner.run()` で **セッションを開始** し、RealtimeSession を受け取ります。  
4. `send_audio()` または `send_message()` で **音声またはテキスト** をセッションへ送信します。  
5. セッションを反復処理して **イベントを受信** します。イベントには音声出力、文字起こし、tool 呼び出し、handoff、エラーなどが含まれます。  
6. ユーザーがエージェントの発話を **割り込んだ場合**、現在の音声生成が自動で停止します。  

セッションは会話履歴を保持し、realtime モデルとの永続接続を管理します。

## エージェント設定

RealtimeAgent は通常の Agent クラスとほぼ同様に動作しますが、いくつか重要な違いがあります。API の詳細は [`RealtimeAgent`](agents.realtime.agent.RealtimeAgent) リファレンスをご覧ください。

主な違い:

-   モデルの選択はエージェントではなくセッションで設定します。  
-   structured outputs（`outputType`）はサポートされません。  
-   声質はエージェントごとに設定できますが、最初のエージェントが発話した後に変更できません。  
-   tools、handoffs、instructions などの他の機能は同じ方法で利用できます。  

## セッション設定

### モデル設定

セッション設定では、基盤となる realtime モデルの動作を制御できます。モデル名（例: `gpt-4o-realtime-preview`）、声質（alloy、echo、fable、onyx、nova、shimmer）、対応モダリティ（text と/または audio）を指定可能です。音声フォーマットは入力と出力で設定でき、デフォルトは PCM16 です。

### 音声設定

音声設定では、音声入力と出力の扱いを制御します。Whisper などのモデルで入力音声を文字起こししたり、言語を指定したり、専門用語の精度向上のため transcription プロンプトを提供したりできます。ターン検出では、音声検出閾値、無音時間、検出された音声の前後パディングなどを調整して、エージェントがいつ応答を開始・停止すべきかを制御します。

## Tools と Functions

### ツールの追加

通常のエージェントと同様に、realtime エージェントも会話中に実行される function tools をサポートします。

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

ハンドオフを使うことで、会話を専門エージェント間で引き継げます。

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

セッションはイベントをストリーム配信します。セッションオブジェクトを反復処理してイベントを受信してください。主なイベントは次のとおりです。

-   **audio**: エージェントの応答音声データ  
-   **audio_end**: エージェントの発話終了  
-   **audio_interrupted**: ユーザーによる割り込み  
-   **tool_start/tool_end**: tool 実行の開始と終了  
-   **handoff**: エージェント間の handoff  
-   **error**: 処理中に発生したエラー  

詳細は [`RealtimeSessionEvent`](agents.realtime.events.RealtimeSessionEvent) を参照してください。

## ガードレール

Realtime エージェントでは出力ガードレールのみサポートされます。パフォーマンス低下を防ぐため、ガードレールはデバウンスされ、（1 文字ごとではなく）定期的に実行されます。デフォルトのデバウンス長は 100 文字ですが、変更可能です。

ガードレールが発火すると `guardrail_tripped` イベントが生成され、エージェントの現在の応答が中断されることがあります。テキストエージェントと違い、Realtime エージェントではガードレール発火時に Exception は発生しません。

## 音声処理

[`session.send_audio(audio_bytes)`](agents.realtime.session.RealtimeSession.send_audio) で音声を、[`session.send_message()`](agents.realtime.session.RealtimeSession.send_message) でテキストを送信できます。

音声出力を扱う際は `audio` イベントを受信し、任意の音声ライブラリで再生してください。ユーザーが割り込んだ場合は `audio_interrupted` イベントを検知して、即座に再生を停止し、キューにある音声をクリアする必要があります。

## 直接モデルアクセス

基盤モデルにアクセスして、カスタムリスナーを追加したり高度な操作を行ったりできます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、低レベルで接続を制御したい高度なユースケース向けに [`RealtimeModel`](agents.realtime.model.RealtimeModel) インターフェースへ直接アクセスできます。

## コード例

完全な動作例は、[examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) をご覧ください。UI あり・なしのデモを含みます。