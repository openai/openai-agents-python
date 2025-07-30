---
search:
  exclude: true
---
# ガイド

このガイドでは、OpenAI Agents SDK の realtime 機能を用いて音声対応の AI エージェントを構築する方法を詳しく説明します。

!!! warning "Beta feature"
Realtime エージェントはベータ版です。今後の改善に伴い、互換性が壊れる変更が入る可能性があります。

## 概要

Realtime エージェントは、音声とテキスト入力をリアルタイムで処理し、音声で応答できる会話フローを実現します。OpenAI の Realtime API との永続的な接続を維持し、低遅延かつ自然な音声会話や割り込みへのスムーズな対応が可能です。

## アーキテクチャ

### コアコンポーネント

realtime システムは以下の主要コンポーネントで構成されています。

- **RealtimeAgent**: instructions、tools、handoffs を設定したエージェント。
- **RealtimeRunner**: 設定を管理します。`runner.run()` を呼び出すとセッションが取得できます。
- **RealtimeSession**: 単一の対話セッション。通常、ユーザーが会話を開始するたびに作成し、会話終了まで保持します。
- **RealtimeModel**: 基盤となるモデルインターフェース（通常は OpenAI の WebSocket 実装）。

### セッションフロー

一般的な realtime セッションの流れは次のとおりです。

1. **RealtimeAgent を作成** し、instructions・tools・handoffs を設定します。  
2. **RealtimeRunner をセットアップ** し、エージェントと各種オプションを指定します。  
3. `await runner.run()` で **セッションを開始** し、RealtimeSession を取得します。  
4. `send_audio()` または `send_message()` を使用して **音声またはテキストメッセージを送信** します。  
5. セッションをイテレートして **イベントを監視** します。イベントには音声出力、文字起こし、tool 呼び出し、handoff、エラーなどがあります。  
6. ユーザーがエージェントの発話を遮った場合に **割り込みを処理** します。これにより現在の音声生成が自動で停止します。  

セッションは会話履歴を保持し、realtime モデルとの永続接続を管理します。

## エージェント設定

RealtimeAgent は通常の Agent クラスとほぼ同様に機能しますが、いくつか重要な違いがあります。詳細は [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] API リファレンスを参照してください。

通常のエージェントとの主な違い:

- モデルの選択はエージェントレベルではなくセッションレベルで設定します。  
- structured outputs（`outputType`）はサポートされません。  
- Voice はエージェントごとに設定できますが、最初のエージェントが話し始めた後は変更できません。  
- tools、handoffs、instructions などその他の機能は同じように使えます。  

## セッション設定

### モデル設定

セッション設定では、基盤となる realtime モデルの動作を制御できます。モデル名（例: `gpt-4o-realtime-preview`）、ボイス選択（alloy、echo、fable、onyx、nova、shimmer）、対応モダリティ（テキストおよび/または音声）を指定可能です。音声フォーマットは入力・出力ともに設定でき、デフォルトは PCM16 です。

### 音声設定

音声設定では、音声入力と出力の取り扱いを制御します。Whisper などのモデルを使った音声入力の文字起こし、言語設定、ドメイン固有用語の精度向上のための transcription prompts を指定できます。ターン検出設定では、音声活動検出のしきい値、無音時間、検出された発話前後のパディングなどを調整し、エージェントが発話を開始・終了するタイミングを制御します。

## ツールと関数

### ツールの追加

通常のエージェントと同様に、realtime エージェントでも会話中に実行される function tools を利用できます。

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

ハンドオフを使用すると、会話を専門化されたエージェント間で転送できます。

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

セッションはイベントをストリーム配信するため、セッションオブジェクトをイテレートして監視できます。主なイベントは次のとおりです。

- **audio**: エージェントの応答から得られる raw 音声データ  
- **audio_end**: エージェントが話し終えた  
- **audio_interrupted**: ユーザーがエージェントを割り込んだ  
- **tool_start/tool_end**: tool 実行のライフサイクル  
- **handoff**: エージェント間のハンドオフが発生  
- **error**: 処理中にエラーが発生  

詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

realtime エージェントでは出力ガードレールのみサポートされます。性能への影響を避けるため、ガードレールはデバウンスされ、リアルタイム生成のすべての単語ではなく一定間隔ごと（デフォルト 100 文字）に実行されます。設定で変更可能です。

ガードレールが発動すると `guardrail_tripped` イベントが生成され、エージェントの現在の応答を中断できます。デバウンスによって安全性とリアルタイム性能のバランスを取ります。テキストエージェントと異なり、realtime エージェントではガードレール発動時に Exception は発生しません。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] で音声を、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] でテキストを送信できます。

音声出力を受け取るには `audio` イベントを監視し、好みのオーディオライブラリで再生してください。`audio_interrupted` イベントをリッスンして、ユーザーが割り込んだ際に再生を即座に停止し、キューに残っている音声もクリアするようにしましょう。

## 直接モデルアクセス

下位レベルの制御やカスタムリスナー追加のために、基盤モデルへ直接アクセスできます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、高度なユースケース向けに [`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## コード例

完全な動作例は [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。UI あり・なしのデモが含まれています。