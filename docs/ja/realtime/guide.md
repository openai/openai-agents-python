---
search:
  exclude: true
---
# ガイド

このガイドでは、 OpenAI Agents SDK のリアルタイム機能を用いて音声対応の AI エージェントを構築する方法を詳しく解説します。

!!! warning "Beta feature"
リアルタイムエージェントはベータ版です。今後の改善に伴い非互換の変更が入る可能性があります。

## 概要

リアルタイムエージェントは、音声とテキスト入力をリアルタイムに処理し、音声で応答する会話フローを実現します。 OpenAI の Realtime API との永続接続を維持することで、低レイテンシかつ自然な音声会話を可能にし、ユーザーの割り込みにもスムーズに対応します。

## アーキテクチャ

### コアコンポーネント

リアルタイムシステムは、以下の主要コンポーネントで構成されます。

- ** RealtimeAgent **: instructions、tools、handoffs を設定したエージェント  
- ** RealtimeRunner **: 設定を管理します。 `runner.run()` を呼び出してセッションを取得します  
- ** RealtimeSession **: 1 回の対話セッション。ユーザーが会話を開始するたびに作成し、会話終了まで維持します  
- ** RealtimeModel **: 基盤となるモデルインターフェース (通常は OpenAI の WebSocket 実装)

### セッションフロー

一般的なリアルタイムセッションは次の流れで進みます。

1. instructions、tools、handoffs を指定して ** RealtimeAgent ** を作成  
2. エージェントと設定オプションを渡して ** RealtimeRunner ** をセットアップ  
3. `await runner.run()` で **セッション開始**。 RealtimeSession が返されます  
4. `send_audio()` または `send_message()` を使用して **音声またはテキスト** を送信  
5. セッションをイテレートしながら **イベントを受信**。音声出力、転写、ツール呼び出し、ハンドオフ、エラーなどのイベントが含まれます  
6. ユーザーがエージェントの発話に被せて話した場合、 **割り込み** を処理。現在の音声生成を自動停止します  

セッションは会話履歴を保持し、リアルタイムモデルとの永続接続を管理します。

## エージェント設定

RealtimeAgent は通常の Agent クラスと似ていますが、いくつか重要な違いがあります。詳細な API 仕様は [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] を参照してください。

主な違い:

- モデルの選択はエージェントではなくセッションレベルで設定します  
- structured outputs (`outputType`) はサポートされていません  
- 音声はエージェントごとに設定できますが、最初のエージェントが発話した後は変更できません  
- tools、handoffs、instructions などその他の機能は同じ方法で利用できます  

## セッション設定

### モデル設定

セッション設定では、基盤となるリアルタイムモデルの挙動を制御できます。モデル名 (例: `gpt-4o-realtime-preview`)、ボイス (alloy、echo、fable、onyx、nova、shimmer)、対応モダリティ (text / audio) を指定できます。音声の入出力フォーマットは両方とも設定可能で、デフォルトは PCM16 です。

### 音声設定

音声設定では、音声入力と出力の扱いを制御します。 Whisper などのモデルを使った入力音声の転写、言語の指定、ドメイン固有語の認識精度を高める転写プロンプトの指定が可能です。ターン検出設定では、音声活動検出のしきい値、無音時間、検出音声の前後余白などを設定し、エージェントが応答を開始・終了するタイミングを調整します。

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

ハンドオフにより、会話を特化した別のエージェントへ引き継ぐことができます。

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

セッションはイベントをストリーミングします。セッションオブジェクトをイテレートしてイベントを受信してください。イベントには、音声出力チャンク、転写結果、ツール実行開始・終了、エージェントハンドオフ、エラーなどがあります。主なイベントは以下のとおりです。

- **audio**: エージェントの応答からの raw 音声データ  
- **audio_end**: エージェントの発話が終了  
- **audio_interrupted**: ユーザーがエージェントを割り込み  
- **tool_start / tool_end**: ツール実行のライフサイクル  
- **handoff**: エージェント間のハンドオフが発生  
- **error**: 処理中にエラーが発生  

詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

リアルタイムエージェントでは出力ガードレールのみサポートされています。パフォーマンス低下を防ぐため、ガードレールはデバウンスされ、リアルタイム生成のたびにではなく周期的に実行されます。デフォルトのデバウンス長は 100 文字ですが、変更可能です。

ガードレールが発火すると `guardrail_tripped` イベントが生成され、エージェントの現在の応答を中断できます。デバウンスにより、安全性とリアルタイム性能のバランスを取っています。テキストエージェントと異なり、リアルタイムエージェントはガードレール発火時に Exception を送出しません。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使って音声を、 [`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] でテキストをセッションへ送信できます。

音声出力を受け取るには `audio` イベントを監視し、任意の音声ライブラリで再生してください。ユーザーが割り込んだ場合に即座に再生を停止し、キューにある音声をクリアするため、 `audio_interrupted` イベントも必ず監視してください。

## 直接モデルアクセス

基盤となるモデルにアクセスし、独自リスナーを追加したり高度な操作を行うことができます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、低レベルで接続を制御したい高度なユースケース向けに [`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## 例

動作する完全なサンプルは、 [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。 UI コンポーネントあり / なし両方のデモが含まれています。