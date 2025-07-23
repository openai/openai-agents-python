---
search:
  exclude: true
---
# ガイド

このガイドでは、OpenAI Agents SDK のリアルタイム機能を使って音声対応 AI エージェントを構築する方法を詳しく説明します。

!!! warning "ベータ機能"
リアルタイムエージェントはベータ版です。実装の改善に伴い、互換性が壊れる変更が発生する可能性があります。

## 概要

リアルタイムエージェントは、音声とテキスト入力をリアルタイムで処理し、音声で応答する対話フローを実現します。OpenAI の Realtime API との永続的な接続を維持し、低レイテンシで自然な音声会話を行いながら、割り込みにもスムーズに対応できます。

## アーキテクチャ

### コアコンポーネント

リアルタイムシステムは、以下の主要コンポーネントで構成されます。

- **RealtimeAgent**: instructions、tools、handoffs を設定したエージェント  
- **RealtimeRunner**: 設定を管理します。`runner.run()` を呼び出してセッションを取得できます。  
- **RealtimeSession**: 1 回の対話セッション。ユーザーが会話を開始するたびに作成し、会話が終了するまで保持します。  
- **RealtimeModel**: 基盤となるモデルインターフェース（通常は OpenAI の WebSocket 実装）

### セッションフロー

典型的なリアルタイムセッションは次の流れで進みます。

1. instructions、tools、handoffs を使用して **RealtimeAgent** を作成する  
2. エージェントと設定オプションを指定して **RealtimeRunner** をセットアップする  
3. `await runner.run()` で **セッションを開始** し、RealtimeSession を受け取る  
4. `send_audio()` または `send_message()` で **音声またはテキストメッセージを送信** する  
5. セッションをイテレートして **イベントを監視** する  
   - イベントには音声出力、文字起こし、ツール呼び出し、ハンドオフ、エラーなどが含まれます  
6. ユーザーがエージェントの発話を遮った場合に **割り込みを処理** する（現在の音声生成が自動で停止）

セッションは会話履歴を保持し、リアルタイムモデルとの永続接続を管理します。

## エージェント設定

RealtimeAgent は通常の Agent クラスとほぼ同じですが、いくつか重要な違いがあります。完全な API 仕様は [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] リファレンスをご覧ください。

主な違い:

- モデルの選択はエージェントレベルではなくセッションレベルで設定します。  
- structured outputs（`outputType`）には対応していません。  
- 音声はエージェントごとに設定できますが、最初のエージェントが話し始めた後は変更できません。  
- tools、handoffs、instructions などその他の機能は同じように動作します。  

## セッション設定

### モデル設定

セッション設定では基盤となるリアルタイムモデルの動作を制御できます。モデル名（例: `gpt-4o-realtime-preview`）、音声（alloy、echo、fable、onyx、nova、shimmer）や対応モダリティ（テキスト／音声）を指定できます。音声の入出力フォーマットは PCM16 がデフォルトですが、変更可能です。

### オーディオ設定

オーディオ設定では、音声入力と出力の扱いを制御します。Whisper などのモデルを使った音声入力の文字起こし、言語の指定、ドメイン固有語句の精度を高めるための transcription prompts を設定できます。ターン検出では、音声活動検出の閾値、無音時間、検出前後のパディングなどにより、エージェントが応答を開始・停止するタイミングを調整します。

## ツールと関数

### ツールの追加

通常のエージェントと同様に、リアルタイムエージェントでも会話中に実行される function tools を利用できます。

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

ハンドオフを使うと、会話を専門化されたエージェント間で引き継げます。

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

セッションはストリーミングでイベントを送信するため、セッションオブジェクトをイテレートして受け取ります。主なイベント:

- **audio**: エージェントの応答からの raw 音声データ  
- **audio_end**: エージェントの発話が終了  
- **audio_interrupted**: ユーザーがエージェントの発話を遮った  
- **tool_start/tool_end**: ツール実行の開始／終了  
- **handoff**: エージェントのハンドオフが発生  
- **error**: 処理中にエラーが発生  

詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

リアルタイムエージェントでは出力ガードレールのみサポートされます。リアルタイム生成中のパフォーマンスを保つためにデバウンスされ、毎回の単語ではなく一定間隔で実行されます。デフォルトのデバウンス長は 100 文字ですが、設定で変更できます。

ガードレールが発動すると `guardrail_tripped` イベントが生成され、エージェントの現在の応答を割り込むことがあります。デバウンスにより、安全性とリアルタイム性能のバランスを取ります。テキストエージェントと異なり、リアルタイムエージェントではガードレール発動時に Exception は発生しません。

## オーディオ処理

音声を送信するには [`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio]、テキストを送るには [`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用します。

音声出力を取得するには `audio` イベントを受け取り、任意のオーディオライブラリで再生してください。`audio_interrupted` イベントを監視し、ユーザーが割り込んだ際は直ちに再生を停止し、キューにある音声をクリアします。

## モデルへの直接アクセス

低レベルの制御が必要な高度なユースケースでは、基盤モデルに直接アクセスし、カスタムリスナーを追加できます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースを直接操作できます。

## コード例

動作する完全なサンプルは、[examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) をご覧ください。UI 付き・なしのデモが含まれています。