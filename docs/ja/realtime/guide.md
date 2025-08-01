---
search:
  exclude: true
---
# ガイド

本ガイドでは、OpenAI Agents SDK の realtime 機能を利用して音声対応の AI エージェントを構築する方法を詳しく説明します。

!!! warning "Beta feature"
Realtime エージェントはベータ版です。実装を改善する過程で破壊的変更が入る可能性があります。

## 概要

Realtime エージェントは、音声とテキスト入力をリアルタイムで処理し、リアルタイム音声で応答できる会話フローを実現します。OpenAI の Realtime API との永続接続を維持し、低レイテンシで自然な音声対話と、割り込みへのスムーズな対応が可能です。

## アーキテクチャ

### 主要コンポーネント

realtime システムは次の主要コンポーネントで構成されます。

- **RealtimeAgent**: instructions、tools、handoffs を設定したエージェントです。  
- **RealtimeRunner**: 設定を管理します。`runner.run()` を呼び出すことでセッションを取得できます。  
- **RealtimeSession**: 単一の対話セッションです。ユーザーが会話を開始するたびに作成し、会話が終了するまで保持します。  
- **RealtimeModel**: 基盤となるモデルインターフェース（通常は OpenAI の WebSocket 実装）です。  

### セッションフロー

一般的な realtime セッションの流れは次のとおりです。

1. instructions、tools、handoffs を設定して **RealtimeAgent** を作成します。  
2. そのエージェントと設定オプションを使って **RealtimeRunner** をセットアップします。  
3. `await runner.run()` で **セッションを開始** し、`RealtimeSession` を取得します。  
4. `send_audio()` または `send_message()` で **音声またはテキストメッセージを送信** します。  
5. セッションをイテレートして **イベントをリッスン** します。イベントには音声出力、文字起こし、ツール呼び出し、ハンドオフ、エラーが含まれます。  
6. ユーザーがエージェントの発話を遮った場合に **割り込みを処理** します。割り込みが発生すると現在の音声生成は自動的に停止します。  

セッションは会話履歴を保持し、realtime モデルとの永続接続を管理します。

## エージェント設定

RealtimeAgent は通常の Agent クラスとほぼ同じですが、いくつかの重要な違いがあります。API の詳細は [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] をご覧ください。

通常のエージェントとの主な違い:

- モデルの選択はエージェントレベルではなくセッションレベルで設定します。  
- structured outputs (`outputType`) はサポートされていません。  
- 音声はエージェントごとに設定できますが、最初のエージェントが話し始めた後は変更できません。  
- tools、handoffs、instructions などのその他の機能は同じ方法で機能します。  

## セッション設定

### モデル設定

セッション設定では、基盤となる realtime モデルの動作を制御できます。モデル名（例: `gpt-4o-realtime-preview`）、音声選択（alloy、echo、fable、onyx、nova、shimmer）、対応モダリティ（text と／または audio）を指定できます。入出力の音声フォーマットはどちらも PCM16 がデフォルトです。

### 音声設定

音声設定では、セッションが音声入力と出力をどのように扱うかを制御します。Whisper などのモデルを使用した入力音声の文字起こし、言語の指定、ドメイン固有用語の精度を高めるための文字起こしプロンプトが設定可能です。ターン検出設定では、音声検出閾値、無音時間、検出された音声周辺のパディングなどを調整し、エージェントがいつ応答を開始・終了すべきかを決定します。

## ツールと関数

### ツールの追加

通常のエージェントと同様に、realtime エージェントは会話中に実行される function tools をサポートします。

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

ハンドオフを使用すると、会話を専門エージェント間で引き継ぐことができます。

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

セッションはイベントをストリーミングします。セッションオブジェクトをイテレートしてイベントをリッスンしてください。イベントには音声出力チャンク、文字起こし結果、ツール実行開始と終了、エージェントのハンドオフ、エラーが含まれます。主に処理すべきイベントは次のとおりです。

- **audio**: エージェントの応答からの raw 音声データ  
- **audio_end**: エージェントが発話を完了  
- **audio_interrupted**: ユーザーがエージェントを割り込み  
- **tool_start/tool_end**: ツール実行ライフサイクル  
- **handoff**: エージェントハンドオフが発生  
- **error**: 処理中にエラーが発生  

完全なイベント詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

realtime エージェントでは出力ガードレールのみサポートされています。ガードレールはデバウンスされ、リアルタイム生成中のパフォーマンス問題を避けるために定期的（全単語ではなく）に実行されます。デフォルトのデバウンス長は 100 文字ですが、設定可能です。

ガードレールがトリガーされると `guardrail_tripped` イベントが生成され、エージェントの現在の応答を中断できます。このデバウンス動作により、安全性とリアルタイム性能のバランスが取れます。テキストエージェントとは異なり、realtime エージェントはガードレールがトリップしても Exception を送出しません。

## 音声処理

音声を送信するには [`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio]、テキストを送信するには [`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用します。

音声出力を再生するには `audio` イベントをリッスンし、好みの音声ライブラリで音声データを再生してください。ユーザーがエージェントを割り込んだ際には `audio_interrupted` イベントをリッスンしてすぐに再生を停止し、キューにある音声をクリアするようにしてください。

## 直接モデルアクセス

基盤となるモデルにアクセスしてカスタムリスナーを追加したり、高度な操作を行うことができます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、より低レベルで接続を制御する必要がある高度なユースケース向けに [`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## コード例

完全な動作例は [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。UI コンポーネントの有無にかかわらずデモを確認できます。