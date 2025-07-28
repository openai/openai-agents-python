---
search:
  exclude: true
---
# ガイド

このガイドでは、 OpenAI Agents SDK のリアルタイム機能を使って音声対応 AI エージェントを構築する方法を詳しく解説します。

!!! warning "Beta feature"
Realtime エージェントはベータ版です。今後の改善に伴い、破壊的変更が入る可能性があります。

## 概要

Realtime エージェントは、音声とテキスト入力をリアルタイムで処理し、音声で応答する対話フローを実現します。 OpenAI の Realtime API と永続的に接続を維持し、低レイテンシで自然な音声会話を行い、割り込みにも柔軟に対応できます。

## アーキテクチャ

### コアコンポーネント

リアルタイムシステムは以下の主要コンポーネントで構成されます。

-   **RealtimeAgent**: instructions、tools、handoffs を設定したエージェント。
-   **RealtimeRunner**: 設定を管理します。 `runner.run()` を呼び出してセッションを取得できます。
-   **RealtimeSession**: 1 回の対話セッション。ユーザーが会話を開始するたびに作成し、会話終了まで保持します。
-   **RealtimeModel**: 基盤となるモデルインターフェース (通常は OpenAI の WebSocket 実装)。

### セッションフロー

典型的なリアルタイムセッションは次のように進行します。

1. **RealtimeAgent を作成**: instructions、tools、handoffs を設定します。  
2. **RealtimeRunner をセットアップ**: エージェントと設定オプションを渡します。  
3. **セッションを開始**: `await runner.run()` を実行し、 RealtimeSession を取得します。  
4. **音声またはテキストを送信**: `send_audio()` あるいは `send_message()` を使用します。  
5. **イベントを受信**: セッションをイテレートしてイベントを監視します。イベントには音声出力、文字起こし、ツール呼び出し、ハンドオフ、エラーが含まれます。  
6. **割り込みを処理**: ユーザーがエージェントの発話中に話し始めた場合、自動的に現在の音声生成を停止します。  

セッションは会話履歴を保持し、リアルタイムモデルとの永続接続を管理します。

## エージェント設定

RealtimeAgent は通常の Agent クラスとほぼ同様ですが、いくつかの相違点があります。詳細は [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] API リファレンスをご覧ください。

主な相違点:

-   モデルの選択はエージェントではなくセッションで設定します。  
-   structured outputs はサポートされません (`outputType` 非対応)。  
-   声色 (voice) はエージェントごとに設定できますが、最初のエージェントが発話した後は変更できません。  
-   tools、handoffs、instructions などその他の機能は同じように動作します。  

## セッション設定

### モデル設定

セッション設定では基盤となるリアルタイムモデルの挙動を制御できます。モデル名 (例: `gpt-4o-realtime-preview`) や音声 (alloy、echo、fable、onyx、nova、shimmer)、対応モダリティ (text / audio) を指定できます。入力・出力の音声フォーマットも設定でき、デフォルトは PCM16 です。

### 音声設定

音声設定では音声入力と出力の扱いを制御します。 Whisper などのモデルを使用した音声文字起こし、言語設定、専門用語の認識精度を高めるための transcription prompts を設定できます。ターン検出では、音声活動検出の閾値、無音継続時間、検出された発話前後のパディングなどを調整できます。

## ツールと Functions

### ツールの追加

通常のエージェントと同様に、リアルタイムエージェントは会話中に実行される function tools をサポートします。

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

ハンドオフを使うと、会話を専門化されたエージェント間で引き継ぐことができます。

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

セッションはイベントをストリーム配信します。セッションオブジェクトをイテレートしてイベントを受信してください。主なイベントは以下のとおりです。

-   **audio**: エージェントの応答からの raw 音声データ  
-   **audio_end**: エージェントの発話終了  
-   **audio_interrupted**: ユーザーによる割り込み  
-   **tool_start/tool_end**: ツール実行の開始・終了  
-   **handoff**: エージェントのハンドオフ発生  
-   **error**: 処理中に発生したエラー  

完全なイベント仕様は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

リアルタイムエージェントでサポートされるガードレールは出力時のみです。パフォーマンス低下を防ぐためにデバウンス処理が行われ、毎単語ではなく一定間隔で評価されます。既定のデバウンス長は 100 文字ですが、変更可能です。

ガードレールがトリガーされると `guardrail_tripped` イベントが発生し、エージェントの現在の応答を割り込むことがあります。テキストエージェントと異なり、リアルタイムエージェントではガードレール発動時に Exception は発生しません。

## 音声処理

音声を送信するには [`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio]、テキストを送信するには [`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用します。

音声出力を受信するには `audio` イベントを監視し、任意の音声ライブラリで再生してください。ユーザーが割り込んだ際には `audio_interrupted` イベントを検知して即座に再生を停止し、キューに残っている音声をクリアする必要があります。

## 直接モデルアクセス

低レベルの制御やカスタムリスナーを追加したい場合は、基盤モデルに直接アクセスできます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、高度なユースケース向けに [`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## 例

動作する完全なコード例は、 UI あり / なしのデモを含む [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) をご覧ください。