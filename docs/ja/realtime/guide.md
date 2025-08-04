---
search:
  exclude: true
---
# ガイド

このガイドでは、 OpenAI  Agents SDK の realtime 機能を使用して音声対応 AI  エージェントを構築する方法を詳しく説明します。

!!! warning "Beta feature"
Realtime エージェントはベータ版です。実装の改良に伴い、互換性が破壊される変更が入る可能性があります。

## 概要

Realtime エージェントは、音声とテキスト入力をリアルタイムで処理し、リアルタイム音声で応答する会話フローを可能にします。これらは OpenAI の Realtime API と永続的な接続を維持し、低レイテンシで自然な音声対話と割り込み処理を実現します。

## アーキテクチャ

### 主要コンポーネント

realtime システムは以下の主要コンポーネントで構成されます。

- ** RealtimeAgent**: instructions、tools、handoffs で構成されたエージェント  
- ** RealtimeRunner**: 設定を管理します。 `runner.run()` を呼び出してセッションを取得します  
- ** RealtimeSession**: 単一の対話セッション。ユーザーが会話を開始するたびに作成し、会話が終了するまで保持します  
- ** RealtimeModel**: 基盤となるモデルインターフェース (通常は OpenAI の WebSocket 実装)

### セッションフロー

典型的な realtime セッションの流れは次のとおりです。

1. ** RealtimeAgent** を instructions、tools、handoffs 付きで作成します  
2. ** RealtimeRunner** をエージェントと設定オプションでセットアップします  
3. `await runner.run()` を使用して **セッションを開始** し、 RealtimeSession を取得します  
4. `send_audio()` または `send_message()` で **音声またはテキストメッセージを送信** します  
5. セッションを反復処理して **イベントをリッスン** します — イベントには音声出力、トランスクリプト、ツール呼び出し、ハンドオフ、エラーが含まれます  
6. ユーザーがエージェントの話し中に話した場合 **割り込みを処理** し、現在の音声生成を自動で停止します  

セッションは会話履歴を保持し、リアルタイムモデルとの永続接続を管理します。

## エージェント設定

RealtimeAgent は通常の Agent クラスと似ていますが、いくつか重要な違いがあります。完全な API 詳細は [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] を参照してください。

通常のエージェントとの主な違い:

- モデル選択はエージェントではなくセッションレベルで設定します  
- structured outputs は非対応 (`outputType` は使用不可)  
- 音声はエージェントごとに設定できますが、最初のエージェントが話した後は変更できません  
- tools、handoffs、instructions など他の機能は同じように動作します  

## セッション設定

### モデル設定

セッション設定では、基盤となる realtime モデルの動作を制御できます。モデル名 (例: `gpt-4o-realtime-preview`)、音声 (alloy、echo、fable、onyx、nova、shimmer)、サポートするモダリティ (テキストおよび / または音声) を指定できます。音声フォーマットは入力・出力ともに設定可能で、デフォルトは PCM16 です。

### オーディオ設定

オーディオ設定では、音声入力と出力の扱いを制御します。Whisper などのモデルを使用した入力音声の文字起こし、言語設定、ドメイン固有用語の精度向上用トランスクリプションプロンプトが指定できます。ターン検出設定では、音声活動検出のしきい値、無音時間、検出した音声の前後パディングなど、エージェントがいつ応答を開始・停止するかを調整します。

## ツールと関数

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

セッションはイベントをストリーム配信します。セッションオブジェクトを反復処理してリッスンしてください。イベントには音声出力チャンク、トランスクリプション結果、ツール実行開始・終了、エージェントハンドオフ、エラーなどがあります。主なイベント:

- **audio**: エージェント応答の raw 音声データ  
- **audio_end**: エージェントが話し終えた  
- **audio_interrupted**: ユーザーがエージェントを割り込んだ  
- **tool_start / tool_end**: ツール実行のライフサイクル  
- **handoff**: エージェントのハンドオフが発生  
- **error**: 処理中にエラーが発生  

完全なイベント詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

Realtime エージェントでは出力ガードレールのみがサポートされます。パフォーマンス低下を防ぐためにデバウンスされ、リアルタイム生成中に毎単語ではなく定期的に実行されます。デフォルトのデバウンス長は 100 文字で、設定可能です。

ガードレールがトリガーされると `guardrail_tripped` イベントが生成され、エージェントの現在の応答を中断できます。デバウンス動作により安全性とリアルタイム性能のバランスを取ります。テキストエージェントとは異なり、realtime エージェントはガードレールがトリップしても Exception を発生させません。

## オーディオ処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] で音声を送信するか、 [`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] でテキストを送信します。

音声出力を再生するには `audio` イベントをリッスンし、好みのオーディオライブラリで再生してください。ユーザーが割り込んだ際は `audio_interrupted` イベントをリッスンし、直ちに再生を停止してキューにある音声をクリアします。

## モデルへの直接アクセス

基盤となるモデルにアクセスしてカスタムリスナーを追加したり、高度な操作を行うことができます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、高度なユースケース向けに低レベルで接続を制御できる [`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## コード例

完全な動作例は [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。 UI コンポーネントあり / なしのデモが含まれています。