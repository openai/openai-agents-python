---
search:
  exclude: true
---
# ガイド

本ガイドでは、OpenAI Agents SDK の realtime 機能を利用して音声対応の AI エージェントを構築する方法を詳しく解説します。

!!! warning "Beta 機能"
Realtime エージェントはベータ版です。実装の改善に伴い、破壊的変更が発生する可能性があります。

## 概要

Realtime エージェントは、音声とテキストをリアルタイムで処理し、リアルタイム音声で応答する対話フローを実現します。OpenAI の Realtime API との持続的な接続を維持し、低レイテンシーで自然な音声会話を可能にし、割り込みにもスムーズに対応します。

## アーキテクチャ

### コアコンポーネント

Realtime システムは以下の主要コンポーネントで構成されます。

- **RealtimeAgent**: instructions、tools、handoffs で構成されたエージェント。
- **RealtimeRunner**: 設定を管理します。`runner.run()` を呼び出してセッションを取得します。
- **RealtimeSession**: 1 つの対話セッション。ユーザーが会話を開始するたびに作成し、会話が終了するまで保持します。
- **RealtimeModel**: 基盤となるモデルインターフェース（通常は OpenAI の WebSocket 実装）。

### セッションフロー

典型的な realtime セッションは以下の流れで進行します。

1. instructions、tools、handoffs を指定して **RealtimeAgent** を作成します。  
2. エージェントと設定オプションを使用して **RealtimeRunner** をセットアップします。  
3. `await runner.run()` で **セッションを開始**し、RealtimeSession を取得します。  
4. `send_audio()` または `send_message()` を使って **音声またはテキストメッセージを送信**します。  
5. セッションをイテレートして **イベントを監視**します。イベントには音声出力、文字起こし、tool 呼び出し、handoff、エラーなどがあります。  
6. ユーザーがエージェントの発話に被せて話した場合は **割り込みを処理**し、現在の音声生成を自動的に停止します。  

セッションは会話履歴を保持し、realtime モデルとの持続的な接続を管理します。

## エージェントの設定

RealtimeAgent は通常の Agent クラスと似ていますが、いくつか重要な違いがあります。詳細は [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] API リファレンスをご覧ください。

通常のエージェントとの主な違い:

- モデルの選択はエージェントではなくセッションレベルで設定します。  
- structured outputs（`outputType`）はサポートされていません。  
- voice はエージェントごとに設定できますが、最初のエージェントが話し始めた後は変更できません。  
- tools、handoffs、instructions などその他の機能は同様に動作します。  

## セッション設定

### モデル設定

セッション設定では、基盤となる realtime モデルの挙動を制御できます。モデル名（例: `gpt-4o-realtime-preview`）、voice の選択（alloy、echo、fable、onyx、nova、shimmer）、対応モダリティ（text と/または audio）を設定できます。入出力の音声フォーマットは両方とも設定可能で、デフォルトは PCM16 です。

### オーディオ設定

オーディオ設定では、音声入力と出力の扱い方を制御します。Whisper などのモデルを用いた入力音声の文字起こし、言語設定、ドメイン固有用語の認識精度を高める文字起こしプロンプトを指定できます。ターン検知設定では、音声活動検出のしきい値、無音時間、検出された音声前後のパディングを調整し、エージェントがいつ応答を開始・停止すべきかを制御します。

## ツールと関数

### ツールの追加

通常のエージェントと同様に、realtime エージェントでも会話中に実行される function tools をサポートしています。

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

ハンドオフを使用すると、会話を専門のエージェント間で転送できます。

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

## イベントハンドリング

セッションはイベントをストリーミングします。セッションオブジェクトをイテレートしてイベントを監視できます。主なイベントは次のとおりです。

- **audio**: エージェントの応答から得られる raw 音声データ  
- **audio_end**: エージェントの発話が終了  
- **audio_interrupted**: ユーザーがエージェントを割り込み  
- **tool_start/tool_end**: tool 実行のライフサイクル  
- **handoff**: エージェント間の handoff が発生  
- **error**: 処理中にエラーが発生  

完全なイベント詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

Realtime エージェントでは出力ガードレールのみがサポートされています。パフォーマンスへの影響を避けるため、ガードレールは毎単語ではなくデバウンス（間隔制御）されて定期的に実行されます。デフォルトのデバウンス長は 100 文字ですが、設定可能です。

ガードレールがトリップすると `guardrail_tripped` イベントが発生し、エージェントの現在の応答を割り込むことがあります。デバウンス動作により、安全性とリアルタイム性能を両立しています。テキストエージェントと異なり、realtime エージェントではガードレールがトリップしても Exception は発生しません。

## オーディオ処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使って音声を、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使ってテキストをセッションに送信できます。

音声出力を扱う際は `audio` イベントを監視し、好みのオーディオライブラリで再生してください。ユーザーが割り込んだ場合は `audio_interrupted` イベントを受け取り、即座に再生を停止してキューにある音声をクリアするようにしてください。

## 直接モデルへアクセス

下記のように基盤モデルへ直接アクセスし、カスタムリスナーを追加したり高度な操作を行えます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、低レベルで接続を制御する必要がある高度なユースケース向けに [`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## コード例

動作する完全なコード例は、[examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) をご覧ください。UI コンポーネントあり・なしのデモが含まれています。